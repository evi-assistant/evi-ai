"""Render docs/*.md into static pages for the evi-ai.dev site.

Uses eVi's OWN Markdown renderer (evi.apps.web.mdlite) — the same one the in-app
Help → Documentation viewer uses — so the website and the app render identically
and this adds no third-party dependency.

Usage:
    python scripts/build-site-docs.py <site-repo-dir>

Writes <site>/docs/index.html plus one page per doc. Idempotent: re-run after any
docs change and commit the result.
"""

from __future__ import annotations

import html as html_mod
import re
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from evi.apps.web.mdlite import render  # noqa: E402

REPO_URL = "https://github.com/evi-assistant/evi-ai"

# These lead the index because they answer "what is this and how do I drive it".
# Every name here must exist in docs/ — main() asserts that, so a renamed or
# deleted doc fails the build instead of silently vanishing from "Start here".
LEAD = ["features", "configuration", "commands", "tools", "troubleshooting"]


def title_of(md: str, fallback: str) -> str:
    for line in md.splitlines():
        if line.startswith("# "):
            return html_mod.escape(line[2:].strip())
    return html_mod.escape(fallback)


def summary_of(md: str) -> str:
    """First real paragraph, flattened to plain text for the index card."""
    body = []
    seen_heading = False
    for line in md.splitlines():
        s = line.strip()
        if s.startswith("# "):
            seen_heading = True
            continue
        if not seen_heading:
            continue
        if not s:
            if body:
                break
            continue
        if s.startswith(("#", "```", "|", ">", "-", "*")):
            if body:
                break
            continue
        body.append(s)
    text = " ".join(body)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\*\*([^*]*)\*\*", r"\1", text)
    text = (text[:180].rstrip() + "…") if len(text) > 180 else text
    # Escape: a doc opening with e.g. "<model> is..." would otherwise inject a
    # bogus tag into the card and break the index's structure.
    return html_mod.escape(text, quote=True)


def slug_for(rel: str) -> str | None:
    """docs/foo.md -> 'foo';  docs/features/foo.md -> 'features-foo'; else None."""
    if not rel.startswith("docs/") or not rel.endswith(".md"):
        return None
    tail = rel[len("docs/") : -len(".md")]
    if "/" not in tail:
        return tail
    sub, _, name = tail.partition("/")
    return f"{sub}-{name}" if sub == "features" and "/" not in name else None


def rewrite_links(html: str, *, src_dir: str, known: set[str]) -> str:
    """Repoint every relative link at either a generated page or GitHub.

    The site is FLAT (docs/features/x.md becomes features-x.html), and pages link
    to each other with paths relative to their own source directory, so a naive
    ".md -> .html" swap produces dead links in both directions. Resolve each href
    against its source dir instead, then map it to a slug — and send anything
    outside docs/ (examples/, EVI.md, …) to GitHub, since we don't publish it.
    """

    def fix(m: re.Match[str]) -> str:
        href, frag = m.group(1), m.group(2) or ""
        if href.startswith(("http://", "https://", "#", "mailto:")):
            return m.group(0)
        # Resolve relative to the source directory, collapsing ../ segments.
        parts: list[str] = []
        for seg in f"{src_dir}/{href}".split("/"):
            if seg in ("", "."):
                continue
            if seg == "..":
                if parts:
                    parts.pop()
            else:
                parts.append(seg)
        rel = "/".join(parts)

        slug = slug_for(rel)
        if slug and slug in known:
            return f'href="{slug}.html{frag}"'
        # Not a published page — point at the source on GitHub so it still works.
        return f'href="{REPO_URL}/blob/main/{rel}{frag}"'

    return re.sub(r'href="([^"#]*)(#[^"]*)?"', fix, html)


def shell(title: str, body: str, *, depth_note: str = "", source: str = "") -> str:
    src = (
        f'<a class="doc-source" href="{REPO_URL}/blob/main/{source}">Edit on GitHub ↗</a>'
        if source
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{title} — eVi docs</title>
  <meta name="description" content="{depth_note or title} — eVi documentation." />
  <link rel="stylesheet" href="../styles.css" />
  <link rel="stylesheet" href="docs.css" />
  <link rel="icon" href="../favicon.svg" type="image/svg+xml" />
</head>
<body>
  <header class="nav">
    <a class="brand" href="../index.html" aria-label="eVi home">
      <span class="brand-mark">e<span class="brand-accent">Vi</span></span>
    </a>
    <nav class="nav-links">
      <a href="index.html">Docs</a>
      <a href="../index.html#install">Install</a>
      <a href="../index.html#download">Download</a>
      <a class="nav-gh" href="{REPO_URL}">GitHub ↗</a>
    </nav>
  </header>
  <main class="doc-main">
    <article class="doc">
{body}
    </article>
    {src}
  </main>
  <footer class="doc-foot">
    <a href="index.html">← All documentation</a>
    <span>eVi — local-first personal AI assistant</span>
  </footer>
</body>
</html>
"""


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    site = Path(sys.argv[1]).resolve()
    if not (site / "index.html").is_file():
        print(f"ERROR: {site} does not look like the site repo (no index.html)")
        return 1

    out = site / "docs"
    out.mkdir(exist_ok=True)

    sources: list[tuple[str, Path]] = [(p.stem, p) for p in sorted((REPO / "docs").glob("*.md"))]
    sources += [
        (f"features-{p.stem}", p) for p in sorted((REPO / "docs" / "features").glob("*.md"))
    ]

    known = {slug for slug, _ in sources}
    entries = []
    for slug, path in sources:
        md = path.read_text(encoding="utf-8")
        title = title_of(md, slug.replace("-", " ").title())
        rel = path.relative_to(REPO).as_posix()
        src_dir = rel.rsplit("/", 1)[0]
        body = rewrite_links(render(md), src_dir=src_dir, known=known)
        page = shell(title, body, depth_note=summary_of(md), source=rel)
        (out / f"{slug}.html").write_text(page, encoding="utf-8")
        entries.append((slug, title, summary_of(md), slug.startswith("features-")))

    known = {e[0] for e in entries}
    if missing := [k for k in LEAD if k not in known]:
        print(f"ERROR: LEAD names no such doc: {missing} — fix LEAD or restore the file")
        return 1
    lead = [e for k in LEAD for e in entries if e[0] == k]
    guides = [e for e in entries if not e[3] and e not in lead]
    feats = [e for e in entries if e[3]]

    def cards(items):
        return "\n".join(
            f'      <a class="doc-card" href="{s}.html"><h3>{t}</h3><p>{d}</p></a>'
            for s, t, d, _ in items
        )

    index_body = f"""      <h1>Documentation</h1>
      <p class="doc-lede">Everything eVi can do, and how to configure it. These pages
      are generated from <a href="{REPO_URL}/tree/main/docs"><code>docs/</code></a> in
      the repo, and ship inside the app too — Help → Documentation works offline.</p>

      <h2>Start here</h2>
      <div class="doc-grid">
{cards(lead)}
      </div>

      <h2>Guides</h2>
      <div class="doc-grid">
{cards(guides)}
      </div>

      <h2>Feature deep-dives</h2>
      <div class="doc-grid">
{cards(feats)}
      </div>
"""
    (out / "index.html").write_text(
        shell("Documentation", index_body, depth_note="eVi documentation index"),
        encoding="utf-8",
    )

    shutil.copyfile(REPO / "scripts" / "site-docs.css", out / "docs.css")

    print(f"wrote {len(entries) + 1} pages to {out}")
    print(f"  start-here: {len(lead)}   guides: {len(guides)}   features: {len(feats)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
