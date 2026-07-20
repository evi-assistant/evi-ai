"""Sync docs/ into the GitHub wiki at evi-assistant/evi-ai.wiki.

Wikis are FLAT, so docs/features/x.md becomes the page `features-x`. Inter-doc
links are rewritten to wiki page names; anything outside docs/ (examples/,
EVI.md, …) falls back to a GitHub blob URL so it resolves instead of 404ing.
Generates Home and _Sidebar as the wiki's navigation.

Usage:
    python scripts/build-wiki.py <wiki-clone-dir> [--check]

--check writes nothing and reports what would change. Clone first with:
    git clone https://github.com/evi-assistant/evi-ai.wiki.git <dir>
Then commit and `git push origin master` from that clone.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REPO_URL = "https://github.com/evi-assistant/evi-ai"

# Grouped nav for Home/_Sidebar. Names are asserted against docs/ below, so a
# renamed doc fails the build instead of quietly dropping out of the sidebar.
GROUPS: list[tuple[str, list[str]]] = [
    ("Start here", ["features", "configuration", "commands", "tools", "troubleshooting"]),
    (
        "Guides",
        [
            "architecture", "sdk", "sdk-coverage", "multi-machine", "self-update",
            "self-build", "development", "releasing", "desktop-bundling",
            "code-signing", "cli-parity", "claude-code-comparison",
            "future-integrations", "roadmap",
        ],
    ),
]


def slug_of(path: Path) -> str:
    rel = path.relative_to(REPO / "docs")
    return f"features-{path.stem}" if rel.parts[:1] == ("features",) else path.stem


def title_of(md: str, fallback: str) -> str:
    for line in md.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback.replace("-", " ").title()


def rewrite(
    md: str,
    *,
    src_dir: str,
    known: set[str],
    titles: dict[str, str],
    broken: list[str] | None = None,
) -> str:
    """Point relative links at wiki pages, or at GitHub when we don't host them."""

    def fix(m: re.Match[str]) -> str:
        label, href, frag = m.group(1), m.group(2), m.group(3) or ""
        if href.startswith(("http://", "https://", "#", "mailto:")):
            return m.group(0)
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
        if rel.startswith("docs/") and rel.endswith(".md"):
            tail = rel[len("docs/") : -len(".md")]
            slug = tail.replace("features/", "features-", 1) if "/" in tail else tail
            if slug in known:
                # Docs often use the path itself as the link text. On the wiki
                # that label is both ugly and untrue (says .md, goes to a page),
                # so show the target page's real title instead.
                if label.strip("`").endswith(".md"):
                    label = titles.get(slug, label)
                return f"[{label}]({slug}{frag})"
        # GitHub fallback only counts as valid if the file is really there — a
        # typo'd link would otherwise become an absolute URL that 404s, which
        # verify() cannot detect because it skips absolute URLs.
        if broken is not None and not (REPO / rel).exists():
            broken.append(f"{src_dir}: link to missing file {href!r}")
        return f"[{label}]({REPO_URL}/blob/main/{rel}{frag})"

    return re.sub(r"\[([^\]]+)\]\(([^)\s#]+)(#[^)\s]*)?\)", fix, md)


