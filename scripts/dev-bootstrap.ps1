<#
.SYNOPSIS
Set up or repair an eVi development environment (Windows PowerShell 5.1+).

.DESCRIPTION
  powershell -ExecutionPolicy Bypass -File scripts\dev-bootstrap.ps1 [options]

  -Venv DIR         the venv to use (default: $env:EVI_DEV_VENV, else
                    ~\.venvs\evi-<version>, e.g. ~\.venvs\evi-313). Keep it
                    outside the checkout: a venv records one machine's
                    interpreter path, so a checkout shared between machines
                    must not carry it.
  -Python VER       Python for a new venv (default 3.13; 3.12+ required)
  -Extras LIST      extras to install
                    (default dev,web,mcp,scheduler,downloads,web-tools,index,e2e)
  -Recreate         delete and recreate a venv that does not start although
                    its interpreter exists (broken, older than 3.12), or a
                    foreign one inside a git checkout
  -Reinstall        reinstall eVi + pytest-xdist into an existing venv
  -Playwright       also download Chromium for tests/e2e
  -Worktree BRANCH  create (or reuse) <repo>\.worktrees\BRANCH and check it
  -Base REF         start point for a new worktree branch (default HEAD)
  -Repo DIR         the checkout (default: the one holding this script)
  -DryRun           print what would happen and change nothing
  -Test [ARGS]      once the check passes, run the venv's
                    `python -m pytest -q -p no:cacheprovider ARGS` in the
                    checkout (in the worktree, with -Worktree) and exit with
                    pytest's code. The arguments no option takes go to
                    pytest, except any PowerShell reads as a parameter
                    (-v is -Venv, --verbose is -Verbose, -p is ambiguous):
                    pass those in $env:PYTEST_ADDOPTS instead.

A working venv is reused. One that is plainly foreign (the interpreter its
pyvenv.cfg names does not exist on this machine) and outside any git checkout
is moved aside, never deleted, and recreated with uv, else with
`py -<version> -m venv`. Any other venv that does not start, or is older than
3.12, and any foreign venv inside a git checkout is replaced only with
-Recreate. The script ends with `python -m evi doctor --dev --strict`, and
with -Test then runs the tests where the check ran (a failed check runs
none). git safe.directory and line-ending problems are reported with their
fix command, never applied.
Keep this file ASCII-only: Windows PowerShell 5.1 reads scripts as cp1252.
#>
[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$Venv = $env:EVI_DEV_VENV,
    [string]$Python = '3.13',
    [string]$Extras = 'dev,web,mcp,scheduler,downloads,web-tools,index,e2e',
    [switch]$Recreate,
    [switch]$Reinstall,
    [switch]$Playwright,
    [string]$Worktree = '',
    [string]$Base = 'HEAD',
    [string]$Repo = '',
    [switch]$DryRun,
    [switch]$Test,
    # With -Test: pytest's arguments (whatever no option above takes).
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$TestArgs = @()
)
$ErrorActionPreference = 'Stop'

function Say([string]$Msg) { Write-Host "dev-bootstrap: $Msg" }
function Fail([string]$Msg) {
    [Console]::Error.WriteLine("dev-bootstrap: $Msg")
    exit 2
}
function Get-FullPath([string]$Path) {
    $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
}

# Arguments no option takes are pytest's, and only with -Test: without it they
# are a typo, never something to run or ignore.
$TestArgs = @($TestArgs | Where-Object { $null -ne $_ })
if ($TestArgs.Count -gt 0 -and -not $Test) {
    Fail "unknown argument(s): $($TestArgs -join ' ') (see Get-Help; pytest's arguments go after -Test)"
}

