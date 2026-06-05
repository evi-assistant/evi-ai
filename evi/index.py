"""ProjectIndex — semantic search over a directory tree.

Walk a project root, read text files, split into overlapping chunks,
embed each chunk, persist vectors + metadata. `query(text, k)` returns
the top-k most similar chunks by cosine similarity.

Storage: one directory per indexed root under `~/.evi/indices/<hash>/`,
containing `vectors.npy` (the chunk embeddings) and `meta.json` (the
chunk text + source path + line range).

Limits to keep this honest:
- Default extension allowlist: only common text/code files.
- 10 MB file cap.
- 1500-line file cap per file.
- 800-char chunks with ~100-char overlap.

Scaled for personal-use (~10k chunks). For bigger trees, switch to a
proper vector DB; we keep numpy on disk because it's zero-dep and fits
this scale comfortably.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from evi.config import INDICES_DIR, LLMSettings
from evi.embeddings import embed_texts


_TEXT_EXTENSIONS = frozenset({
    # source code
    ".py", ".js", ".ts", ".tsx", ".jsx", ".rs", ".go", ".rb",
    ".java", ".kt", ".swift", ".c", ".cc", ".cpp", ".h", ".hpp",
    ".cs", ".scala", ".lua", ".php", ".sh", ".ps1", ".bat",
    # config / data
    ".toml", ".yaml", ".yml", ".json", ".xml", ".ini", ".cfg",
    # docs
    ".md", ".markdown", ".rst", ".txt",
    # web
    ".html", ".css", ".scss", ".sass", ".vue", ".svelte",
})

# Skip these directory names anywhere in the walked tree.
_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "__pycache__", "node_modules",
    ".venv", "venv", "env", "dist", "build", "target",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    ".evi",  # never index Evi's own state
})

_MAX_FILE_BYTES = 10 * 1024 * 1024
_MAX_FILE_LINES = 1500
_CHUNK_CHARS = 800
_CHUNK_OVERLAP = 100
_EMBED_BATCH = 32


def _import_numpy():
    """Lazy import — numpy isn't a base dep, only `evi[index]`."""
    try:
        import numpy as np  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "semantic-search index requires numpy — "
            "install with: pip install 'evi-ai[index]'"
        ) from exc
    return np


@dataclass(frozen=True)
class Chunk:
    """One indexed slice of a file."""

    path: str
    start_line: int
    end_line: int
    text: str

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "text": self.text,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Chunk":
        return cls(
            path=d["path"],
            start_line=int(d["start_line"]),
            end_line=int(d["end_line"]),
            text=d["text"],
        )


@dataclass(frozen=True)
class Hit:
    """One semantic-search result."""

    score: float
    chunk: Chunk


def _index_id(root: Path) -> str:
    """Stable ID for an indexed root based on its absolute path."""
    return hashlib.sha1(str(root.resolve()).encode("utf-8")).hexdigest()[:12]


def _index_dir(root: Path) -> Path:
    return INDICES_DIR / _index_id(root)


