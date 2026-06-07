"""HuggingFace direct downloads for LM Studio / llama.cpp backends.

Ollama owns its own model store; for it we just call `backend.pull_model()`.
LM Studio and llama.cpp expect GGUF files on disk that the user points the
backend at (`-m <path>` for llama-server, "Local Models" folder for LM
Studio). This module fetches those files into `~/.evi/models/` and prints
the resulting path so the user can wire it up.

Optional dep: `huggingface_hub`. We import it inside `download_gguf` so
users who never call `evi models pull hf:...` don't need it installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from evi.config import MODELS_DIR, ensure_dirs


@dataclass(frozen=True)
class HFRef:
    """`hf:bartowski/Qwen2.5-14B-Instruct-GGUF:Qwen2.5-14B-Instruct-Q4_K_M.gguf`"""

    repo: str
    filename: str | None  # None = pick the first .gguf in the repo


def parse_hf_ref(ref: str) -> HFRef | None:
    """Parse `hf:<repo>` or `hf:<repo>:<filename>`. Returns None on miss."""
    if not ref.startswith("hf:"):
        return None
    body = ref[3:]
    if ":" in body:
        repo, filename = body.split(":", 1)
        return HFRef(repo=repo, filename=filename or None)
    return HFRef(repo=body, filename=None)


def download_gguf(
    ref: HFRef,
    *,
    dest_root: Path | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> Path:
    """Download one GGUF file from HuggingFace; return the local path.

    - If `ref.filename` is set, fetch exactly that file.
    - Otherwise list the repo and pick the smallest Q4_K_M GGUF (a sensible
      default for our personal-use sizing).

    Re-runs are cheap — `huggingface_hub` caches by file hash and returns
    the same path on subsequent calls.
    """
    try:
        from huggingface_hub import HfApi, hf_hub_download
    except ImportError as exc:  # pragma: no cover — exercised in CLI only
        raise RuntimeError(
            "huggingface_hub not installed — run: pip install 'evi-assistant[downloads]'"
        ) from exc

    ensure_dirs()
    local_dir = (dest_root or MODELS_DIR) / ref.repo.replace("/", "__")
    local_dir.mkdir(parents=True, exist_ok=True)

    filename = ref.filename
    if not filename:
        if on_progress:
            on_progress(f"listing files in {ref.repo}…")
        api = HfApi()
        files = api.list_repo_files(repo_id=ref.repo)
        ggufs = [f for f in files if f.lower().endswith(".gguf")]
        if not ggufs:
            raise RuntimeError(f"no GGUF files found in {ref.repo}")
        # Prefer Q4_K_M (the recommended tier for eVi); else first file.
        q4 = [f for f in ggufs if "q4_k_m" in f.lower()]
        filename = (q4 or ggufs)[0]

    if on_progress:
        on_progress(f"downloading {filename}…")
    local_path_str = hf_hub_download(
        repo_id=ref.repo,
        filename=filename,
        local_dir=str(local_dir),
    )
    return Path(local_path_str)
