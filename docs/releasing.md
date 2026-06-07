# Releasing

How to cut a new version of eVi.

## One-time setup

1. **PyPI Trusted Publishing.** On https://pypi.org → Manage → Publishing,
   add a pending publisher for the **`evi-assistant`** distribution (the import
   package and CLI stay `evi`; only the PyPI name is `evi-assistant`) pointing at
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
.venv/Scripts/python -m ruff check evi tests scripts

# Build
.venv/Scripts/python -m build
# Outputs to dist/. Both sdist + wheel should appear.

# Smoke-test the wheel in a fresh venv:
python -m venv /tmp/wheel-check
/tmp/wheel-check/bin/python -m pip install ./dist/evi_assistant-X.Y.Z-py3-none-any.whl
/tmp/wheel-check/bin/evi --version
```

## Desktop installers (separate pipeline)

The Tauri desktop app versions **independently** of the Python package
(`desktop/src-tauri/tauri.conf.json` → `version`, currently `0.1.0`), so it
has its own workflow — `.github/workflows/desktop-release.yml` — driven by
`desktop-v*` tags, not the PyPI `v*.*.*` tags above.

```bash
# Bump desktop/src-tauri/tauri.conf.json "version" first if needed, then:
git tag desktop-v0.1.0
git push origin desktop-v0.1.0
```

The workflow (Windows / macOS / Linux matrix, `fail-fast: false`):

1. Freezes the practical-tier sidecar in an isolated `.venv-build`
   (`build-sidecar.{ps1,sh}`) and runs `evi-server --check`.
2. Builds the standalone app via `tauri-action` with
   `--config src-tauri/tauri.standalone.conf.json` (ships the onedir sidecar
   through `bundle.resources`).
3. Creates a **draft** GitHub release for the tag and attaches the installers
   (`.msi`/`-setup.exe` on Windows, `.dmg`/`.app` on macOS,
   `.deb`/`.rpm`/`.AppImage` on Linux). Also uploads them as workflow
   artifacts, so a manual `workflow_dispatch` run (no tag) still produces
   downloadables.

Caveats:

- Installers are **unsigned** — Windows SmartScreen and macOS Gatekeeper warn
  on first run. Code-signing is still TODO (see *Roadmap* below).
- Only the **Windows** path is verified end-to-end (2026-06-06); the macOS and
  Linux jobs use the standard Tauri 2 setup but are unverified — treat their
  first green run as the verification.

### In-app auto-update (Tauri updater)

The desktop app self-updates from a **public** GitHub release channel
(`dmang-dev/evi-ai-releases`) — the source repo `dmang-dev/evi-ai` stays
private, but private release assets 404 for unauthenticated end users, so the
updater can't read them. On launch the Rust shell checks the public repo's
`releases/latest/download/latest.json`, and if a newer **signed** build exists
it downloads, installs, and restarts. Opt out with `EVI_AUTO_UPDATE=0`.

- **Public channel setup (one-time).** `desktop-release.yml`'s `mirror` job
  copies the signed installers + a URL-rewritten `latest.json` from the private
  build release to `dmang-dev/evi-ai-releases`. It needs a **`RELEASES_TOKEN`**
  secret on the private repo — a PAT (classic: `repo` scope; or fine-grained:
  **Contents: Read and write** on `evi-ai-releases`). Create it at
  github.com/settings/tokens, then `gh secret set RELEASES_TOKEN -R
  dmang-dev/evi-ai`. Without it the build still succeeds; the mirror just warns
  and skips (so the updater stays on the last published manifest).

- **Signing keys.** The updater only installs bundles signed with our key. The
  **public** key lives in `tauri.conf.json` (`plugins.updater.pubkey`); the
  **private** key + its password are repo secrets `TAURI_SIGNING_PRIVATE_KEY`
  and `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`, consumed by `desktop-release.yml`.
  A local backup is in `~/.evi/evi-updater.key` (+ `.pub`) and
  `~/.evi/evi-updater.pass` — **keep these safe; losing them means no client
  can verify future updates** (you'd have to ship a new pubkey + re-onboard).
- **Cutting an updatable release.** Bump `desktop/src-tauri/tauri.conf.json`
  `version` (and `Cargo.toml` / `package.json` to match), then push a
  `desktop-v<version>` tag. `createUpdaterArtifacts: true` makes the build emit
  the `.sig` files; `tauri-action` (with the signing env) attaches them plus a
  generated `latest.json` to the release. The updater compares the running
  app's version to `latest.json`, so the **version must increase** for clients
  to update.
- **Rotating the key.** `npx tauri signer generate -w ~/.evi/evi-updater.key -f`,
  update the pubkey in `tauri.conf.json`, and reset the two repo secrets
  (`gh secret set …`). Clients on the old pubkey won't auto-update across the
  rotation — they'll need a manual reinstall once.
- **OS code-signing is separate** (and still TODO): the updater's minisign
  signature is not Authenticode/Apple notarization, so SmartScreen/Gatekeeper
  still warn until those are added.

## Docker

The CI workflow doesn't push images by default. To do it manually after a
release:

```bash
docker build -t evi:0.8.0 .
docker tag evi:0.8.0 evi:latest
# Push to GHCR or Docker Hub from here.
```

The `Dockerfile` is set up for the headless web-server use case — pair it
with `docker-compose.yml` for an Ollama + eVi stack out of the box.

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
- macOS/Windows code-signing for the Tauri desktop bundle — the build
  pipeline now exists (`desktop-release.yml`); signing the artifacts (so
  SmartScreen/Gatekeeper don't warn) is the remaining gap. Needs an
  Authenticode cert (Windows) and an Apple Developer ID (macOS), wired into
  `tauri-action` via its signing inputs / env.