def _walk_text_files(root: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in __import__("os").walk(root):
        # Modify dirnames in place to prune the walk.
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for fname in filenames:
            p = Path(dirpath) / fname
            if p.suffix.lower() not in _TEXT_EXTENSIONS:
                continue
            try:
                if p.stat().st_size > _MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield p


def _read_lines(path: Path) -> list[str] | None:
    try:
        data = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    lines = data.splitlines()
    if len(lines) > _MAX_FILE_LINES:
        lines = lines[:_MAX_FILE_LINES]
    return lines


def _chunk_lines(rel_path: str, lines: list[str]) -> list[Chunk]:
    """Split a file's lines into overlapping char-bounded chunks."""
    chunks: list[Chunk] = []
    if not lines:
        return chunks

    buf: list[str] = []
    buf_chars = 0
    start_line = 1
    for i, line in enumerate(lines, start=1):
        candidate = line if not buf else line
        added = len(candidate) + 1  # +1 for the newline we'll re-join with
        if buf and buf_chars + added > _CHUNK_CHARS:
            text = "\n".join(buf)
            chunks.append(Chunk(rel_path, start_line, i - 1, text))
            # Slide the window: keep ~_CHUNK_OVERLAP chars worth of tail.
            overlap_lines: list[str] = []
            overlap_chars = 0
            for prev in reversed(buf):
                if overlap_chars + len(prev) + 1 > _CHUNK_OVERLAP:
                    break
                overlap_lines.insert(0, prev)
                overlap_chars += len(prev) + 1
            buf = overlap_lines[:]
            buf_chars = sum(len(line) + 1 for line in buf)
            start_line = i - len(overlap_lines)
        buf.append(line)
        buf_chars += added
    if buf:
        chunks.append(Chunk(rel_path, start_line, len(lines), "\n".join(buf)))
    return chunks


class ProjectIndex:
    """Build / load / query a semantic index over a directory tree."""

    def __init__(self, root: Path, settings: LLMSettings) -> None:
        self.root = Path(root).resolve()
        self.settings = settings
        self.dir = _index_dir(self.root)

    # --- build ---------------------------------------------------------

    def build(self) -> int:
        """Walk the tree, chunk + embed everything, persist. Returns chunks indexed."""
        np = _import_numpy()

        chunks: list[Chunk] = []
        for path in _walk_text_files(self.root):
            lines = _read_lines(path)
            if not lines:
                continue
            rel = str(path.relative_to(self.root)).replace("\\", "/")
            chunks.extend(_chunk_lines(rel, lines))

        if not chunks:
            return 0

        # Embed in batches; preserve order.
        vectors: list[list[float]] = []
        for i in range(0, len(chunks), _EMBED_BATCH):
            batch = chunks[i : i + _EMBED_BATCH]
            vectors.extend(embed_texts([c.text for c in batch], self.settings))

        arr = np.array(vectors, dtype=np.float32)
        # Normalise so cosine similarity = dot product.
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        arr = arr / norms

        self.dir.mkdir(parents=True, exist_ok=True)
        np.save(self.dir / "vectors.npy", arr)
        (self.dir / "meta.json").write_text(
            json.dumps({
                "root": str(self.root),
                "model": self.settings.embed_model,
                "chunks": [c.to_dict() for c in chunks],
            }),
            encoding="utf-8",
        )
        return len(chunks)

    # --- query ---------------------------------------------------------

    def query(self, text: str, k: int = 5) -> list[Hit]:
        # Short-circuit BEFORE we require numpy so callers can probe a
        # non-existent index without installing the optional dep.
        if not self.dir.is_dir():
            return []
        if not (self.dir / "vectors.npy").is_file():
            return []

        np = _import_numpy()
        try:
            meta = json.loads((self.dir / "meta.json").read_text("utf-8"))
            vectors = np.load(self.dir / "vectors.npy")
        except (OSError, json.JSONDecodeError, ValueError):
            return []

        embedded = embed_texts([text], self.settings)
        if not embedded:
            return []
        q = np.array(embedded[0], dtype=np.float32)
        nq = float(np.linalg.norm(q)) or 1.0
        q = q / nq

        sims = vectors @ q  # cosine since both sides are normalised
        # Top-k indices, descending.
        idxs = np.argsort(-sims)[: max(1, int(k))]
        chunks_meta = meta.get("chunks") or []
        out: list[Hit] = []
        for i in idxs:
            i = int(i)
            if i >= len(chunks_meta):
                continue
            out.append(Hit(score=float(sims[i]), chunk=Chunk.from_dict(chunks_meta[i])))
        return out

    # --- introspection -------------------------------------------------

    def exists(self) -> bool:
        return (self.dir / "vectors.npy").is_file() and (self.dir / "meta.json").is_file()

    def stats(self) -> dict:
        if not self.exists():
            return {"indexed": False}
        try:
            meta = json.loads((self.dir / "meta.json").read_text("utf-8"))
        except OSError:
            return {"indexed": False}
        return {
            "indexed": True,
            "root": meta.get("root"),
            "model": meta.get("model"),
            "chunks": len(meta.get("chunks") or []),
        }
