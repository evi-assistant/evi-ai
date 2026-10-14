#!/usr/bin/env python3
"""Pin sha256 digests for everything eVi's managed runtime downloads.

eVi runs the `llama-server` it downloads and loads the GGUFs it fetches, so
every download is checked against a sha256 pinned in this repo:

- llama.cpp release assets: the generated `_ASSET_SHA256` block in
  evi/runtime/llamacpp_runtime.py, one pin per asset in its `release_assets()`
  (every `_*_ASSETS` table) for the current LLAMACPP_VERSION (GitHub's
  published asset `digest`).
- catalog GGUFs: `revision` (an HF commit) and `sha256` (the file's LFS
  sha256) on each entry in evi/data/runtime_models.json.

Re-run it after bumping LLAMACPP_VERSION or adding a catalog model, and commit
the result:

    python scripts/pin_runtime_hashes.py                  # pin new assets and models
    python scripts/pin_runtime_hashes.py --models-latest  # also move models to current main
    python scripts/pin_runtime_hashes.py --check          # offline: every download is pinned
    python scripts/pin_runtime_hashes.py --verify         # online: pins still match upstream
    python scripts/pin_runtime_hashes.py --format         # offline: normalize the catalog layout

An asset that is already pinned is never silently re-pinned: asset names carry
the release tag, so a different upstream digest under the same name means the
release asset was replaced, which is exactly what the pin exists to catch. The
run then changes nothing and exits 1; pass --repin-runtime only once you have
confirmed the replacement is genuine. Likewise a model whose pinned commit now
reports a different sha256 is refused (a commit's content can't change).

Only metadata is fetched (the GitHub releases API and the HuggingFace model
API); nothing is downloaded or run. Set GITHUB_TOKEN to avoid GitHub's
anonymous rate limit. Exit codes: 0 ok, 1 missing/stale/mismatched/changed
pins, 2 usage or network error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PY = ROOT / "evi" / "runtime" / "llamacpp_runtime.py"
CATALOG_JSON = ROOT / "evi" / "data" / "runtime_models.json"

GITHUB_RELEASE_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/{tag}"
HF_REVISION_API = "https://huggingface.co/api/models/{repo}/revision/{revision}?blobs=true"

BEGIN = "# BEGIN PINNED SHA256 (scripts/pin_runtime_hashes.py)"
END = "# END PINNED SHA256"

_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
# A JSON scalar, for keeping short lists on one line the way the catalog is
# hand-formatted ("tags": ["tiny", "general"]).
_SCALAR = r'(?:"(?:[^"\\\n]|\\.)*"|-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?|true|false|null)'
_SCALAR_LIST = re.compile(r"\[\n\s*(" + _SCALAR + r"(?:,\n\s*" + _SCALAR + r")*)\n\s*\]")

Fetch = Callable[[str, dict[str, str]], Any]


class PinError(Exception):
    """Upstream metadata didn't give a usable pin (network, missing asset/file)."""