# Run a native command and capture stdout+stderr as text. Windows PowerShell 5.1
# turns redirected native stderr into a terminating error under
# ErrorActionPreference=Stop, so relax it for the call.
function Invoke-Quiet([string]$Exe, [string[]]$Arguments) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $Exe @Arguments 2>&1 | ForEach-Object { "$_" }
        return [pscustomobject]@{ Code = $LASTEXITCODE; Output = (@($out) -join "`n") }
    } catch {
        return [pscustomobject]@{ Code = 1; Output = "$_" }
    } finally {
        $ErrorActionPreference = $old
    }
}

# Echo a step, then run it unless -DryRun.
function Invoke-Step([string]$Exe, [string[]]$Arguments) {
    Write-Host ('+ ' + $Exe + ' ' + ($Arguments -join ' '))
    if ($DryRun) { return }
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { Fail "$Exe exited $LASTEXITCODE" }
}

# The given command starts and is Python 3.12+ (checked by its output).
function Test-Python312([string]$Exe, [string[]]$Prefix) {
    if (-not (Get-Command $Exe -ErrorAction SilentlyContinue)) { return $false }
    # No double quotes in the code: Windows PowerShell 5.1 does not escape them
    # for native commands, so python would receive mangled source.
    $probe = 'import sys; print(''py-ok'' if sys.version_info >= (3, 12) else ''py-old'')'
    $r = Invoke-Quiet $Exe (@($Prefix) + @('-c', $probe))
    return ($r.Code -eq 0 -and $r.Output.Trim() -eq 'py-ok')
}

# The git checkout (a directory holding .git) that holds $Path, or $null. It
# walks the path itself instead of comparing it with $Repo as a string, which a
# different spelling (8.3 name, mapped drive vs UNC) slips past.
function Get-Checkout([string]$Path) {
    $d = $Path
    while ($d) {
        if (Test-Path -LiteralPath (Join-Path $d '.git')) { return $d }
        $parent = Split-Path -Parent $d
        if (-not $parent -or $parent -eq $d) { break }
        $d = $parent
    }
    return $null
}

if (-not $Repo) { $Repo = Split-Path -Parent $PSScriptRoot }
$Repo = Get-FullPath $Repo
$script:SafeDirFix = ''

# --- git safe.directory ------------------------------------------------------
# git refuses a checkout owned by another account (a network share) with
# "dubious ownership". Print git's own fix and trust the path for this run only.
function Test-GitTrust([string]$Dir) {
    $oldLang = $env:LC_ALL
    $env:LC_ALL = 'C'
    try {
        $r = Invoke-Quiet 'git' @('-C', $Dir, 'rev-parse', '--show-toplevel')
    } finally {
        $env:LC_ALL = $oldLang
    }
    if ($r.Code -eq 0) { return }
    if ($r.Output -notmatch 'dubious ownership') { Fail "git cannot read ${Dir}: $($r.Output)" }
    $fix = [regex]::Match($r.Output, 'git config --global --add safe\.directory .+').Value.Trim()
    Say "git does not trust $Dir (owned by another account; usual on a network share)."
    Say "fix it once with:  $fix"
    $value = $fix.Substring('git config --global --add safe.directory '.Length).Trim().Trim("'")
    $n = 0
    if ($env:GIT_CONFIG_COUNT) { $n = [int]$env:GIT_CONFIG_COUNT }
    Set-Item -Path "env:GIT_CONFIG_KEY_$n" -Value 'safe.directory'
    Set-Item -Path "env:GIT_CONFIG_VALUE_$n" -Value $value
    $env:GIT_CONFIG_COUNT = [string]($n + 1)
    if ($script:SafeDirFix) { $script:SafeDirFix += '; ' }
    $script:SafeDirFix += $fix
}
Test-GitTrust $Repo

# --- the venv ----------------------------------------------------------------
if (-not $Venv) { $Venv = Join-Path $HOME ('.venvs\evi-' + ($Python -replace '\.', '')) }
$Venv = Get-FullPath $Venv
$Py = Join-Path $Venv 'Scripts\python.exe'
$HaveUv = [bool](Get-Command uv -ErrorAction SilentlyContinue)

