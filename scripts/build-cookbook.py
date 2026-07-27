"""Render cookbook/*.md into static pages for evi-ai.dev/cookbook.

Sibling of build-site-docs.py — same renderer (evi.apps.web.mdlite), same
link-rewriting and verification, so the cookbook, the docs site, and the in-app
viewer all render identically with no third-party dependency.

cookbook/README.md is the curated index (rendered to cookbook/index.html);
every other cookbook/*.md is one recipe page. Recipe -> recipe links (relative
.md) are rewritten to the flat .html page; links outside cookbook/ (examples/,
docs/) fall back to a GitHub blob URL so they resolve.

Usage:
    python scripts/build-cookbook.py <site-repo-dir> [--check]

--check builds and verifies but writes nothing.
"""

from __future__ import annotations

import html as html_mod
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from evi.apps.web.mdlite import render  # noqa: E402

REPO_URL = "https://github.com/evi-assistant/evi-ai"
SRC = REPO / "cookbook"


def title_of(md: str, fallback: str) -> str:
    for line in md.splitlines():
        if line.startswith("# "):
            return html_mod.escape(line[2:].strip())
    return html_mod.escape(fallback)


def rewrite_links(html: str, *, known: set[str], broken: list[str]) -> str:
    """Recipe links (relative .md in cookbook/) -> flat .html; anything else
    relative -> a GitHub blob URL, but only if the target really exists so a
    typo fails the build instead of becoming a silent 404 (see build-site-docs)."""

    def fix(m: re.Match[str]) -> str:
        href, frag = m.group(1), m.group(2) or ""
        if href.startswith(("http://", "https://", "#", "mailto:")):
            return m.group(0)
        # Resolve relative to cookbook/, collapsing ../ segments.
        parts: list[str] = []
        for seg in f"cookbook/{href}".split("/"):
            if seg in ("", "."):
                continue
            if seg == "..":
                if parts:
                    parts.pop()
            else:
                parts.append(seg)
        rel = "/".join(parts)
        # A recipe page: cookbook/<slug>.md (README is the index -> index.html).
        if rel.startswith("cookbook/") and rel.endswith(".md"):
            slug = rel[len("cookbook/") : -len(".md")]
            slug = "index" if slug == "README" else slug
            if slug in known:
                return f'href="{slug}.html{frag}"'
        if not (REPO / rel).exists():
            broken.append(f"link to missing file {href!r}")
        return f'href="{REPO_URL}/blob/main/{rel}{frag}"'

    return re.sub(r'href="([^"#]*)(#[^"]*)?"', fix, html)


def shell(title: str, body: str, *, source: str) -> str:
    src = (
        f'<a class="doc-source" href="{REPO_URL}/blob/main/{source}">Edit on GitHub ↗</a>'
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{title} — eVi cookbook</title>
  <meta name="description" content="{title} — an eVi cookbook recipe." />
  <link rel="stylesheet" href="../styles.css" />
  <link rel="stylesheet" href="cookbook.css" />
  <link rel="icon" href="../favicon.svg" type="image/svg+xml" />
</head>
<body>
  <header class="nav">
    <a class="brand" href="../index.html" aria-label="eVi home">
      <span class="brand-mark">e<span class="brand-accent">Vi</span></span>
    </a>
    <nav class="nav-links">
      <a href="index.html">Cookbook</a>
      <a href="../docs/index.html">Docs</a>
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
    <a href="index.html">← All recipes</a>
    <span>eVi — local-first personal AI assistant</span>
  </footer>
</body>
</html>
"""


class _Struct(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.bad: list[str] = []
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("br", "img", "meta", "link", "hr", "input"):
            return
        self.stack.append(tag)
        self.hrefs += [v for k, v in attrs if k == "href" and v]

    def handle_endtag(self, tag):
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
        elif tag in self.stack:
            self.bad.append(tag)
            while self.stack and self.stack.pop() != tag:
                pass


def verify(out: Path, recipes: list[str], readme_md: str) -> list[str]:
    """Structure + links + the orphan check the README promises."""
    fails: list[str] = []
    pages = sorted(out.glob("*.html"))
    expected = len(recipes) + 1  # + index
    if len(pages) != expected:
        fails.append(f"expected {expected} pages, found {len(pages)}")

    for p in pages:
        s = _Struct()
        s.feed(p.read_text(encoding="utf-8"))
        if s.bad:
            fails.append(f"{p.name}: mismatched tags {sorted(set(s.bad))}")
        if s.stack:
            fails.append(f"{p.name}: unclosed tags {s.stack}")
        for h in s.hrefs:
            if h.startswith(("http", "#", "mailto:")):
                continue
            if h.endswith(".md") or ".md#" in h:
                fails.append(f"{p.name}: unrewritten markdown link {h!r}")
                continue
            target = (p.parent / h.split("#")[0]).resolve()
            # Only verify links that stay INSIDE the cookbook output. Links that
            # escape to the rest of the site (../docs/, ../index.html, ../styles)
            # are owned by the site structure / other generators — the docs build
            # runs before this one in CI — so their existence is not the
            # cookbook build's to assert.
            if out.resolve() in target.parents or target.parent == out.resolve():
                if not target.exists():
                    fails.append(f"{p.name}: dead cookbook link -> {h}")

    # Every recipe must be linked from the index, or it is unreachable.
    linked = set(re.findall(r"\]\(([\w-]+)\.md\)", readme_md))
    for slug in recipes:
        if slug not in linked:
            fails.append(f"recipe {slug!r} is not linked from cookbook/README.md")
    return fails


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    check = "--check" in sys.argv
    if len(args) != 1:
        print(__doc__)
        return 2
    site = Path(args[0]).resolve()
    if not (site / "index.html").is_file():
        print(f"ERROR: {site} does not look like the site repo (no index.html)")
        return 1

    out = site / "cookbook"
    recipes = sorted(p.stem for p in SRC.glob("*.md") if p.name != "README.md")
    known = set(recipes) | {"index"}
    broken: list[str] = []

    if not check:
        out.mkdir(exist_ok=True)

    readme_md = (SRC / "README.md").read_text(encoding="utf-8")
    pages = [("index", SRC / "README.md", "eVi Cookbook")] + [
        (s, SRC / f"{s}.md", s.replace("-", " ").title()) for s in recipes
    ]
    for slug, path, fallback in pages:
        md = path.read_text(encoding="utf-8")
        title = title_of(md, fallback)
        body = rewrite_links(render(md), known=known, broken=broken)
        html = shell(title, body, source=path.relative_to(REPO).as_posix())
        if not check:
            (out / f"{slug}.html").write_text(html, encoding="utf-8")

    if not check:
        (out / "cookbook.css").write_text(
            (REPO / "scripts" / "site-docs.css").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    print(f"{'would write' if check else 'wrote'} {len(pages)} pages "
          f"({len(recipes)} recipes + index)")

    fails = broken + ([] if check else verify(out, recipes, readme_md))
    # On --check we can't run the output-structure verify (nothing written), but
    # broken source links and the orphan check still apply.
    if check:
        linked = set(re.findall(r"\]\(([\w-]+)\.md\)", readme_md))
        fails += [f"recipe {s!r} not linked from README" for s in recipes if s not in linked]
    if fails:
        print(f"\n{len(fails)} VERIFICATION FAILURE(S):")
        for f in fails[:25]:
            print(f"  {f}")
        return 1
    print("verified: structure, links, and recipe index all OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
