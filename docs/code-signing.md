# Code signing policy

Free code signing provided by [SignPath.io](https://about.signpath.io), certificate by [SignPath Foundation](https://signpath.org).

## What is signed

eVi's Windows desktop installers — the NSIS `*-setup.exe` and the `.msi`, published
on the [GitHub Releases page](https://github.com/evi-assistant/evi-ai/releases) — are
code-signed. The digital signature identifies the publisher as **SignPath Foundation**
on behalf of the eVi project.

> **Rollout status.** eVi's SignPath Foundation subscription is being set up
> (application submitted July 2026). Until Authenticode signing is live in the release
> pipeline, Windows installers are distributed unsigned — verify them with the SHA-256
> checksums and the update signatures described below. macOS and Linux builds are not
> covered by this certificate (see [Scope](#scope)).

## Project roles

eVi is maintained by a single developer. Following SignPath's role model, the
maintainer holds all signing roles:

- **Authors** — trusted to modify source in version control without additional review:
  [@dmang-dev](https://github.com/dmang-dev)
  (see the [evi-assistant organization owners](https://github.com/orgs/evi-assistant/people?query=role%3Aowner)).
- **Reviewers** — review changes proposed by non-committers:
  [@dmang-dev](https://github.com/dmang-dev).
- **Approvers** — authorize each release for signing:
  [@dmang-dev](https://github.com/dmang-dev).

Every release is built on a trusted build system (GitHub Actions) from the public
source at [github.com/evi-assistant/evi-ai](https://github.com/evi-assistant/evi-ai),
and each signing request is manually approved by an Approver before the artifact is
signed.

## Privacy

eVi is a local-first application. **It will not transfer any information to other
networked systems unless specifically requested by the user or the person installing or
operating it.** eVi runs language models on the user's own hardware, or connects only to
the model backends and services the user explicitly configures (for example, a local LLM
server or a CLI agent the user points it at). Those user-selected third-party services
are governed by their own privacy policies. eVi ships with no telemetry enabled by
default.

## Verifying a signed installer

- **Publisher:** SignPath Foundation
- **Hash algorithm:** SHA-256
- On Windows, right-click the installer → **Properties** → **Digital Signatures** to
  inspect the signature.
- All release artifacts are additionally covered by **minisign** signatures for eVi's
  in-app updater, and PyPI packages are published with **Sigstore** attestations via
  PyPI Trusted Publishing.

## Signing infrastructure

Signing keys are generated and held in SignPath's Hardware Security Module (HSM); the
eVi project never has direct access to the private key. Builds are produced in public CI
and signed only after a maintainer manually approves the release.

## Reporting

To report a suspicious or unsigned binary claiming to be eVi, open a private report via
[GitHub Security Advisories](https://github.com/evi-assistant/evi-ai/security/advisories/new).

## Scope

This certificate covers eVi's **Windows** installers only. macOS and Linux artifacts are
not signed under this certificate — macOS notarization requires an Apple Developer ID,
and Linux packages are distributed unsigned with published checksums. Auto-updates on all
platforms are verified with minisign.