$Create = $false
if (Test-Path -LiteralPath $Venv) {
    # Windows PowerShell 5.1's Remove-Item -Recurse follows a junction into its
    # target, so a linked venv is never replaced: name the real directory.
    $item = Get-Item -LiteralPath $Venv -Force
    if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        Fail "$Venv is a link (junction or symlink); refusing to touch it: pass -Venv the directory it points to"
    }
    if (Test-Python312 $Py @()) {
        Say "using venv $Venv"
    } else {
        $cfg = Join-Path $Venv 'pyvenv.cfg'
        if (-not (Test-Path -LiteralPath $cfg)) { Fail "$Venv exists but is not a venv (no pyvenv.cfg); refusing to touch it" }
        $homeLine = Get-Content -LiteralPath $cfg | Where-Object { $_ -match '^home\s*=' } | Select-Object -First 1
        $baseHome = if ($homeLine) { ($homeLine -replace '^home\s*=\s*', '').Trim() } else { '' }
        # Replace it unasked only when it is plainly foreign: the interpreter it
        # was made from does not exist here. Anything else needs -Recreate.
        if ($baseHome -and (Test-Path -LiteralPath $baseHome -PathType Container)) {
            if (-not $Recreate) {
                Fail "venv $Venv does not start (or is older than 3.12) although its interpreter $baseHome exists: rerun with -Recreate to replace it"
            }
        } else {
            Say "venv $Venv is foreign: its base interpreter $baseHome does not exist on this machine"
        }
        $checkout = Get-Checkout $Venv
        if ($checkout -and -not $Recreate) {
            Fail "it is inside the git checkout $checkout and may be another machine's venv: rerun with -Recreate to replace it, or use -Venv outside the checkout"
        }
        if ($Recreate) {
            Write-Host "+ Remove-Item -Recurse -Force $Venv"
            if (-not $DryRun) { Remove-Item -LiteralPath $Venv -Recurse -Force }
        } else {
            # Moved aside, not deleted: "foreign" is a guess from this account's
            # view, and a venv whose interpreter this shell cannot see (another
            # machine, or an app's redirected AppData) may still work there.
            $aside = $Venv + '.foreign-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + $PID
            Write-Host "+ Move-Item $Venv $aside"
            if (-not $DryRun) {
                Move-Item -LiteralPath $Venv -Destination $aside
                Say "moved it aside to ${aside}: delete that once the new venv works, or move it back"
            }
        }
        $Create = $true
    }
} else {
    $Create = $true
}

if ($Create) {
    if ($HaveUv) {
        Invoke-Step 'uv' @('venv', '--python', $Python, $Venv)
    } else {
        # (Not $base: PowerShell names are case-insensitive, so that is -Base.)
        $basePy = $null
        foreach ($cand in @("py -$Python", "python$Python", 'python3', 'python')) {
            $parts = @($cand -split ' ')
            if (Test-Python312 $parts[0] @($parts | Select-Object -Skip 1)) { $basePy = $parts; break }
        }
        if (-not $basePy) { Fail "no Python 3.12+ found: install uv (https://docs.astral.sh/uv/) or Python $Python" }
        Invoke-Step $basePy[0] (@($basePy | Select-Object -Skip 1) + @('-m', 'venv', $Venv))
    }
}

if ($Create -or $Reinstall) {
    $spec = $Repo + '[' + $Extras + ']'
    if ($HaveUv) {
        Invoke-Step 'uv' @('pip', 'install', '--python', $Py, '-e', $spec, 'pytest-xdist')
    } else {
        Invoke-Step $Py @('-m', 'pip', 'install', '--upgrade', 'pip')
        Invoke-Step $Py @('-m', 'pip', 'install', '-e', $spec, 'pytest-xdist')
    }
}
if ($Playwright) { Invoke-Step $Py @('-m', 'playwright', 'install', 'chromium') }