def verify(wiki: Path) -> list[str]:
    """Every internal link must resolve to a real page. Always run — the wiki is
    public, and a dead link there is invisible until someone clicks it."""
    fails: list[str] = []
    pages = {p.stem for p in wiki.glob("*.md")}
    for p in sorted(wiki.glob("*.md")):
        md = p.read_text(encoding="utf-8")
        for label, href in re.findall(r"\[([^\]]+)\]\(([^)\s]+)\)", md):
            if href.startswith(("http://", "https://", "#", "mailto:")):
                continue
            target = href.split("#")[0]
            if target.endswith(".md"):
                fails.append(f"{p.name}: link still points at a .md file -> {href}")
            elif target not in pages:
                fails.append(f"{p.name}: dead wiki link [{label}] -> {href}")
        for _, slug in re.findall(r"\[\[([^\]|]+)\|([^\]]+)\]\]", md):
            if slug not in pages and slug != "Home":
                fails.append(f"{p.name}: dead sidebar link -> {slug}")
        if "](docs/" in md or "](../" in md:
            fails.append(f"{p.name}: unresolved relative path survived")
    for required in ("Home", "_Sidebar"):
        if required not in pages:
            fails.append(f"missing required page: {required}")
    return fails


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    check = "--check" in sys.argv
    if len(args) != 1:
        print(__doc__)
        return 2
    wiki = Path(args[0]).resolve()
    if not (wiki / ".git").is_dir():
        print(f"ERROR: {wiki} is not a git clone of the wiki")
        return 1

    sources = sorted((REPO / "docs").glob("*.md")) + sorted(
        (REPO / "docs" / "features").glob("*.md")
    )
    known = {slug_of(p) for p in sources}

    named = {n for _, names in GROUPS for n in names}
    if missing := sorted(named - known):
        print(f"ERROR: GROUPS names no such doc: {missing}")
        return 1

    # Pass 1: every title, so link labels can be resolved during pass 2.
    titles: dict[str, str] = {
        slug_of(p): title_of(p.read_text(encoding="utf-8"), slug_of(p)) for p in sources
    }

    broken: list[str] = []
    written = 0
    for path in sources:
        slug = slug_of(path)
        md = path.read_text(encoding="utf-8")
        src_dir = path.relative_to(REPO).as_posix().rsplit("/", 1)[0]
        body = rewrite(md, src_dir=src_dir, known=known, titles=titles, broken=broken)
        body += (
            f"\n\n---\n\n_Generated from "
            f"[`{path.relative_to(REPO).as_posix()}`]"
            f"({REPO_URL}/blob/main/{path.relative_to(REPO).as_posix()}) — "
            f"edit there, not here._\n"
        )
        target = wiki / f"{slug}.md"
        if not check:
            target.write_text(body, encoding="utf-8")
        written += 1

    feats = sorted(s for s in known if s.startswith("features-"))
    grouped = GROUPS + [("Feature deep-dives", feats)]
    listed = {n for _, names in grouped for n in names}
    if leftover := sorted(known - listed):
        grouped.append(("Other", leftover))

    home = [
        "# eVi documentation",
        "",
        "eVi is a local-first personal AI assistant — CLI, web, and desktop over one",
        f"shared core. See the [repo]({REPO_URL}) or [evi-ai.dev](https://evi-ai.dev).",
        "",
        "> These pages are generated from `docs/` in the repo. Edit the source there;",
        "> changes here are overwritten on the next sync.",
        "",
    ]
    sidebar = ["### [eVi docs](Home)", ""]
    for heading, names in grouped:
        home += [f"## {heading}", ""]
        sidebar += [f"**{heading}**", ""]
        for n in names:
            home.append(f"- [{titles.get(n, n)}]({n})")
            sidebar.append(f"- [[{titles.get(n, n)}|{n}]]")
        home.append("")
        sidebar.append("")

    if not check:
        (wiki / "Home.md").write_text("\n".join(home), encoding="utf-8")
        (wiki / "_Sidebar.md").write_text("\n".join(sidebar), encoding="utf-8")

    verb = "would write" if check else "wrote"
    print(f"{verb} {written} pages + Home + _Sidebar to {wiki}")

    if check:
        if broken:
            print(f"\n{len(broken)} broken source link(s):")
            for b in broken[:25]:
                print(f"  {b}")
            return 1
        return 0
    if fails := broken + verify(wiki):
        print(f"\n{len(fails)} VERIFICATION FAILURE(S):")
        for f in fails[:25]:
            print(f"  {f}")
        if len(fails) > 25:
            print(f"  … and {len(fails) - 25} more")
        return 1
    print("verified: every internal link resolves to a page")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
