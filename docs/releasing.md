# Releasing

How to cut a new version of eVi.

## One-time setup

1. **PyPI Trusted Publishing.** On https://pypi.org → Manage → Publishing,
   add a pending publisher for the **`evi-assistant`** distribution (the import
   package and CLI stay `evi`; only the PyPI name is `evi-assistant`) pointing at
   this repo's `release.yml` workflow. No API tokens required.
2. **GHCR.** The container image is pushed to `ghcr.io/<owner>/<repo>` with
   the workflow's own `GITHUB_TOKEN` (`packages: write`); no secret to add.
   Docker Hub is not wired up.
3. **Labels and branch protection (admin).** Create the `docs-skip` label (see
   [TESTING.md → CI gates](../TESTING.md#ci-gates)) and, if you want them to
   block merges, add these checks to branch protection: `actionlint`, `workflow injection (zizmor +
   strict check)`, `docs freshness`, `cargo check + test (desktop shell,
   Windows)` and `playwright UI e2e`.

## Cutting a release

```bash
# 1. Bump the version in two places (they're tripwired by the release CI):
#      pyproject.toml → [project] version
#      evi/__init__.py → __version__
#
# 2. Update CHANGELOG.md — turn `## [Unreleased]` into the new version's
#    entry, exactly `## [0.8.0] — YYYY-MM-DD` (an em dash, and the date you
#    tag, within a day), and leave an empty `## [Unreleased]` above it. The
#    release refuses a tag whose version isn't the newest entry.
#
# 3. Commit, tag, push.
git add pyproject.toml evi/__init__.py CHANGELOG.md
git commit -m "release: 0.8.0"
git tag v0.8.0
git push origin main --tags
```

The `release.yml` workflow:

1. Runs the browser e2e suite (`e2e.yml`, called as a job). Nothing below
   runs unless it passes: 1.0.49 reached PyPI with a dead button because the
   release only ran the unit suite. A failing test gets one retry; a test
   marked `@pytest.mark.quarantine` runs in a separate step that can't fail
   the run (see [TESTING.md](../TESTING.md#ci-gates)).
2. Verifies the git tag matches **both** `pyproject.toml`'s version and
   `evi/__init__.py`'s `__version__` (fails on any drift).
3. Checks the CHANGELOG: `python -m tests.test_changelog_lint --tag <tag>
   --date <tagged commit date>` fails unless the file lints clean, its newest
   entry is the tag's version, and that entry is dated within a day of the
   tagged commit. Only `vX.Y.Z` tags pass; pre-release tags (`v1.1.0rc1`) are
   refused, since this pipeline publishes final releases only.
4. Builds sdist + wheel via `python -m build`.
5. Installs the package + all useful extras, runs ruff, then the full test
   suite (`tests` and `.github/actions`).
6. Publishes to PyPI via Trusted Publishing (OIDC — no API token).
7. Keyless (OIDC) sigstore-signs the artifacts and creates a GitHub release
   with auto-generated notes + attached artifacts (wheel/sdist + `*.sigstore.json`).
8. After the PyPI publish, two more jobs: the desktop release
   (`desktop-release.yml` with `release_tag: desktop-v<version>`) and the GHCR
   image (`docker.yml`). Neither runs on its own for a `v*` tag, so a tag cut
   while e2e fails publishes nothing anywhere.

The version and CHANGELOG checks need a tag, so a manual `workflow_dispatch`
re-publish (for a run that failed before publishing) skips them, and skips the
desktop and Docker jobs too.

`tests/test_changelog_lint.py` also runs in the unit suite, so a malformed
header, a duplicate version, a repeated `### Section` heading inside a
released entry (the trace a lost release header leaves) or a date in the
future fails CI before you tag. The same file checks the shape of
`release.yml`: every publishing job sits downstream of e2e, and no gate step
can be skipped or have its failure ignored.

If anything fails before publish, fix it on a new commit and retag with a
patched version (`v0.8.1`). Don't re-push the same tag — GitHub releases
get confused and PyPI rejects duplicates.

## Local verification before tagging

```bash
# Test (the composite actions' tests live under .github/actions)
.venv/Scripts/python -m pytest -q --timeout=30 tests .github/actions

# Lint
.venv/Scripts/python -m ruff check evi tests scripts .github/actions

# The release's CHANGELOG check, as the tag will run it
.venv/Scripts/python -m tests.test_changelog_lint --tag v0.8.0 --date "$(git log -1 --format=%cI)"

# The release's e2e gate (needs the e2e extra + `playwright install chromium`)
.venv/Scripts/python -m pytest tests/e2e -m "e2e and not quarantine" --timeout=120

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
(`desktop/src-tauri/tauri.conf.json` → `version`, currently `1.0.0`), so it
has its own workflow — `.github/workflows/desktop-release.yml` — driven by
`desktop-v*` tags, not the PyPI `v*.*.*` tags above.

```bash
# Bump desktop/src-tauri/tauri.conf.json "version" first if needed
# (keep Cargo.toml / package.json in lockstep), then:
git tag desktop-v1.0.0
git push origin desktop-v1.0.0
```

Every PyPI release also runs this workflow (`release.yml` calls it with
`release_tag: desktop-v<version>` after the PyPI publish), so a `v*` tag cuts
the matching desktop release too.

The workflow (Windows / macOS / Linux matrix, `fail-fast: false`):

1. On Windows, runs the desktop shell's `cargo test` first (its Windows-only
   code, such as the link opener, only compiles there); a failing test keeps
   the Windows installers out of the release.
2. Freezes the practical-tier sidecar in an isolated `.venv-build`
   (`build-sidecar.{ps1,sh}`) and runs `evi-server --check`.
3. Optionally, on Windows, runs the native-desktop smoke tests
   (`tests/desktop_e2e`) against the freshly built `evi-desktop.exe` through
   tauri-driver and an msedgedriver matched to the WebView2 runtime, before
   anything is published. Off by default: tick the `desktop_e2e` input on a
   manual run, or set the repository variable `EVI_DESKTOP_E2E=true` to run it
   on every desktop release. With it on, a missing driver or exe, zero tests or
   any skipped test fails the Windows job, so its installers aren't published.
   Try a manual run before setting the variable.
4. Builds the standalone app via `tauri-action` with
   `--config src-tauri/tauri.standalone.conf.json` (ships the onedir sidecar
   through `bundle.resources`).
5. Creates a **non-draft (published)** GitHub release for the tag and attaches
   the minisign-signed installers (`.msi`/`-setup.exe` on Windows,
   `.dmg`/`.app` on macOS, `.deb`/`.rpm`/`.AppImage` on Linux) plus a generated
   `latest.json` for the in-app updater. Also uploads them as workflow
   artifacts, so a manual `workflow_dispatch` run (no tag) still produces
   downloadables.

Caveats:

- Installers carry a **minisign** signature (for the in-app updater), but are
  **not OS code-signed** — Windows SmartScreen and macOS Gatekeeper still warn
  on first run. Authenticode/Apple notarization is still TODO (see *Roadmap* below).
- All three OSes are verified end-to-end as of the **desktop-v1.0.0** matrix
  (2026-07-01): the Windows / macOS / Linux jobs all built and published signed
  installers + `latest.json` to the public release.

### In-app auto-update (Tauri updater)

The desktop app self-updates directly from the **public** `evi-assistant/evi-ai`
GitHub releases. On launch the Rust shell checks
`releases/latest/download/latest.json`, and if a newer **signed** build exists it
downloads, installs, and restarts. Opt out with `EVI_AUTO_UPDATE=0`.

- **No separate release channel.** Because the repo is public, `desktop-release.yml`
  attaches the signed installers + `latest.json` straight to the repo's own
  releases and the updater reads them there — no mirror repo or `RELEASES_TOKEN`
  needed. (Historically, while the repo was private, a `mirror` job copied assets
  to a separate public releases channel; that's been removed.)

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

`release.yml` publishes the GHCR image (`ghcr.io/<owner>/<repo>`, tagged with
the version, `major.minor` and `latest`, for amd64 and arm64) through
`docker.yml` after the PyPI publish, so it waits for the same e2e and
CHANGELOG gates. `docker.yml` has no tag trigger of its own; a manual
`workflow_dispatch` of it builds, and pushes only if you tick `push`.

To build an image by hand instead:

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

- macOS/Windows code-signing for the Tauri desktop bundle — the build
  pipeline now exists (`desktop-release.yml`); signing the artifacts (so
  SmartScreen/Gatekeeper don't warn) is the remaining gap. Needs an
  Authenticode cert (Windows) and an Apple Developer ID (macOS), wired into
  `tauri-action` via its signing inputs / env.
