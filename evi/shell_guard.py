"""Curated destructive-shell-command guard.

A default-on, deterministic second gate over shell-executing tools: a small,
curated set of high-precision patterns for genuinely destructive/irreversible
commands (``rm -rf ~``, ``git reset --hard``, disk formats, force-pushes,
credential exfil, IaC mass-teardown, ``GIT_*=`` / ``LD_PRELOAD=`` environment
that silently changes what a command runs, …) across bash/zsh, PowerShell, and
cmd — since eVi runs on Windows as well as macOS/Linux.

This is *not* a sandbox — a determined operator can obfuscate around it
(``bash -c``/``python -c``/base64/dynamic strings). It's a safety net that stops
the overwhelmingly common destructive mistakes from being run **silently**. The
agent layer (`evi/llm/agent.py`) consults `destructive_hit` for every
shell-executing tool (``run_command``, ``monitor``) and forces the call through
a confirmation prompt when a UI exists, or denies it outright when there's none
(headless / scheduler). NOTE: ``run_python`` executes arbitrary Python, a
different (broader) threat surface a shell-command regex can't meaningfully
secure — it is out of scope here and gated by its own tool toggle + sandbox.

Design (mirrors Claude Code's "auto mode" soft-deny, minus the LLM classifier):
- Precision over recall: every `prompt`-tier rule lets the common benign form
  through (``git reset --soft``, ``rm -rf ./build``, ``rm -rf /home/u/proj``,
  ``chmod 644``, ``terraform plan`` …). A guard that false-positives gets
  disabled by users within a day.
- Patterns anchor on the *dangerous flag combination*, not the bare binary, and
  consume an optional quote before a target (``rm -rf "$HOME"`` is caught).
- `[^;|&\n]*` bounded wildcards keep a match from spanning a command separator
  into an unrelated command's argument. A *pure* ``echo``/``printf`` that merely
  prints a dangerous-looking string is not treated as a command.
- Severity ``block`` = catastrophic/near-certainly-malicious; ``prompt`` =
  destructive-but-often-intended. Both resolve to "require explicit
  confirmation" at the agent layer, but only a ``prompt`` hit can be cleared
  by a specific ``[auto] rules`` allow rule; a ``block`` hit never is.

Config (``[auto]`` in config.toml): ``block_destructive`` (default true),
``destructive_allow`` (fnmatch globs matched against each command segment of
the line, see `evi.shellsplit`; a hit is exempt only when every segment it
touches matches one; a glob whose command word holds a wildcard is rejected,
see `rejected_allow_globs`), and ``destructive_disable_rules`` (builtin rule
ids to silence).

Director rules (`director_hit`, `director_code_hit` for ``run_python``): a
line that, as code, names the director's attestation key, sets or clears
``EVI_DIRECTOR`` / ``EVI_AGENT``, imports or patches `evi.attest`, or runs
director tooling (``evi task verify|reject``, ``evi attest ...``), itself or
through a program it hands the command to, is the director's, never an
agent's (`evi.attest`); a mention that is only data (a search pattern, a
commit message, an ``echo``) is not. `destructive_hit` reports such a hit
first, and no ``destructive_allow`` glob or ``destructive_disable_rules``
entry clears it; `evi.permissions.decide` denies the call in every mode.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Sequence

from evi import shellsplit
from evi.attest import DIRECTOR_DIRNAME, KEY_FILENAME, KEYRING_SERVICE


@dataclass(frozen=True)
class DestructiveRule:
    id: str
    severity: str  # "block" | "prompt"
    reason: str
    pattern: re.Pattern[str]


# --- environment assignments that change what a command loads or runs ---
# A command position: the line start or a separator / opening bracket / `$(`,
# then up to two of sudo / env / export / set / then ... (with their options)
# and up to eight other assignments (`LANG=C LD_PRELOAD=x ls`). Every run of
# whitespace is followed by a token that can't start with whitespace, so a long
# blank run can't drive backtracking.
_CMD_POS = (
    r'''(?:^|[;&|({`\n]|\$\()\s*'''
    r'''(?:(?:sudo|doas|env|export|set|declare|typeset|local|readonly|nohup|command|exec'''
    r'''|then|do|else)\s+(?:-[-\w]+\s+)*){0,2}'''
)
_OTHER_ASSIGNS = r'''(?:[A-Za-z_]\w*=(?:"[^"\n]*"|'[^'\n]*'|[^\s;|&"'])*\s+){0,8}'''
# GIT_* (GIT_EXTERNAL_DIFF, GIT_SSH_COMMAND, GIT_DIR, GIT_CONFIG_COUNT/KEY/VALUE
# — any config, `alias.*` and `core.fsmonitor` included), minus the data-only
# ones (identity, dates, prompt / lock / autoedit switches, trace levels) and
# the no-op editor / pager agents use (`GIT_EDITOR=true`, `GIT_PAGER=cat`).
_GIT_ENV_NAME = (
    r'''GIT_(?!(?:(?:AUTHOR|COMMITTER)_(?:NAME|EMAIL|DATE)|TERMINAL_PROMPT|OPTIONAL_LOCKS'''
    r'''|MERGE_AUTOEDIT)\b|(?:(?:SEQUENCE_)?EDITOR\s*=\s*["']?(?:true|:)|PAGER\s*=\s*["']?'''
    r'''(?:cat)?|TRACE\w*\s*=\s*["']?(?:[0-2]|true|false))["']?(?=[\s;&|)}]|$))\w+'''
)
# The dynamic loader's injection / search-path variables (Linux, macOS).
_LOADER_ENV_NAME = r'''(?:LD_(?:PRELOAD|LIBRARY_PATH|AUDIT)|DYLD_\w+)\b'''


def _env_assign(name: str) -> str:
    """Regex for an assignment of the variable `name` matches that a command
    (or the rest of the session) then sees: bash/zsh `NAME=v cmd`,
    `export NAME=v`, `env NAME=v cmd`; cmd `set NAME=v`, `set "NAME=v"`,
    `setx NAME v`; PowerShell `$env:NAME = v`, `Set-Item env:NAME v`,
    `[Environment]::SetEnvironmentVariable('NAME', v)`."""
    return (
        rf'''{_CMD_POS}{_OTHER_ASSIGNS}["']?{name}='''
        rf'''|{_CMD_POS}\$\{{?env:{name}\}}?\s*=(?!=)'''
        rf'''|\bsetx\s+["']?{name}|\b(?:Set-Item|New-Item|si|ni)\s+(?:-Path\s+)?["']?env:{name}'''
        rf'''|SetEnvironmentVariable\(\s*["']{name}["']'''
    )


# (id, severity, case_insensitive, reason, regex). Sourced from a cross-platform
# destructive-command study + an adversarial review; ids are stable (used by
# `destructive_disable_rules`).
_RAW: list[tuple[str, str, bool, str, str]] = [
    # --- git history / working-tree destruction ---
    ("git-reset-hard", "prompt", True,
     "git reset --hard discards all uncommitted changes irreversibly",
     r'''\bgit\s+reset\b[^;|&\n]*\s--hard\b'''),
    ("git-checkout-discard", "prompt", True,
     "git checkout -- <path> discards uncommitted working-tree edits",
     r'''\bgit\s+checkout\s+(?:-[\w-]+\s+)*--\s+\S'''),
    ("git-restore-worktree", "prompt", True,
     "git restore overwrites working-tree files from the index/HEAD",
     r'''\bgit\s+restore\s+(?![^;|&\n]*--staged\b)\S'''),
    ("git-clean-force", "prompt", True,
     "git clean -f deletes untracked files/directories",
     r'''\bgit\s+clean\s+(?:[^;|&\n]*\s)?-[a-z]*f[a-z]*\b'''),
    ("git-worktree-remove-force", "prompt", True,
     "git worktree remove --force discards uncommitted work in that worktree",
     # Only the FORCED variant: plain `git worktree remove` already refuses when
     # the worktree is dirty, so matching it would false-positive on routine
     # cleanup. Bounded {0,200} so a long argv can't drive backtracking.
     r'''\bgit\s+worktree\s+remove\b[^;|&\n]{0,200}?(?<![\w-])(?:--force\b|-f+\b)'''),
    ("git-stash-drop", "prompt", True,
     "git stash drop/clear permanently deletes stashed work",
     r'''\bgit\s+stash\s+(?:drop|clear)\b'''),
    ("git-commit-amend", "prompt", True,
     "git commit --amend rewrites the last commit",
     r'''\bgit\s+commit\b[^;|&\n]*(?<!["'\w])--amend\b'''),
    ("git-force-push", "block", True,
     "git push --force (or a +refspec) overwrites remote history teammates depend on",
     r'''\bgit\s+push\b[^;|&\n]*(?<![\w-])(?:--force(?:-with-lease)?|-f)\b|\bgit\s+push\b[^;|&\n]*\s\+[\w./]+(?::[\w./-]+)?(?=\s|$)'''),
    ("git-branch-force-delete", "prompt", False,  # case-SENSITIVE: -D forces, -d is safe
     "git branch -D force-deletes a possibly-unmerged branch",
     r'''\bgit\s+branch\s+(?:-[a-zA-Z]*\s+)*-[a-zA-Z]*D\b'''),
    ("git-history-purge", "block", True,
     "expiring reflog / pruning now / deleting refs makes lost commits unrecoverable",
     r'''\bgit\s+(?:reflog\s+expire\b[^;|&\n]*--expire(?:[= ](?:now|all))|gc\b[^;|&\n]*--prune[= ]now|update-ref\s+-d)\b'''),

    # --- environment that changes what a command loads or runs ---
    # Invisible in the command itself: `GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=
    # alias.status GIT_CONFIG_VALUE_0='!rm -rf ~' git status` reads as a status,
    # `LD_PRELOAD=x.so ls` as an ls. Only an assignment counts — reading
    # `$GIT_DIR` / `$env:GIT_DIR`, `unset`, or `-e GIT_X=1` handed to another
    # program does not. See _CMD_POS / _GIT_ENV_NAME above for the forms.
    ("env-git-override", "prompt", True,
     "a GIT_* environment assignment can make git run another program or use another repository",
     _env_assign(_GIT_ENV_NAME)),
    ("env-loader-inject", "prompt", True,
     "LD_PRELOAD / LD_LIBRARY_PATH / DYLD_* make every program load a library of the caller's choosing",
     _env_assign(_LOADER_ENV_NAME)),

    # --- recursive/forced deletion (unix) ---
    # An optional quote is consumed before the target so `rm -rf "$HOME"` is
    # caught; /home and /root only match at home-root depth (so deleting a build
    # dir under /home/<user>/proj does NOT trip the guard).
    ("rm-rf-root-home", "block", True,
     "recursive force-delete of /, a system dir, or a home root",
     r'''\brm\b(?=[^;|&\n]*(?:-[a-z]*r|--recursive))(?=[^;|&\n]*(?:-[a-z]*f|--force))[^;|&\n]*\s(?:-[a-z-]+\s+)*["']?(?:/(?=[\s/*"'`)]|$)|/(?:etc|usr|var|bin|boot|lib(?:64)?|sbin|opt|sys|proc|dev)\b|/(?:home|root)(?:/[^/\s;|&"'*]+)?/?(?=[\s*"'`)]|$)|~(?=[\s/*"'`)]|$)|\$\{?HOME\}?|\$env:USERPROFILE)'''),
    ("rm-rf-unresolved-var", "block", True,
     "recursive force-delete whose target is an unexpanded variable (path unknown)",
     r'''\brm\b(?=[^;|&\n]*(?:-[a-z]*r|--recursive))(?=[^;|&\n]*(?:-[a-z]*f|--force))[^;|&\n]*\s"?\$\{?\w+\}?"?/'''),
    ("no-preserve-root", "block", True,
     "--no-preserve-root removes the guard against deleting /",
     r'''--no-preserve-root\b'''),
    # NOTE: a bare `rm -rf <relative path>` (e.g. ./build, node_modules) is
    # deliberately NOT flagged — the catastrophic targets (/, ~, $HOME, system
    # dirs, home roots, unexpanded vars) are covered above; a catch-all here
    # would fire on everyday dev cleanup and get the whole guard switched off.
    ("find-delete", "prompt", True,
     "find … -delete / -exec rm mass-deletes matched files",
     r'''\bfind\s+[^;|&\n]*(?:\s-delete\b|-exec\s+rm\b)'''),
    ("shred", "prompt", True,
     "shred irreversibly overwrites/erases files",
     r'''\bshred\b(?:\s+-\S+)*\s+(?:/dev/|/(?:etc|usr|var|home|boot)\b)|\bshred\b[^;|&\n]*-[a-z]*u'''),

    # --- recursive/forced deletion (windows) ---
    # `rm` is a PowerShell alias for Remove-Item, so it's in the binary list;
    # the rule still requires -Recurse + -Force AND a dangerous target, so it
    # won't fire on unix `rm -rf ./build`. C:\Users matches only at the bare
    # root (a subdir like C:\Users\me\repo\node_modules is fine).
    ("ps-remove-system-path", "block", True,
     "Remove-Item -Recurse -Force targeting a drive root / Windows / Users / $env path",
     r'''\b(?:Remove-Item|ri|rm|del|rd|rmdir)\b(?=[^;|&\n]*\s-r(?:ec(?:urse)?)?\b)(?=[^;|&\n]*\s-fo?(?:rce)?\b)[^;|&\n]*\s"?(?:[A-Za-z]:\\?(?=["\s*]|$)|\$env:USERPROFILE(?=["\s]|$)|\$env:SystemRoot|\$HOME(?=["\s]|$)|C:\\Windows|C:\\Users(?=["\s]|$))'''),
    ("cmd-rd-s", "prompt", True,
     "rd /s recursively removes a directory tree",
     r'''\b(?:rd|rmdir)\s+[^;|&\n]*/s\b'''),
    ("cmd-del-fsq", "prompt", True,
     "del /s /q /f force-deletes files recursively/quietly",
     r'''\b(?:del|erase)\s+[^;|&\n]*(?:/s\b|/q\b[^;|&\n]*/f\b|/f\b[^;|&\n]*/q\b)'''),
    ("ps-clear-content", "prompt", True,
     "Clear-Content truncates a file to zero bytes",
     r'''\bClear-Content\b(?=[^;|&\n]*(?:-Force|-Path|\s\S))[^;|&\n]*(?:[A-Za-z]:\\|\$env:|/etc/|\.(?:conf|config|env|json|ya?ml|db|sqlite))'''),

    # --- disk / format / partition / recovery sabotage ---
    ("dd-to-device", "block", True,
     "dd writing to a raw block device destroys the disk",
     r'''\bdd\b[^;|&\n]*\bof=["']?/dev/(?!null\b|zero\b|random\b|urandom\b|stdout\b)(?:sd|nvme|disk|hd|mmcblk|vd|xvd|loop)'''),
    ("mkfs-wipe", "block", True,
     "mkfs / wipefs / blkdiscard / sgdisk --zap destroys a filesystem or partition table",
     r'''\bmkfs(?:\.[a-z0-9]+)?\b|\bwipefs\b|\bblkdiscard\b|\bsgdisk\b[^;|&\n]*--zap(?:-all)?\b'''),
    ("cmd-format", "block", True,
     "format X: erases a drive",
     r'''\bformat\s+[A-Za-z]:(?=[\s"'/]|$)'''),
    ("ps-disk-ops", "block", True,
     "Format-Volume / Clear-Disk / Remove-Partition destroys disk data",
     r'''\b(?:Format-Volume|Clear-Disk|Initialize-Disk|Remove-Partition|Reset-PhysicalDisk)\b'''),
    ("diskpart", "prompt", True,
     "diskpart can clean/repartition disks",
     r'''\bdiskpart\b'''),
    ("recovery-sabotage", "block", True,
     "deleting shadow copies / backups / disabling recovery (ransomware TTP)",
     r'''\bvssadmin(?:\.exe)?\s+delete\s+shadows\b|\bwmic\s+shadowcopy\s+delete\b|Win32_Shadowcopy[^;|&\n]*(?:Remove-WmiObject|Delete\(\))|\bwbadmin\s+delete\s+(?:catalog|systemstatebackup|backup)\b|\bbcdedit\b[^;|&\n]*recoveryenabled\s+no\b'''),
    ("redirect-to-device", "block", True,
     "redirecting output onto a raw block device corrupts the disk",
     r'''>\s*["']?/dev/(?:sd|nvme|hd|disk|mmcblk|vd)\w*'''),

    # --- fork bombs / resource exhaustion ---
    # NOTE: the function-name group is length-bounded on purpose — an unbounded
    # `(\w+)` here backtracks catastrophically (seconds) on a long word run,
    # which would stall the permission check on a large command string.
    ("fork-bomb", "block", True,
     "fork bomb — exhausts process table and hangs the machine",
     r''':\(\)\s*\{\s*:\s*\|\s*:?\s*&?\s*\}\s*;\s*:|(\w{1,32})\s*\(\)\s*\{\s*\1\s*\|\s*\1\s*&\s*\}\s*;\s*\1'''),
    ("while-true-fork", "prompt", True,
     "unbounded background-spawning loop",
     r'''\bwhile\s*(?:\(\s*(?:1|true)\s*\)|true|:)[^;|&\n]*;?\s*do[^;|&\n]*(?:&\s*(?:done|$)|fork)'''),
    ("ps-job-bomb", "prompt", True,
     "unbounded PowerShell job/process spawn loop",
     r'''\bStart-Job\b[^;|&\n]*\bwhile\b[^;|&\n]*\bStart-Job\b|for\s*\(;;\)[^;|&\n]*Start-Process'''),

    # --- pipe-to-shell / remote code execution ---
    ("curl-pipe-sh", "block", True,
     "downloading and piping straight into a shell runs unaudited remote code",
     r'''\b(?:curl|wget|fetch)\b[^;\n]*\|\s*(?:sudo\s+)?(?:sh|bash|zsh|ksh|dash|fish|python[0-9.]*|perl|ruby|node|php)\b'''),
    ("ps-iwr-iex", "block", True,
     "IWR/IRM piped to Invoke-Expression runs unaudited remote code",
     r'''(?:\b(?:iwr|irm|Invoke-WebRequest|Invoke-RestMethod|curl|wget)\b[^;\n]*\|\s*(?:iex|Invoke-Expression)\b)|(?:\b(?:iex|Invoke-Expression)\b\s*\(?\s*(?:iwr|irm|Invoke-WebRequest|Invoke-RestMethod|\(?New-Object\s+(?:System\.)?Net\.WebClient\)?\.Download(?:String|File)))'''),
    ("base64-pipe-sh", "block", True,
     "base64-decoding into a shell hides what is being executed",
     r'''\bbase64\s+(?:-d|--decode|-D)\b[^;\n]*\|\s*(?:sh|bash|zsh|python[0-9.]*|perl|node)\b|\becho\s+[A-Za-z0-9+/=]{40,}\s*\|\s*base64\s+-d[^;\n]*\|\s*(?:sh|bash|zsh|python[0-9.]*|perl|node)\b'''),

    # --- permission / ownership escalation ---
    ("chmod-777", "prompt", True,
     "chmod 777 makes files world-writable",
     r'''\bchmod\s+(?:-\S+\s+)*(?:0?777|a\+rwx|ugo\+rwx)\b'''),
    ("chmod-r-777-root", "block", True,
     "recursive chmod 777 on a system path",
     r'''\bchmod\s+-R\s+(?:0?777|a\+rwx)\s+/(?:\s|$|etc|usr|var|bin|home|root|boot|lib|opt)'''),
    ("chown-r-root", "block", True,
     "recursive chown on a system path",
     r'''\bchown\s+(?:-\S+\s+)*-R\b[^;|&\n]*\s/(?:\s|$|etc|usr|var|bin|home|root|boot|lib|opt|sbin)'''),
    ("icacls-takeown", "prompt", True,
     "icacls grant-Everyone / reset / takeown weakens Windows ACLs",
     r'''\bicacls\b[^;|&\n]*/grant\b[^;|&\n]*\b(?:Everyone|Users|BUILTIN\\Users|Authenticated Users)\b[^;|&\n]*:\(?(?:F|M|OI|CI)\)?|\bicacls\b[^;|&\n]*/reset\b[^;|&\n]*/t\b|\btakeown\s+[^;|&\n]*/f[^;|&\n]*/r\b'''),

    # --- credential / secret exfiltration ---
    ("secret-pipe-net", "block", True,
     "reading a secret/key and piping it to the network",
     r'''(?:\.ssh/id_(?:rsa|ed25519|ecdsa|dsa)(?!\.pub)\b|\.aws/credentials\b|\.config/gcloud\b|\.kube/config\b|\.git-credentials\b|/etc/shadow\b|\.env(?:\.\w+)?\b|\.pem\b|secrets?\.(?:ya?ml|json|env)\b|\.netrc\b|\$\{?\w*(?:TOKEN|SECRET|API[_-]?KEY|PASSWORD|CREDENTIAL|PRIVATE[_-]?KEY)\w*\}?)[^;\n]*\|\s*(?:curl|wget|nc\b|ncat|netcat|ssh|scp|telnet|Invoke-WebRequest|Invoke-RestMethod|iwr|irm)\b'''),
    ("upload-secret", "block", True,
     "uploading a credentials/secret file over the network",
     r'''\b(?:curl|wget|Invoke-RestMethod|Invoke-WebRequest|iwr|irm)\b[^;\n]*(?:-d|--data(?:-binary|-raw)?|-F|--form|-T|--upload-file|-Body|-InFile)\b[^;\n]*(?:\$\(\s*cat\s+[^)]*(?:\.env|\.ssh|credentials|id_rsa|secret)|@?(?:\.env\b|.*\.ssh/|.*credentials\b|.*id_rsa\b|.*\.pem\b))'''),
    ("env-pipe-net", "block", True,
     "piping the whole environment (which may hold secrets) to the network",
     r'''\b(?:printenv|env|set)\b\s*\|\s*(?:curl|wget|nc\b|ncat|Invoke-RestMethod|iwr|irm)\b'''),
    ("read-private-key", "prompt", True,
     "reading a private key / shadow / credentials file",
     r'''\b(?:cat|type|Get-Content|gc|bat|less|more|head|tail)\b[^;|&\n]*(?:\.ssh/id_(?:rsa|ed25519|ecdsa|dsa)(?!\.pub)\b|/etc/shadow\b|\.aws/credentials\b|\.git-credentials\b|(?<![\w.])id_rsa\b(?!\.pub))'''),

    # --- IaC / cloud mass-teardown ---
    ("iac-destroy", "block", True,
     "terraform/pulumi/cdk destroy tears down provisioned infrastructure",
     r'''\b(?:terraform|terragrunt|tofu|opentofu|pulumi|cdk|cdktf)\s+(?:[^;|&\n]*\s)?destroy\b'''),
    ("iac-state-rm", "prompt", True,
     "removing IaC state / workspaces / applying a destroy plan",
     r'''\bterraform\s+(?:state\s+rm|workspace\s+delete|apply\b[^;|&\n]*\bdestroy)\b|\bpulumi\s+stack\s+rm\b'''),
    ("kubectl-delete-all", "block", True,
     "kubectl delete --all / --all-namespaces / a namespace wipes cluster resources",
     r'''\bkubectl\s+delete\b[^;|&\n]*(?:--all\b|--all-namespaces\b|(?<!-)-A\b|(?<!-)\bnamespaces?\b|\bns\s+\S)'''),
    ("kubectl-drain-helm", "prompt", True,
     "kubectl drain / helm uninstall removes running workloads",
     r'''\bkubectl\s+drain\b|\bhelm\s+(?:uninstall|delete|del)\b'''),
    ("cloud-storage-delete", "block", True,
     "recursive/forced cloud object-storage or bucket deletion",
     r'''\baws\s+s3\s+(?:rb\b[^;|&\n]*--force|rm\b[^;|&\n]*--recursive)\b|\baws\s+s3api\s+delete-(?:bucket|objects)\b|\bgsutil\s+(?:rm|rb)\b[^;|&\n]*-r\b|\bgcloud\s+storage\s+rm\b[^;|&\n]*(?:-r|--recursive)\b|\baz\s+storage\s+(?:container\s+delete|blob\s+delete-batch)\b'''),
    ("docker-prune", "prompt", True,
     "docker system prune / volume prune can delete data and volumes",
     r'''\bdocker\s+system\s+prune\b[^;|&\n]*(?:-a|--all|--volumes)\b|\bdocker\s+volume\s+prune\b|\bdocker\s+(?:rm|rmi)\s+(?:-\S*\s+)*-\S*f\b[^;|&\n]*(?:\$\(docker|--all|-aq)'''),

    # --- package-manager global / mass wipe ---
    ("npm-global-remove", "prompt", True,
     "global package removal",
     r'''\bnpm\s+(?:uninstall|rm|remove|un)\s+(?:-g|--global)\b|\byarn\s+global\s+remove\b|\bpnpm\s+(?:remove|rm)\s+(?:-g|--global)\b'''),
    ("pip-mass-uninstall", "prompt", True,
     "pip uninstall from a requirements file / freeze|xargs uninstall removes many packages",
     r'''\bpip[0-9.]*\s+uninstall\b[^;\n]*(?:-y\b[^;\n]*(?:-r\b|--requirement)|--requirement\b|\s-r\b)|\bpip[0-9.]*\s+freeze\b[^;\n]*\|\s*(?:xargs\s+)?pip[0-9.]*\s+uninstall'''),
    ("pkg-remove-critical", "block", True,
     "removing kernel/libc/systemd/coreutils can brick the OS",
     r'''\b(?:apt|apt-get|aptitude)\s+(?:purge|remove|autoremove)\b[^;|&\n]*\b(?:linux-image\S*|linux-generic|linux-headers\S*|kernel\S*|(?:libc6|glibc|systemd|udev|coreutils|bash|dpkg|apt|rpm)(?![-\w]))|\b(?:yum|dnf)\s+(?:remove|erase)\b[^;|&\n]*\b(?:kernel\S*|(?:glibc|systemd|coreutils|bash|rpm)(?![-\w]))'''),
    # NOTE: a generic `apt/yum remove <app-package>` is intentionally NOT
    # flagged (it's common and reversible); only removing an OS-critical package
    # (above) blocks.

    # --- firewall / security-control disable ---
    ("unix-security-disable", "block", True,
     "disabling the firewall / SELinux / AppArmor / auditd",
     r'''\bufw\s+disable\b|\bsystemctl\s+(?:stop|disable|mask)\s+(?:firewalld|ufw|apparmor|auditd)\b|\biptables\s+-F\b|\bnft\s+flush\s+ruleset\b|\bsetenforce\s+0\b|\bsystemctl\s+stop\s+nftables\b'''),
    ("win-firewall-disable", "block", True,
     "turning off the Windows firewall",
     r'''\bnetsh\s+advfirewall\s+set\s+\w+\s+state\s+off\b|\bSet-NetFirewallProfile\b[^;|&\n]*-Enabled\s+(?:False|\$false)\b|\bnetsh\s+firewall\s+set\s+opmode\s+disable\b'''),
    ("defender-disable", "block", True,
     "disabling Microsoft Defender real-time protection / stopping its service",
     r'''\bSet-MpPreference\b[^;|&\n]*-Disable\w*(?:Monitoring|Protection|Scanning|Access)\b[^;|&\n]*(?:\$true|True|1)\b|\b(?:Stop-Service|sc(?:\.exe)?\s+stop|sc(?:\.exe)?\s+config)\b[^;|&\n]*\b(?:WinDefend|MpsSvc|Sense|wscsvc)\b|\bDisable-WindowsOptionalFeature\b[^;|&\n]*Defender'''),
    ("log-tamper", "block", True,
     "clearing event logs / audit policy / USN journal (evidence tampering)",
     r'''\bwevtutil\s+cl\b|\bClear-EventLog\b|\bauditpol\s+/clear\b|\bRemove-EventLog\b|\bfsutil\s+usn\s+deletejournal\b'''),

    # --- system power / transcript tampering ---
    # bare verbs (halt/reboot/…) only match at a command position, so `make halt`
    # / `npm run shutdown-server` don't trip.
    ("system-power", "prompt", True,
     "shutting down / rebooting the machine",
     r'''(?:^|[;&|]\s*|\bsudo\s+|\bdoas\s+)(?:shutdown(?![^;|&\n]*(?:/a\b|-c\b))|reboot|poweroff|halt|init\s+0|telinit\s+0)\b|\b(?:Stop-Computer|Restart-Computer)\b'''),
    ("transcript-tamper", "block", True,
     "writing to or deleting eVi/Claude session transcript files",
     r'''(?:>|>>|Out-File|Set-Content|Add-Content|tee\b)[^;|&\n]*(?:\.(?:claude|evi)[/\\](?:projects|transcripts|sessions)[/\\]|\.(?:claude|evi)[/\\][^;\n]*\.jsonl)|\brm\b[^;|&\n]*\.(?:claude|evi)[/\\](?:projects|transcripts|sessions)[/\\]'''),
]


def _compile() -> list[DestructiveRule]:
    rules: list[DestructiveRule] = []
    for rid, severity, ci, reason, pat in _RAW:
        flags = re.IGNORECASE if ci else 0
        rules.append(DestructiveRule(rid, severity, reason, re.compile(pat, flags)))
    return rules


DEFAULT_RULES: list[DestructiveRule] = _compile()
RULE_IDS: frozenset[str] = frozenset(r.id for r in DEFAULT_RULES)
# Match order: all `block` rules before `prompt` rules, so when a command matches
# both a specific catastrophic rule and a broader one, the most severe
# classification wins (e.g. `chmod -R 777 /etc` → chmod-r-777-root, not chmod-777).
_MATCH_ORDER: list[DestructiveRule] = sorted(
    DEFAULT_RULES, key=lambda r: 0 if r.severity == "block" else 1
)

# --- director-only ground (evi.attest) ----------------------------------------
#
# Marking a task verified or rejected is the director's, never eVi's
# (docs/roadmap.md, Phase 1). These rules are not destructive-command rules:
# they are kept out of DEFAULT_RULES / RULE_IDS (so naming one in
# `destructive_disable_rules` disables nothing, and configlint says so), and
# `destructive_hit` reports them first whatever the config says. Like the rest
# of this module they raise the bar rather than wall it off: code an agent
# writes and then runs can still do what evi.attest does (see its Limits).
#
# A rule trips on code: a command segment's words, what a program is handed to
# run (`schtasks /tr "..."`, `ssh host ...`, a git `!` alias), a redirect
# target, a heredoc or pipe feeding an interpreter, or a line the splitter
# can't follow. A mention that is only data is left alone: a search pattern
# (`grep "director.key"`), a commit message, an `echo`'s words, a heredoc body
# nothing runs (`_code_and_data`).

_DIRECTOR_VARS = r'''EVI_(?:DIRECTOR|AGENT)\b'''
_DIRECTOR_RAW: list[tuple[str, str]] = [
    ("director-env",
     # Assigning EVI_DIRECTOR / EVI_AGENT (any form `_env_assign` knows), or
     # clearing one: `unset`, `env -u`, PowerShell `Remove-Item env:...`.
     _env_assign(_DIRECTOR_VARS)
     + r'''|\bunset\s+(?:-[a-z]+\s+)*(?:[A-Za-z_]\w*\s+){0,8}["']?''' + _DIRECTOR_VARS
     + r'''|\benv\b[^;|&\n]{0,200}?\s(?:-u\s*|--unset[=\s]\s*)["']?''' + _DIRECTOR_VARS
     + r'''|\b(?:Remove-Item|Clear-Item|ri|rm|del|erase|cli)\s+(?:-\w+\s+){0,4}["']?env:[\\/]?'''
     + _DIRECTOR_VARS),
    ("director-credential",
     # The keyring entry, the key file, or its folder under eVi's home.
     r'''(?<![\w-])''' + re.escape(KEYRING_SERVICE) + r'''(?![\w-])'''
     + r'''|(?:\.evi|EVI_HOME\}?%?)[/\\]+''' + re.escape(DIRECTOR_DIRNAME) + r'''(?![\w.-])'''
     + r'''|(?<![\w-])''' + re.escape(KEY_FILENAME) + r'''\b'''),
    ("director-internals",
     # evi.attest itself, or the internals that decide who may sign and read
     # the key, in code a line runs (`python -c "..."`, code fed to python):
     # what patching it to sign from an agent's process takes.
     r'''\bevi\.attest\b|\bfrom\s+evi\s+import\b[^;\n]{0,200}?\battest\b'''
     r'''|\b(?:_process_chain|_agent_parent|agent_parent|agent_context|_key_or_reason'''
     r'''|_load_key|_file_get|_keyring_get)\b'''),
    ("director-tooling",
     # Matched per command segment (`is_director_argv`) and over the code
     # words a program is handed (`_spread`); as plain text only for a line
     # the splitter can't follow (`bash -c "..."`, `cmd /c ...`).
     r'''(?<![\w.-])evi(?:\.exe)?["']?(?:\s+-[\w-]+(?:=\S*)?(?:\s+[^\s;|&"'-][^\s;|&"']*)?){0,6}'''
     r'''\s+(?:task(?:\s+-[\w-]+){0,6}\s+(?:verify|reject)|attest)(?![\w-])'''),
]
_DIRECTOR_REASONS = {
    "director-env": "EVI_DIRECTOR / EVI_AGENT are not an agent's to set or clear: only the "
                    "director's own shell runs director tooling",
    "director-credential": "the director's attestation key (the evi-director keyring entry, "
                           "<eVi home>/director/) is out of an agent's reach",
    "director-internals": "evi.attest decides who may sign and holds the director key: an "
                          "agent's code doesn't import or patch it",
    "director-tooling": "`evi task verify|reject` and `evi attest` are director tooling: eVi "
                        "never marks a task verified or rejected itself",
}
DIRECTOR_RULES: list[DestructiveRule] = [
    DestructiveRule(rid, "block", _DIRECTOR_REASONS[rid], re.compile(pat, re.IGNORECASE))
    for rid, pat in _DIRECTOR_RAW
]
DIRECTOR_RULE_IDS: frozenset[str] = frozenset(r.id for r in DIRECTOR_RULES)
_DIRECTOR_BY_ID = {r.id: r for r in DIRECTOR_RULES}
_DIRECTOR_TEXT_RULES = [r for r in DIRECTOR_RULES if r.id != "director-tooling"]
_DIRECTOR_TOOLING = _DIRECTOR_BY_ID["director-tooling"]

# Running eVi: its console script (or `evi-script.py`), `python -m evi`, and
# the launchers that run one of those (`uv run evi`, `pipx run evi` ...).
_EVI_PROGRAMS = frozenset({"evi", "evi-script.py"})
_EVI_MODULES = frozenset({"evi", "evi.__main__", "evi.apps.cli", "evi.apps.cli.main"})
_PYTHONS = re.compile(r"(?:pythonw?|pypy|pyw?)(?:\d+(?:\.\d+)*)?\Z")
_PY_VALUE_OPTIONS = frozenset({"-W", "-X", "-Q"})
_LAUNCHERS = frozenset({
    "uv", "uvx", "pipx", "poetry", "pdm", "hatch", "rye", "pixi", "pipenv", "conda",
    "mamba", "micromamba",
})
# eVi's global options that take a value (`evi --profile work task verify`).
_EVI_VALUE_OPTIONS = frozenset({"--profile", "-p"})
# Director tooling: these subcommands of these groups, and these commands.
_DIRECTOR_GROUPS = {"task": frozenset({"verify", "reject"})}
_DIRECTOR_COMMANDS = frozenset({"attest"})
_QUOTING = re.compile(r'''["'`^\\]''')
# A word whose value isn't known until the shell runs it: an expansion, or
# xargs' `{}` placeholder.
_DYNAMIC = re.compile(r"[$%`]|\{\}")
# Interpreters that read their program from stdin when given no script.
_INTERPRETERS = re.compile(
    r"(?:pythonw?|pypy|pyw?|node|nodejs|deno|bun|ruby|perl|php|lua|rscript|julia|osascript)"
    r"(?:\d+(?:\.\d+)*)?\Z"
)
# cmd and PowerShell refuse a line holding a bare `<<` outright (cmd: "<< was
# unexpected", and it runs nothing past the first newline anyway; PowerShell
# reserves `<` and parses the whole line before running any of it), so they
# never run a bash heredoc's body.
_REFUSED_BY_SHELL = "redirect '<' without a target"

# Where a mention is data (`_code_and_data`): what these print...
_PRINTERS = frozenset({
    "echo", "printf", "write-output", "write-host", "write", "write-information",
})
# ...the pattern these search for...
_SEARCHERS = frozenset({
    "grep", "egrep", "fgrep", "rg", "ag", "ack", "findstr", "select-string", "sls",
})
_PATTERN_OPTIONS = frozenset({"-e", "--regexp", "-pattern"})
# (each one's short options that take no value: only past these is the first
# plain word known to be the pattern, not an option's value)
_SEARCH_FLAG_LETTERS = {
    "grep": "abcEFGHhIiLlnoPqRrsUuVvwxZz",
    "egrep": "abcEFGHhIiLlnoPqRrsUuVvwxZz",
    "fgrep": "abcEFGHhIiLlnoPqRrsUuVvwxZz",
    "rg": "abcFHhIiLlNnoPpqSsUuvwxz.",
    "ag": "acfHhiLlnoQRrSsUuvwz",
    "ack": "cfHhIiLlnoQRrsvwx",
}
_SEARCH_LONG_FLAGS = frozenset({
    "--recursive", "--line-number", "--ignore-case", "--files-with-matches",
    "--files-without-match", "--count", "--word-regexp", "--line-regexp", "--invert-match",
    "--fixed-strings", "--extended-regexp", "--perl-regexp", "--basic-regexp", "--no-messages",
    "--with-filename", "--no-filename", "--only-matching", "--quiet", "--silent", "--null",
    "--hidden", "--no-ignore", "--smart-case", "--case-sensitive", "--no-heading", "--heading",
    "--color", "--colour", "--no-line-number", "--vimgrep", "--json", "--follow", "--multiline",
    "--literal",
})
_SLS_SWITCHES = frozenset({
    "-simplematch", "-casesensitive", "-list", "-quiet", "-notmatch", "-allmatches", "-raw",
    "-noemphasis",
})
# ...and a git commit / tag message or a log search.
_GIT_VALUE_OPTIONS = frozenset({
    "-c", "-C", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env",
    "--exec-path",
})
_GIT_MESSAGE_COMMANDS = frozenset({
    "commit", "tag", "notes", "stash", "merge", "revert", "cherry-pick",
})
_GIT_LOG_COMMANDS = frozenset({"log", "show", "shortlog", "rev-list", "reflog", "whatchanged"})
_GIT_MESSAGE_CLUSTER = re.compile(r"-[A-Za-z]*m\Z")  # -m, -am, -sm: the next word
_SQUASH = re.compile(r'''[\s"'`]''')
_SPREAD = re.compile(r'''[,"'\[\](){}]''')


def evi_args(words: Sequence[str]) -> list[str] | None:
    """The arguments eVi gets when a command runs it, or None when it runs
    something else. `words` is a command segment's words or a process's
    command line: ``evi ...`` (any path, ``.exe``), ``python -m evi ...``,
    ``python path/to/evi ...``, ``uv run evi ...`` and other launchers, past
    wrappers such as ``sudo``, ``env``, ``start`` (`evi.shellsplit`)."""
    words = list(words)
    return _evi_args_at(words, shellsplit._command_index(words))


def _evi_args_at(words: list[str], i: int | None) -> list[str] | None:
    """`evi_args` for the command starting at word `i`."""
    for _ in range(8):  # launcher -> python -> script at most, a few times over
        if i is None or i >= len(words):
            return None
        base = shellsplit._base(words[i])
        if base in _EVI_PROGRAMS:
            return words[i + 1:]
        if _PYTHONS.match(base):
            j, nxt = i + 1, None
            while j < len(words):
                w = words[j]
                if w == "-m":
                    mod = words[j + 1] if j + 1 < len(words) else ""
                    return words[j + 2:] if mod in _EVI_MODULES else None
                if w.startswith("-m") and not w.startswith("--"):
                    return words[j + 1:] if w[2:] in _EVI_MODULES else None
                if w == "-" or w.startswith("-c"):
                    return None  # code on stdin / inline: not a command line
                if w.startswith("-"):
                    j += 2 if w in _PY_VALUE_OPTIONS else 1
                    continue
                nxt = j  # the script python runs
                break
            i = nxt
            continue
        if base in _LAUNCHERS:
            i = next((k for k in range(i + 1, len(words))
                      if shellsplit._base(words[k]) in _EVI_PROGRAMS
                      or _PYTHONS.match(shellsplit._base(words[k]))), None)
            continue
        return None
    return None


def _director_args(args: Sequence[str]) -> bool:
    k = 0
    while k < len(args) and args[k].startswith("-"):
        k += 2 if args[k] in _EVI_VALUE_OPTIONS else 1
    if k >= len(args):
        return False
    sub = args[k].lower()
    if sub in _DIRECTOR_COMMANDS:
        return True
    if sub not in _DIRECTOR_GROUPS:
        return False
    k += 1
    while k < len(args) and args[k].startswith("-"):
        k += 1
    return k < len(args) and args[k].lower() in _DIRECTOR_GROUPS[sub]


def is_director_argv(words: Sequence[str]) -> bool:
    """True if this command (a segment's words, or a process's command line)
    runs eVi's director tooling: ``evi task verify|reject`` or ``evi attest``,
    however eVi is launched (`evi_args`). Every word of the leading run of
    assignments, wrappers and their options (and an option's value: ``sudo
    -u me``, ``env -u FOO``, ``timeout -s KILL 5``) is tried as the program,
    as the splitter can't tell an option's value from the command."""
    words = list(words)
    after_option = False
    for k, word in enumerate(words):
        args = _evi_args_at(words, k)
        if args is not None and _director_args(args):
            return True
        if (
            not word
            or shellsplit._ASSIGNMENT.match(word)
            or shellsplit._base(word) in shellsplit._WRAPPERS
            or shellsplit._WRAPPER_ARG.match(word)
        ):
            after_option = False
        elif word.startswith("-"):
            after_option = True
        elif after_option:
            after_option = False
        else:
            return False
    return False


def _runs_unknown_evi_command(words: Sequence[str]) -> bool:
    """True if this command runs eVi with a subcommand that can't be read
    until it runs: an expansion where it goes (``evi $CMD T1``, ``evi task
    %V% T1``), or xargs appending it (``... | xargs evi task``)."""
    words = list(words)
    ci = shellsplit._command_index(words)
    args = _evi_args_at(words, ci)
    if ci is None or args is None:
        return False
    appended = any(shellsplit._base(w) == "xargs" for w in words[:ci])
    k = 0
    while k < len(args) and args[k].startswith("-"):
        k += 2 if args[k] in _EVI_VALUE_OPTIONS else 1
    if k >= len(args):
        return appended
    if _DYNAMIC.search(args[k]):
        return True
    group = _DIRECTOR_GROUPS.get(args[k].lower())
    if group is None:
        return False
    k += 1
    while k < len(args) and args[k].startswith("-"):
        k += 1
    return appended if k >= len(args) else bool(_DYNAMIC.search(args[k]))


def _reads_code_from_stdin(words: Sequence[str]) -> bool:
    """True if this command runs an interpreter that reads its program from
    stdin (no script: ``python``, ``python -``, ``node``), so what a heredoc
    or a pipe feeds it is code, not data."""
    ci = shellsplit._command_index(words)
    if ci is None or not _INTERPRETERS.match(shellsplit._base(words[ci])):
        return False
    k = ci + 1
    while k < len(words):
        w = words[k]
        if w == "-":
            return True
        if not w.startswith("-"):
            return False  # a script file (or inline code, which is a code word)
        k += 2 if w in _PY_VALUE_OPTIONS else 1
    return True


def _search_pattern(words: Sequence[str], i: int, prog: str) -> set[int]:
    """The indexes of the words that are the search tool `prog`'s (at
    ``words[i]``) pattern: an ``-e`` / ``--regexp`` / ``-Pattern`` value, or
    the first plain word when only options known to take no value come
    before it (findstr: its first plain word and ``/c:`` strings)."""
    out: set[int] = set()
    positional = True  # still only past options that take no value
    k = i + 1
    while k < len(words):
        w = words[k]
        lw = w.lower()
        if prog == "findstr":
            if w.startswith("/"):
                if lw.startswith("/c:"):
                    out.add(k)
                k += 1
                continue
            out.add(k)
            break
        if lw in _PATTERN_OPTIONS:
            if k + 1 < len(words):
                out.add(k + 1)
            positional = False  # the plain words after it are paths
            k += 2
            continue
        if lw.startswith("--regexp="):
            out.add(k)
            positional = False
            k += 1
            continue
        if w == "--":
            if positional and k + 1 < len(words):
                out.add(k + 1)
            break
        if w.startswith("-") and len(w) > 1:
            if w.startswith("--"):
                known = "=" in w or lw in _SEARCH_LONG_FLAGS
            elif prog in ("select-string", "sls"):
                known = lw in _SLS_SWITCHES
            else:
                known = all(c in _SEARCH_FLAG_LETTERS.get(prog, "") for c in w[1:])
            positional = positional and known
            k += 1
            continue
        if positional:
            out.add(k)
        break
    return out


def _git_data(words: Sequence[str], i: int) -> set[int]:
    """The indexes of a git command's (at ``words[i]``) data words: a commit
    / tag / stash message (``-m``, ``--message``), a log search (``--grep``,
    ``-S``, ``-G``) or a ``git grep`` pattern. Config values (``-c k=v``)
    and everything else are code."""
    k = i + 1
    while k < len(words) and words[k].startswith("-"):
        k += 2 if words[k] in _GIT_VALUE_OPTIONS else 1
    if k >= len(words):
        return set()
    sub = words[k].lower()
    if sub == "grep":
        return _search_pattern(words, k, "grep")
    out: set[int] = set()
    j = k + 1
    while j < len(words):
        w = words[j]
        if w == "--":
            break
        if sub in _GIT_MESSAGE_COMMANDS:
            if w == "--message" or _GIT_MESSAGE_CLUSTER.match(w):
                out.add(j + 1)
                j += 2
                continue
            if w.startswith("--message=") or (w.startswith("-m") and not w.startswith("--")):
                out.add(j)
        elif sub in _GIT_LOG_COMMANDS:
            if w in ("--grep", "-S", "-G"):
                out.add(j + 1)
                j += 2
                continue
            if w.startswith(("--grep=", "-S", "-G")):
                out.add(j)
        j += 1
    return {d for d in out if d < len(words)}


def _code_and_data(words: Sequence[str]) -> tuple[list[str], list[str]]:
    """A command's words split into code and data. Data is what is only
    printed or searched for: an ``echo`` / ``printf`` / ``Write-Output``'s
    plain words, a search tool's pattern (`_search_pattern`), a git message
    or log search (`_git_data`). Everything else is code: the program, its
    options, paths, and whatever another program is handed to run."""
    ci = shellsplit._command_index(words)
    if ci is None:
        return list(words), []
    prog = shellsplit._base(words[ci])
    if prog in _PRINTERS:
        data = {k for k in range(ci + 1, len(words)) if not words[k].startswith("-")}
    elif prog in _SEARCHERS:
        data = _search_pattern(words, ci, prog)
    elif prog == "git":
        data = _git_data(words, ci)
    else:
        data = set()
    return ([w for k, w in enumerate(words) if k not in data],
            [words[k] for k in sorted(data)])


def _spread(text: str) -> str:
    """`text` for the tooling pattern, with quotes and list punctuation
    (PowerShell ``'task','verify'``, Python ``["evi", "task"]``) as spaces."""
    return _SPREAD.sub(" ", text)


class _DirectorView:
    """A command line as one shell splits it, read for the director rules."""

    def __init__(self, cmd: str, sp: shellsplit.Split) -> None:
        self.sp = sp
        # (words, code text, code words squashed, data words squashed, source span)
        self.segments: list[tuple[list[str], str, str, str, str]] = []
        stdin_code = False
        for seg in sp.commands:
            words = list(seg.words)
            code, data = _code_and_data(words)
            self.segments.append((
                words, _spread(" ".join(code)),
                "\0".join(_SQUASH.sub("", w).lower() for w in code),
                "\0".join(_SQUASH.sub("", w).lower() for w in data),
                cmd[seg.start:seg.end],
            ))
            stdin_code = stdin_code or _reads_code_from_stdin(words)
        # Text the splitter couldn't follow is matched as is (cmd runs only up
        # to the first newline); so is a line feeding an interpreter its code.
        self.texts: list[str] = []
        if not sp.ok and not (sp.dialect in ("cmd", "pwsh") and sp.error == _REFUSED_BY_SHELL):
            self.texts.append(cmd.split("\n", 1)[0] if sp.dialect == "cmd" else cmd)
        if stdin_code:
            self.texts.append(cmd)

    def trips(self, rule: DestructiveRule) -> bool:
        if any(rule.pattern.search(t) for t in self.texts):
            return True
        if rule is _DIRECTOR_TOOLING:
            return any(
                is_director_argv(words) or _runs_unknown_evi_command(words)
                or rule.pattern.search(code_text)
                for words, code_text, _c, _d, _s in self.segments
            )
        if any(rule.pattern.search(r.text) for r in self.sp.redirects):
            return True
        for _w, _t, code, data, span in self.segments:
            seen: set[str] = set()
            for m in rule.pattern.finditer(span):
                found = _SQUASH.sub("", m.group()).lower()
                if found in seen:
                    continue
                seen.add(found)
                # only data when it lies inside a data word and in no code word
                if found and (found not in data or found in code):
                    return True
        return False


def director_hit(command: str, *, dialect: str | None = None) -> DestructiveRule | None:
    """The director rule `command` trips, or None: as code (`_code_and_data`)
    it names the director's attestation key, sets or clears ``EVI_DIRECTOR``
    / ``EVI_AGENT``, imports or patches `evi.attest`, or runs director
    tooling, in any command segment or what a program is handed to run, as
    `dialect` splits it (default: every dialect, so a line is refused if any
    shell would run it so; a line the splitter can't follow is matched as
    text). A mention that is only data (a search pattern, a commit message,
    an ``echo``, a heredoc body nothing runs) is not. No allow glob or
    disabled rule id applies."""
    if not command or not command.strip():
        return None
    cmd = command.strip()
    if "evi" not in _QUOTING.sub("", cmd).lower() and not any(
        r.pattern.search(cmd) for r in _DIRECTOR_TEXT_RULES
    ):
        return None  # every way of running eVi names it
    views = [_DirectorView(cmd, shellsplit.split(cmd, d))
             for d in ((dialect,) if dialect else shellsplit.DIALECTS)]
    for rule in DIRECTOR_RULES:
        if any(v.trips(rule) for v in views):
            return rule
    return None


_CODE_ENV = re.compile(_DIRECTOR_VARS)


def director_code_hit(code: str) -> DestructiveRule | None:
    """The director rule the Python source `code` (``run_python``) trips, or
    None: it names ``EVI_DIRECTOR`` / ``EVI_AGENT`` at all (reading, popping,
    setting), names the key, imports or patches `evi.attest`, or spells
    director tooling (as a ``["evi", "task", "verify"]`` list too). Code has
    no data positions: every mention counts."""
    if not code or not code.strip():
        return None
    if _CODE_ENV.search(code):
        return _DIRECTOR_BY_ID["director-env"]
    for rule in DIRECTOR_RULES:
        if rule.pattern.search(_spread(code) if rule is _DIRECTOR_TOOLING else code):
            return rule
    return None


# A leading, self-contained `echo`/`printf`/`Write-Output`/`Write-Host` that only
# PRINTS text (no pipe, redirect, command-substitution, or chaining) can't run a
# command, so its (possibly dangerous-looking) argument must not trip the guard.
_PURE_PRINT = re.compile(r'''^\s*(?:echo|printf|Write-Output|Write-Host)\b''', re.IGNORECASE)
_HAS_EXEC_META = re.compile(r'''[|>`;&]|\$\(|\$\{|<\(''')


def _first_hit(cmd: str, disabled: set[str]) -> DestructiveRule | None:
    if _PURE_PRINT.match(cmd) and not _HAS_EXEC_META.search(cmd):
        return None
    for rule in _MATCH_ORDER:
        if rule.id in disabled:
            continue
        if rule.pattern.search(cmd):
            return rule
    return None


def _unexempted_hit(
    cmd: str,
    hit: DestructiveRule,
    globs: list[str],
    disabled: set[str],
    dialect: str | None,
) -> DestructiveRule | None:
    """Apply `destructive_allow` globs per segment: a glob exempts the command
    segments it matches, never the line around them (so ``*--force-with-lease*``
    can't exempt ``rm -rf ~; git push --force-with-lease``)."""
    sp = shellsplit.split(cmd, dialect)
    if not sp.ok:  # can't tell which command a hit belongs to
        return hit
    segs = sp.commands
    exempt = [any(fnmatch.fnmatch(seg.text, g) for g in globs) for seg in segs]
    if not any(exempt):
        return hit
    # Every match must lie only in exempt segments. One that touches no segment
    # (a heredoc body, a comment) or a redirect can't be attributed, so it stands.
    for rule in _MATCH_ORDER:
        if rule.id in disabled:
            continue
        for m in rule.pattern.finditer(cmd):
            a, b = m.start(), max(m.end(), m.start() + 1)
            touched = [k for k, seg in enumerate(segs) if seg.start < b and a < seg.end]
            if (
                not touched
                or not all(exempt[k] for k in touched)
                or any(r.start < b and a < r.end for r in sp.redirects)
            ):
                return rule
    # Backstop: every non-exempt segment, scanned on its own.
    for k, seg in enumerate(segs):
        if not exempt[k]:
            seg_hit = _first_hit(seg.text, disabled)
            if seg_hit is not None:
                return seg_hit
    return None


def rejected_allow_globs(allow: object) -> list[str]:
    """The `destructive_allow` globs `destructive_hit` ignores: those whose
    first word (the command) holds a wildcard (``*``, ``?``, ``[``). Such a glob
    doesn't name the command it exempts: ``*--force-with-lease*`` would exempt
    ``rm -rf ~ --force-with-lease`` as well as the push it was written for, and
    ``r*`` or ``git*`` exempt ``rm -rf ~`` or a force-push. Write the command
    out: ``git push --force-with-lease*``."""
    out: list[str] = []
    for a in allow or ():
        words = str(a).split()
        if words and any(c in words[0] for c in "*?["):
            out.append(str(a))
    return out


def destructive_hit(
    command: str,
    *,
    disable_rules: object = (),
    allow: object = (),
    dialect: str | None = None,
) -> DestructiveRule | None:
    """Return the first matching destructive rule for `command`, or None.

    `allow` is an iterable of fnmatch globs (user opt-outs for a pattern they
    truly want) matched against each command segment of the line as `dialect`
    (default: this platform's shell) splits it: a hit is exempt only when every
    segment it touches matches a glob, and never when the line is unparsable.
    A glob whose command word holds a wildcard is ignored (`rejected_allow_globs`).
    `disable_rules` is an iterable of rule ids to skip entirely. A director
    rule (`director_hit`) is reported first, and neither applies to it.
    """
    if not command or not command.strip():
        return None
    cmd = command.strip()
    director = director_hit(cmd, dialect=dialect)
    if director is not None:
        return director
    disabled = {str(d) for d in (disable_rules or ())}
    hit = _first_hit(cmd, disabled)
    if hit is None:
        return None
    allow = [str(a) for a in (allow or ())]
    rejected = set(rejected_allow_globs(allow))
    globs = [a for a in allow if a.strip() and a not in rejected]
    if not globs:
        return hit
    return _unexempted_hit(cmd, hit, globs, disabled, dialect)