# --- the worktree ------------------------------------------------------------
$Target = $Repo
if ($Worktree) {
    if ((Invoke-Quiet 'git' @('-C', $Repo, 'check-ref-format', '--branch', $Worktree)).Code -ne 0) {
        Fail "not a valid branch name: $Worktree"
    }
    $Target = Join-Path $Repo ('.worktrees\' + ($Worktree -replace '/', '__'))
    if (Test-Path -LiteralPath (Join-Path $Target '.git')) {
        # Reuse it only for the same branch: a/b and a__b share a directory,
        # and a worktree can have switched branches since it was made.
        Test-GitTrust $Target
        $have = (Invoke-Quiet 'git' @('-C', $Target, 'symbolic-ref', '--short', '-q', 'HEAD')).Output.Trim()
        if ($have -cne $Worktree) {
            if (-not $have) { $have = '(a detached HEAD)' }
            Fail "$Target is the worktree of branch $have, not ${Worktree}: pick another branch name, or remove that worktree"
        }
        Say "reusing worktree $Target"
    } elseif (Test-Path -LiteralPath $Target) {
        Fail "$Target exists but is not a worktree"
    } else {
        # Keep worktrees out of the main checkout's `git add -A`.
        if ((Invoke-Quiet 'git' @('-C', $Repo, 'check-ignore', '-q', '--no-index', '.worktrees/probe')).Code -ne 0) {
            $common = (Invoke-Quiet 'git' @('-C', $Repo, 'rev-parse', '--path-format=absolute', '--git-common-dir')).Output.Trim()
            $exclude = Join-Path $common 'info\exclude'
            Write-Host "+ append .worktrees/ to $exclude"
            if (-not $DryRun) {
                New-Item -ItemType Directory -Force -Path (Split-Path -Parent $exclude) | Out-Null
                [System.IO.File]::AppendAllText($exclude, "`n.worktrees/`n")
            }
        }
        if ((Invoke-Quiet 'git' @('-C', $Repo, 'show-ref', '--verify', '--quiet', "refs/heads/$Worktree")).Code -eq 0) {
            Invoke-Step 'git' @('-C', $Repo, 'worktree', 'add', $Target, $Worktree)
        } else {
            Invoke-Step 'git' @('-C', $Repo, 'worktree', 'add', '-b', $Worktree, $Target, $Base)
        }
        if (-not $DryRun) { Test-GitTrust $Target }
    }
}

# --- check -------------------------------------------------------------------
# pytest from the checkout's own folder: `python -m` puts that folder first on
# sys.path, so a worktree's tests import the worktree's evi, not the main
# checkout the venv's editable install points at.
$PytestArgs = @('-m', 'pytest', '-q', '-p', 'no:cacheprovider') + $TestArgs
if ($DryRun) {
    Write-Host "+ (cd $Target; $Py -m evi doctor --dev --repo $Target --venv $Venv --strict)"
    if ($Test) { Write-Host ("+ (cd $Target; $Py " + ($PytestArgs -join ' ') + ')') }
    Say 'dry run: nothing was changed'
    exit 0
}
Push-Location -LiteralPath $Target
try {
    & $Py -m evi doctor --dev --repo $Target --venv $Venv --strict
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($script:SafeDirFix) {
    Say "git trusted the checkout for this run only; make it permanent with: $($script:SafeDirFix)"
}
if ($code -eq 0) { Say "ready: $Py (work in $Target)" }
if ($Test) {
    if ($code -ne 0) {
        Say "the check failed (exit $code), so the tests were not run"
        exit $code
    }
    Write-Host ("+ (cd $Target; $Py " + ($PytestArgs -join ' ') + ')')
    Push-Location -LiteralPath $Target
    try {
        & $Py @PytestArgs
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($code -eq 0) { Say "tests passed in $Target" } else { Say "tests failed in $Target (pytest exit $code)" }
}
exit $code
