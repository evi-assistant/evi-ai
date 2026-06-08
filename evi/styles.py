"""Output styles — switchable response personas.

A style is a short instruction layered onto the system prompt that shapes *how*
eVi responds (tone, length, structure) — independent of the Chat/Cowork/Code
*tool* modes. Three are built in; users can add or override any by dropping a
markdown file at ``~/.evi/styles/<name>.md`` (its contents become the style
text). Selected via ``[llm] output_style`` (empty = eVi's default voice).
"""

from __future__ import annotations

from pathlib import Path

import evi.config as config

BUILTIN_STYLES: dict[str, str] = {
    "concise": (
        "Response style — Concise: answer in as few words as correctness allows. "
        "Lead with the answer, skip preamble and restating the question, prefer "
        "short bullets over paragraphs, and omit caveats unless they matter."
    ),
    "explanatory": (
        "Response style — Explanatory: explain your reasoning as you go. Briefly "
        "note why you chose an approach, surface relevant trade-offs, and define "
        "non-obvious terms — while staying on task."
    ),
    "teacher": (
        "Response style — Teacher: treat each answer as a teaching moment. Build "
        "from fundamentals, use a small concrete example, and end with one or two "
        "things to explore next. Assume an eager learner, not an expert."
    ),
}


def styles_dir(root: Path | None = None) -> Path:
    return (root if root is not None else config.HOME) / "styles"


def _slug(name: str) -> str:
    return Path(name).name.removesuffix(".md")


def list_styles(root: Path | None = None) -> list[str]:
    """All available style names — built-ins plus user files, sorted/unique."""
    names = set(BUILTIN_STYLES)
    d = styles_dir(root)
    if d.is_dir():
        names.update(p.stem for p in d.glob("*.md"))
    return sorted(names)


def style_text(name: str, root: Path | None = None) -> str:
    """The style's instruction text. A user file overrides a built-in of the
    same name. Empty/unknown name → "" (eVi's default voice)."""
    if not name:
        return ""
    slug = _slug(name)
    p = styles_dir(root) / f"{slug}.md"
    if p.is_file():
        try:
            return p.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return BUILTIN_STYLES.get(slug, "")