def _get_json(url: str, headers: dict[str, str]) -> Any:
    req = urllib.request.Request(
        url, headers={"User-Agent": "evi-pin-runtime-hashes", "Accept": "application/json",
                      **headers},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (fixed hosts)
        return json.load(resp)


def _runtime_module():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from evi.runtime import llamacpp_runtime

    return llamacpp_runtime


def runtime_assets(rt) -> list[str]:
    """Every release asset the runtime can download: its own single list,
    which covers every `_*_ASSETS` table (a new one included)."""
    return sorted(rt.release_assets())


def fetch_release_digests(tag: str, fetch: Fetch = _get_json) -> dict[str, str]:
    """{asset name: sha256} from a llama.cpp release's published asset digests."""
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        data = fetch(GITHUB_RELEASE_API.format(tag=urllib.parse.quote(tag, safe="")), headers)
    except Exception as exc:  # noqa: BLE001
        raise PinError(f"could not read llama.cpp release {tag}: {exc}") from exc
    out: dict[str, str] = {}
    for asset in data.get("assets", []) or []:
        digest = str(asset.get("digest") or "").lower()
        if digest.startswith("sha256:") and _SHA256.fullmatch(digest[7:]):
            out[str(asset.get("name"))] = digest[7:]
    return out


def fetch_hf_pin(repo: str, filename: str, revision: str = "main",
                 fetch: Fetch = _get_json) -> tuple[str, str]:
    """(commit, sha256) of `filename` in HF `repo` at `revision`."""
    url = HF_REVISION_API.format(repo=repo, revision=urllib.parse.quote(revision, safe=""))
    try:
        data = fetch(url, {})
    except Exception as exc:  # noqa: BLE001
        raise PinError(f"could not read {repo}@{revision}: {exc}") from exc
    commit = str(data.get("sha") or "").lower()
    sib = next((s for s in data.get("siblings", []) or [] if s.get("rfilename") == filename), {})
    sha = str((sib.get("lfs") or {}).get("sha256") or "").lower()
    if not _COMMIT.fullmatch(commit) or not _SHA256.fullmatch(sha):
        raise PinError(f"{repo}@{revision} has no LFS sha256 for {filename}")
    return commit, sha


def render_block(pins: dict[str, str]) -> str:
    lines = [BEGIN, "_ASSET_SHA256: dict[str, str] = {"]
    for name in sorted(pins):
        lines.append(f'    "{name}":')
        lines.append(f'        "{pins[name]}",')
    lines += ["}", END]
    return "\n".join(lines)


def replace_block(source: str, pins: dict[str, str]) -> str:
    start = source.find(BEGIN)
    end = source.find(END, start)
    if start < 0 or end < 0:
        raise PinError(f"pinned-sha256 markers not found in {RUNTIME_PY.name}")
    return source[:start] + render_block(pins) + source[end + len(END):]


def format_catalog(data: dict) -> str:
    """The catalog as JSON in its hand-written layout (2-space indent, short
    scalar lists inline, UTF-8 kept), so a re-pin diffs only the pins."""
    text = json.dumps(data, indent=2, ensure_ascii=False)
    text = _SCALAR_LIST.sub(lambda m: "[" + ", ".join(re.findall(_SCALAR, m.group(1))) + "]",
                            text)
    return text + "\n"


def pin_entry(entry: dict, revision: str, sha256: str) -> dict:
    """`entry` with `revision`/`sha256` set; new keys go right after `filename`."""
    out: dict = {}
    for key, value in entry.items():
        if key in ("revision", "sha256"):
            continue
        out[key] = value
        if key == "filename":
            out["revision"], out["sha256"] = revision, sha256
    if "revision" not in out:
        out["revision"], out["sha256"] = revision, sha256
    return out


def check_pins(assets: list[str], pins: dict[str, str], models: list[dict]) -> list[str]:
    """Offline problems: an unpinned or malformed asset/model pin, or a stale
    asset pin. Empty when every download eVi can make is pinned."""
    problems: list[str] = []
    for name in assets:
        if not _SHA256.fullmatch(str(pins.get(name, ""))):
            problems.append(f"runtime asset {name}: no valid sha256 pin")
    for name in sorted(set(pins) - set(assets)):
        problems.append(f"runtime asset {name}: stale pin (not in the asset tables)")
    for m in models:
        mid = m.get("id", "?")
        if not _SHA256.fullmatch(str(m.get("sha256", ""))):
            problems.append(f"model {mid}: no valid sha256 pin")
        if not _COMMIT.fullmatch(str(m.get("revision", ""))):
            problems.append(f"model {mid}: no pinned HF commit revision")
    return problems


def _load_catalog() -> dict:
    return json.loads(CATALOG_JSON.read_text(encoding="utf-8"))


def cmd_check(rt) -> int:
    problems = check_pins(runtime_assets(rt), dict(rt._ASSET_SHA256), _load_catalog()["models"])
    for p in problems:
        print(f"UNPINNED  {p}")
    if problems:
        print("Run scripts/pin_runtime_hashes.py and commit the result.")
        return 1
    print("ok: every runtime asset and catalog model is pinned")
    return 0


def cmd_verify(rt, fetch: Fetch = _get_json) -> int:
    bad = 0
    digests = fetch_release_digests(rt.LLAMACPP_VERSION, fetch)
    for name in runtime_assets(rt):
        want, have = digests.get(name), rt._ASSET_SHA256.get(name)
        if not want or want != have:
            print(f"MISMATCH  {name}: pinned {have}, upstream {want}")
            bad += 1
    for m in _load_catalog()["models"]:
        commit, sha = fetch_hf_pin(m["hf_repo"], m["filename"], m.get("revision") or "main",
                                   fetch)
        if sha != m.get("sha256") or commit != m.get("revision"):
            print(f"MISMATCH  {m['id']}: pinned {m.get('revision')}/{m.get('sha256')}, "
                  f"upstream {commit}/{sha}")
            bad += 1
    print("ok: pins match upstream" if not bad else f"{bad} pin(s) differ from upstream")
    return 1 if bad else 0


def cmd_write(rt, *, runtime: bool, models: bool, models_latest: bool,
              repin_runtime: bool = False, fetch: Fetch = _get_json) -> int:
    """Pin new runtime assets and catalog models. Everything is fetched and
    checked first, and nothing is written if any existing pin would change
    under the same name (asset) or commit (model)."""
    replaced: list[str] = []
    runtime_text = catalog_text = None
    if runtime:
        digests = fetch_release_digests(rt.LLAMACPP_VERSION, fetch)
        assets = runtime_assets(rt)
        missing = [a for a in assets if a not in digests]
        if missing:
            raise PinError(f"release {rt.LLAMACPP_VERSION} publishes no sha256 digest for: "
                           + ", ".join(missing))
        pins = {a: digests[a] for a in assets}
        current = dict(rt._ASSET_SHA256)
        new = sum(1 for a in assets if a not in current)
        if not repin_runtime:
            replaced += [f"runtime asset {a}: pinned {current[a]}, upstream now {pins[a]}"
                         for a in assets if a in current and current[a] != pins[a]]
        runtime_text = replace_block(RUNTIME_PY.read_text(encoding="utf-8"), pins)
        runtime_msg = f"runtime {rt.LLAMACPP_VERSION}: pinned {len(pins)} assets ({new} new)"
    if models:
        data = _load_catalog()
        out = []
        for m in data["models"]:
            revision = "main" if models_latest else (m.get("revision") or "main")
            commit, sha = fetch_hf_pin(m["hf_repo"], m["filename"], revision, fetch)
            if commit == m.get("revision") and m.get("sha256") and sha != m["sha256"]:
                replaced.append(f"model {m['id']}: commit {commit} pinned sha256 "
                                f"{m['sha256']}, upstream now {sha}")
            elif (commit, sha) != (m.get("revision"), m.get("sha256")):
                print(f"model {m['id']}: {commit[:12]} {sha[:16]}...")
            out.append(pin_entry(m, commit, sha))
        data["models"] = out
        catalog_text = format_catalog(data)
    if replaced:
        for line in replaced:
            print(f"CHANGED   {line}", file=sys.stderr)
        print("An already-pinned download changed upstream: the release asset or file was "
              "replaced. Nothing was written. Confirm the new file is genuine before "
              "re-pinning (--repin-runtime for release assets).", file=sys.stderr)
        return 1
    if runtime_text is not None:
        changed = runtime_text != RUNTIME_PY.read_text(encoding="utf-8")
        if changed:
            RUNTIME_PY.write_bytes(runtime_text.encode("utf-8"))
        print(runtime_msg + ("" if changed else " (unchanged)"))
    if catalog_text is not None:
        changed = catalog_text != CATALOG_JSON.read_text(encoding="utf-8")
        if changed:
            CATALOG_JSON.write_bytes(catalog_text.encode("utf-8"))
        print(f"catalog: pinned {len(data['models'])} models"
              + ("" if changed else " (unchanged)"))
    return 0


def cmd_format() -> int:
    """Offline: rewrite the catalog in the layout a re-pin writes (so a hand
    edit stops failing the round-trip test), changing no values."""
    text = format_catalog(_load_catalog())
    changed = text != CATALOG_JSON.read_text(encoding="utf-8")
    if changed:
        CATALOG_JSON.write_bytes(text.encode("utf-8"))
    print(f"catalog: {'reformatted' if changed else 'already formatted'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="offline: fail if any runtime asset or catalog model is unpinned")
    mode.add_argument("--verify", action="store_true",
                      help="online: fail if any pin differs from upstream")
    mode.add_argument("--format", action="store_true",
                      help="offline: rewrite the catalog in the layout a re-pin writes")
    ap.add_argument("--only", choices=("runtime", "models"),
                    help="write only the runtime asset pins or only the model pins")
    ap.add_argument("--models-latest", action="store_true",
                    help="re-pin every model at its repo's current main (default: keep "
                         "each pinned revision and only pin new models)")
    ap.add_argument("--repin-runtime", action="store_true",
                    help="accept a changed upstream digest for an already-pinned release "
                         "asset (only after confirming the replacement is genuine)")
    args = ap.parse_args(argv)
    if args.format:
        return cmd_format()
    rt = _runtime_module()
    try:
        if args.check:
            return cmd_check(rt)
        if args.verify:
            return cmd_verify(rt)
        return cmd_write(rt, runtime=args.only in (None, "runtime"),
                         models=args.only in (None, "models"),
                         models_latest=args.models_latest,
                         repin_runtime=args.repin_runtime)
    except PinError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
