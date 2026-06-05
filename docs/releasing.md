# Releasing

How to cut a new version of Evi.

## One-time setup

1. **PyPI Trusted Publishing.** On https://pypi.org → Manage → Publishing,
   add a pending publisher for the **`evi-ai`** distribution (the import
   package and CLI stay `evi`; only the PyPI name is `evi-ai`) pointing at
   this repo's `release.yml` workflow. No API tokens required.
2. **Docker Hub / GHCR (optional).** If you want to push the image too,
   add `DOCKER_USERNAME` / `DOCKER_TOKEN` secrets and a job to
   `release.yml` (not enabled by default — see *Roadmap* below).

## Cutting a release

```bash
# 1. Bump the version in two places (they're tripwired by the release CI):
#      pyproject.toml → [project] version
#      evi/__init__.py → __version__
#
# 2. Update CHANGELOG.md — add a section under the new version with the
#    notable changes. Phase memory ($CLAUDE_HOME/.claude/.../project_evi.md)
#    is a good source of truth.
#
# 3. Commit, tag, push.
git add pyproject.toml evi/__init__.py CHANGELOG.md
git commit -m "release: 0.8.0"
git tag v0.8.0
git push origin main --tags
```

The `release.yml` workflow:

1. Verifies the git tag matches `pyproject.toml`'s version.
2. Installs the package + all useful extras.
3. Runs the full test suite.
4. Builds sdist + wheel via `python -m build`.
5. Publishes to PyPI via Trusted Publishing.
6. Creates a GitHub release with auto-generated notes + attached artifacts.

If anything fails before publish, fix it on a new commit and retag with a
patched version (`v0.8.1`). Don't re-push the same tag — GitHub releases
get confused and PyPI rejects duplicates.

## Local verification before tagging

```bash
# Test
.venv/Scripts/python -m pytest -q --timeout=30

# Lint
.venv/Scripts/python -m ruff check evi apps tests

# Build
.venv/Scripts/python -m build
# Outputs to dist/. Both sdist + wheel should appear.

# Smoke-test the wheel in a fresh venv:
python -m venv /tmp/wheel-check
/tmp/wheel-check/bin/python -m pip install ./dist/evi_ai-X.Y.Z-py3-none-any.whl
/tmp/wheel-check/bin/evi --version
```

## Docker

The CI workflow doesn't push images by default. To do it manually after a
release:

```bash
docker build -t evi:0.8.0 .
docker tag evi:0.8.0 evi:latest
# Push to GHCR or Docker Hub from here.
```

The `Dockerfile` is set up for the headless web-server use case — pair it
with `docker-compose.yml` for an Ollama + Evi stack out of the box.

## Versioning

Loose semver:

- **0.x.0 minor** — new features. We're here.
- **0.x.y patch** — bug fixes, polish, no surface changes.
- **1.0.0** — when the surface is stable enough that breaking changes
  warrant a deprecation path. Not there yet.

## What about breaking changes?

Until 1.0, breaking changes are allowed in minor versions. Call them out
clearly in the CHANGELOG under a `### Breaking` heading.

## Roadmap

- Optional Docker push step in `release.yml` (commented out for now).
- Signing wheels with sigstore (post-1.0).
- macOS/Windows code-signing for the Tauri desktop bundle (separate
  release pipeline).
