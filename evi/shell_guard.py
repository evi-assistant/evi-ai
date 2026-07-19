"""Curated destructive-shell-command guard.

A default-on, deterministic second gate over shell-executing tools: a small,
curated set of high-precision patterns for genuinely destructive/irreversible
commands (``rm -rf ~``, ``git reset --hard``, disk formats, force-pushes,
credential exfil, IaC mass-teardown, …) across bash/zsh, PowerShell, and cmd —
since eVi runs on Windows as well as macOS/Linux.

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
  destructive-but-often-intended. Both currently resolve to "require explicit
  confirmation" at the agent layer; the tier is surfaced in the reason.

Config (``[auto]`` in config.toml): ``block_destructive`` (default true),
``destructive_allow`` (fnmatch globs matched against the whole command that
exempt it), and ``destructive_disable_rules`` (builtin rule ids to silence).
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class DestructiveRule:
    id: str
    severity: str  # "block" | "prompt"
    reason: str
    pattern: re.Pattern[str]


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

# A leading, self-contained `echo`/`printf`/`Write-Output`/`Write-Host` that only
# PRINTS text (no pipe, redirect, command-substitution, or chaining) can't run a
# command, so its (possibly dangerous-looking) argument must not trip the guard.
_PURE_PRINT = re.compile(r'''^\s*(?:echo|printf|Write-Output|Write-Host)\b''', re.IGNORECASE)
_HAS_EXEC_META = re.compile(r'''[|>`;&]|\$\(|\$\{|<\(''')


def destructive_hit(
    command: str,
    *,
    disable_rules: object = (),
    allow: object = (),
) -> DestructiveRule | None:
    """Return the first matching destructive rule for `command`, or None.

    `allow` is an iterable of fnmatch globs matched against the WHOLE command —
    a match exempts the command (user opt-out for a pattern they truly want).
    `disable_rules` is an iterable of rule ids to skip entirely.
    """
    if not command or not command.strip():
        return None
    cmd = command.strip()

    # Whole-command glob exemptions only (no substring match — a substring
    # exemption would let `<allowed>; rm -rf ~` through).
    for a in allow or ():
        a = str(a)
        if a and fnmatch.fnmatch(cmd, a):
            return None

    if _PURE_PRINT.match(cmd) and not _HAS_EXEC_META.search(cmd):
        return None

    disabled = {str(d) for d in (disable_rules or ())}
    for rule in _MATCH_ORDER:
        if rule.id in disabled:
            continue
        if rule.pattern.search(cmd):
            return rule
    return None
