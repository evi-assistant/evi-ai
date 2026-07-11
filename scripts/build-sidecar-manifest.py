#!/usr/bin/env python3
"""Build the `sidecar-latest.json` manifest for the desktop sidecar update channel
(HANDOFF → "sidecar update channel"). The desktop shell fetches this, and if a
newer, ABI-compatible sidecar exists, downloads + verifies + stages it so a *core*
update ships without a full Tauri rebuild.

The manifest is signed separately (minisign, reusing the Tauri updater key) — this
script only assembles the JSON. Per-OS zips are published to a fixed-tag release
(`sidecar-latest`) with deterministic names, so the URLs are computed from the
repo + tag + platform key rather than passed in.

Usage:
    build-sidecar-manifest.py --version 1.0.10 \
        --repo evi-assistant/evi-ai --tag sidecar-latest \
        --sha windows-x86_64=<sha256> --sha darwin-aarch64=<sha256> \
        --sha linux-x86_64=<sha256> [--min-shell-abi 1] [--out sidecar-latest.json]

Platform keys match the Rust client's `platform_key()` (rust target_os/target_arch):
`windows-x86_64`, `darwin-aarch64`, `darwin-x86_64`, `linux-x86_64`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

# The shell↔sidecar contract version. A sidecar in the manifest is only staged by
# a shell whose SHELL_ABI >= the manifest's `min_shell_abi`. Bump when the shell's
# launch contract with the sidecar changes incompatibly (flags, handshake, ports).
DEFAULT_MIN_SHELL_ABI = 1

_SEMVER = re.compile(r"^\d+\.\d+\.\d+([-+].+)?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PLATFORMS = {"windows-x86_64", "darwin-aarch64", "darwin-x86_64", "linux-x86_64"}
_ASSET = {  # platform key -> published zip asset name (must match the CI upload)
    "windows-x86_64": "sidecar-windows-x86_64.zip",
    "darwin-aarch64": "sidecar-darwin-aarch64.zip",
    "darwin-x86_64": "sidecar-darwin-x86_64.zip",
    "linux-x86_64": "sidecar-linux-x86_64.zip",
}


def build(version: str, repo: str, tag: str, shas: dict[str, str],
          min_shell_abi: int) -> dict:
    if not _SEMVER.match(version):
        raise SystemExit(f"bad --version {version!r} (want X.Y.Z)")
    base = f"https://github.com/{repo}/releases/download/{tag}"
    platforms: dict[str, dict] = {}
    for plat, sha in shas.items():
        if plat not in _PLATFORMS:
            raise SystemExit(f"unknown platform {plat!r}; want one of {sorted(_PLATFORMS)}")
        if not _SHA256.match(sha.lower()):
            raise SystemExit(f"bad sha256 for {plat}: {sha!r}")
        platforms[plat] = {"url": f"{base}/{_ASSET[plat]}", "sha256": sha.lower()}
    if not platforms:
        raise SystemExit("no platforms given (need at least one --sha)")
    return {"version": version, "min_shell_abi": int(min_shell_abi), "platforms": platforms}


def _parse_sha(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in pairs:
        if "=" not in p:
            raise SystemExit(f"--sha must be PLATFORM=SHA256, got {p!r}")
        k, v = p.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="build-sidecar-manifest")
    ap.add_argument("--version", required=True)
    ap.add_argument("--repo", required=True, help="owner/repo")
    ap.add_argument("--tag", default="sidecar-latest")
    ap.add_argument("--sha", action="append", default=[], metavar="PLATFORM=SHA256")
    ap.add_argument("--min-shell-abi", type=int, default=DEFAULT_MIN_SHELL_ABI)
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)

    manifest = build(args.version, args.repo, args.tag, _parse_sha(args.sha),
                     args.min_shell_abi)
    text = json.dumps(manifest, indent=2) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
