"""Multi-backend registry — the menu of model providers eVi can talk to.

eVi's ``[llm]`` config always holds the *active* backend (kind + base_url +
api_key + model), so ``make_client()`` / ``get_backend()`` / the agent are all
unchanged. This registry (``~/.evi/backends.json``, mirroring ``peers.json``) is
the LIST of AVAILABLE backends you can pick from — local (ollama / lmstudio /
llamacpp) and online (openai / xai / anthropic / … via presets). Selecting one in
the model picker copies its kind/base_url/api_key into ``[llm]``.

On-disk shape (``backends.json``)::

    [{"name": "openai", "kind": "openai_compat",
      "base_url": "https://api.openai.com/v1", "api_key": "env:OPENAI_API_KEY",
      "enabled": true}]

``api_key`` may be an ``env:VARNAME`` reference (resolved at client-build time so
the secret stays out of files) or an inline secret — user's choice per backend.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evi.config import BACKENDS_PATH


@dataclass
class BackendEntry:
    """One configured backend. ``kind`` is the backend type; ``name`` is a unique
    label. Exposes ``.backend`` (== kind) + base_url/api_key/request_timeout so it
    duck-types ``LLMSettings`` for ``get_backend()`` / ``make_client()``."""

    name: str
    kind: str = "openai_compat"
    base_url: str = ""
    api_key: str = ""
    enabled: bool = True
    # Opt this backend's models into the subagent fan-out pool (ultracode /
    # workflows / teams may route agents to it). Off by default — you choose
    # which providers are allowed to serve delegated agents.
    fanout: bool = False
    request_timeout: float = 120.0

    @property
    def backend(self) -> str:  # get_backend()/make_client() read `.backend`
        return self.kind


def _default_base_url(kind: str) -> str:
    from evi.backends.factory import default_base_url

    return default_base_url(kind)


def load_backends(path: Path | None = None) -> list[BackendEntry]:
    """Read ~/.evi/backends.json. Missing/malformed → []; bad entries skipped."""
    p = path or BACKENDS_PATH
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    out: list[BackendEntry] = []
    for e in data:
        if not isinstance(e, dict):
            continue
        name = str(e.get("name") or "").strip()
        if not name:
            continue
        kind = str(e.get("kind") or "openai_compat").strip()
        base = str(e.get("base_url") or "").strip() or _default_base_url(kind)
        out.append(
            BackendEntry(
                name=name,
                kind=kind,
                base_url=base,
                api_key=str(e.get("api_key", "")),
                enabled=bool(e.get("enabled", True)),
                fanout=bool(e.get("fanout", False)),
                request_timeout=float(e.get("request_timeout", 120.0) or 120.0),
            )
        )
    return out


def save_backends(entries: list[BackendEntry], path: Path | None = None) -> None:
    p = path or BACKENDS_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "name": e.name,
            "kind": e.kind,
            "base_url": e.base_url,
            "api_key": e.api_key,
            "enabled": e.enabled,
            "fanout": e.fanout,
        }
        for e in entries
    ]
    p.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")


def get_entry(name: str, entries: list[BackendEntry] | None = None) -> BackendEntry | None:
    n = (name or "").strip().lower()
    for e in entries if entries is not None else load_backends():
        if e.name.lower() == n:
            return e
    return None


def add_backend(entry: BackendEntry, path: Path | None = None, *, overwrite: bool = False) -> bool:
    """Add (or with `overwrite` replace) a backend by name. False if it exists
    and overwrite is off."""
    entries = load_backends(path)
    for i, ex in enumerate(entries):
        if ex.name.lower() == entry.name.lower():
            if not overwrite:
                return False
            entries[i] = entry
            save_backends(entries, path)
            return True
    entries.append(entry)
    save_backends(entries, path)
    return True


def remove_backend(name: str, path: Path | None = None) -> bool:
    entries = load_backends(path)
    kept = [e for e in entries if e.name.lower() != (name or "").strip().lower()]
    if len(kept) == len(entries):
        return False
    save_backends(kept, path)
    return True


def from_preset(preset_name: str, *, name: str = "", api_key: str = "") -> BackendEntry | None:
    """Build a registry entry from an ONLINE_PRESETS provider (openai/xai/…).

    With no `api_key`, defaults to an ``env:<PROVIDER>_API_KEY`` reference; pass a
    key to store it inline instead."""
    from evi.backends.presets import get_preset

    p = get_preset(preset_name)
    if p is None:
        return None
    return BackendEntry(
        name=(name or p.name).strip(),
        kind="openai_compat",
        base_url=p.base_url,
        api_key=api_key or f"env:{p.api_key_env}",
    )


def _entry_from_llm(llm: Any) -> BackendEntry:
    """Synthesize a registry entry from the legacy single ``[llm]`` backend, so a
    config with no registry still presents its one active backend."""
    kind = (getattr(llm, "backend", None) or "lmstudio").strip()
    return BackendEntry(
        name=kind,
        kind=kind,
        base_url=getattr(llm, "base_url", "") or _default_base_url(kind),
        api_key=getattr(llm, "api_key", "") or "",
        request_timeout=float(getattr(llm, "request_timeout", 120.0) or 120.0),
    )


def effective_backends(cfg: Any) -> list[BackendEntry]:
    """The registry, or a single synthesized entry from ``[llm]`` when the
    registry is empty (back-compat)."""
    entries = load_backends()
    return entries if entries else [_entry_from_llm(cfg.llm)]


def active_backend_name(cfg: Any, entries: list[BackendEntry] | None = None) -> str:
    """Which registry entry matches the active ``[llm]`` backend (by kind +
    base_url). Falls back to the kind so the UI always has something to show."""
    ents = entries if entries is not None else effective_backends(cfg)
    kind = (getattr(cfg.llm, "backend", "") or "").strip().lower()
    base = (getattr(cfg.llm, "base_url", "") or "").strip().rstrip("/").lower()
    for e in ents:
        if e.kind.lower() == kind and e.base_url.strip().rstrip("/").lower() == base:
            return e.name
    return kind or (ents[0].name if ents else "")


def client_for(entry: BackendEntry):
    """An OpenAI client routed at `entry` (resolves an ``env:`` api_key)."""
    from evi.llm.client import make_client

    return make_client(entry)  # entry duck-types LLMSettings


def list_models_for(entry: BackendEntry) -> list[str]:
    """Model ids offered by `entry`, or [] if unreachable (never raises)."""
    from evi.backends import get_backend

    try:
        return [m.id for m in get_backend(entry).list_models()]
    except Exception:  # noqa: BLE001 — an unreachable backend must not break the picker
        return []


def all_models(cfg: Any, *, max_workers: int = 8) -> list[dict]:
    """Probe every enabled backend concurrently and return, per backend,
    ``{"backend", "kind", "reachable", "models": [ids]}`` — sorted by name.

    Unreachable backends still appear (reachable=false, empty models) so the UI
    can show them greyed out rather than silently dropping them."""
    entries = [e for e in effective_backends(cfg) if e.enabled]
    if not entries:
        return []

    def _probe(e: BackendEntry) -> dict:
        ids = list_models_for(e)
        return {"backend": e.name, "kind": e.kind, "reachable": bool(ids), "models": ids}

    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(entries)))) as pool:
        rows = list(pool.map(_probe, entries))
    return sorted(rows, key=lambda r: r["backend"].lower())


def fanout_backends() -> list[BackendEntry]:
    """Backends flagged eligible for the subagent fan-out pool (enabled + fanout)."""
    return [e for e in load_backends() if e.enabled and e.fanout]


def fanout_models(*, max_workers: int = 8) -> list[dict]:
    """(backend, model) pairs eligible for subagent fan-out — every model on a
    backend the user flagged ``fanout=True``. Returns
    ``[{"backend", "kind", "model"}, …]``; empty if none are flagged (fan-out then
    just uses the active backend)."""
    entries = fanout_backends()
    if not entries:
        return []

    def _probe(e: BackendEntry) -> list[dict]:
        return [{"backend": e.name, "kind": e.kind, "model": m} for m in list_models_for(e)]

    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(entries)))) as pool:
        nested = list(pool.map(_probe, entries))
    # Interleave by backend (round-robin) so a downstream round-robin over the flat
    # list spreads work across providers, not all of one backend before the next.
    from itertools import zip_longest

    return [row for group in zip_longest(*nested) for row in group if row is not None]
