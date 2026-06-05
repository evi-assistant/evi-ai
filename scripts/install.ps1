# Evi install script — Windows PowerShell
#
# Usage:
#   .\scripts\install.ps1                  # core install
#   .\scripts\install.ps1 -All             # all optional extras
#   .\scripts\install.ps1 -Extras "web,mcp,scheduler"
#
# Prereqs:
#   - Python 3.11+ (check with `py --list`)
#   - Git 2.17+ for `evi worktree`
#
# What it does:
#   1. Creates .venv next to this repo if missing
#   2. Installs evi in editable mode with the requested extras
#   3. Runs the test suite as a smoke check

[CmdletBinding()]
param(
    [string]$Extras = "dev,web,mcp,scheduler",
    [switch]$All
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot

if ($All) {
    $Extras = "dev,web,mcp,scheduler,downloads,web-tools,stt,computer"
}

Write-Host "==> Evi install" -ForegroundColor Cyan
Write-Host "    repo:    $repoRoot"
Write-Host "    extras:  $Extras"

# Pick a Python — prefer 3.12, fall back to 3.11.
$python = $null
foreach ($v in @("3.12", "3.11")) {
    try {
        & py -$v --version | Out-Null
        if ($LASTEXITCODE -eq 0) { $python = "py -$v"; break }
    } catch {}
}
if (-not $python) {
    Write-Error "No suitable Python found. Install 3.11+ from python.org or the Microsoft Store."
    exit 1
}
Write-Host "    python:  $python"

# Create venv if missing.
$venvPath = Join-Path $repoRoot ".venv"
if (-not (Test-Path $venvPath)) {
    Write-Host "==> Creating venv at $venvPath" -ForegroundColor Cyan
    Invoke-Expression "$python -m venv `"$venvPath`""
}

$venvPython = Join-Path $venvPath "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Error "venv broken — no python.exe under .venv\Scripts\"
    exit 1
}

Write-Host "==> Upgrading pip" -ForegroundColor Cyan
& $venvPython -m pip install --quiet --upgrade pip

Write-Host "==> Installing evi with extras [$Extras]" -ForegroundColor Cyan
& $venvPython -m pip install --quiet -e ".[$Extras]"

Write-Host "==> Smoke test" -ForegroundColor Cyan
& $venvPython -m pytest -q --timeout=15

Write-Host ""
Write-Host "==> Done." -ForegroundColor Green
Write-Host "Activate the venv with:"
Write-Host "    .\.venv\Scripts\Activate.ps1"
Write-Host "Then try:"
Write-Host "    evi models recommend"
Write-Host "    evi chat"
