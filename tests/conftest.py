"""Root test conftest.

Keep the e2e suite (tests/e2e/, Playwright-driven) out of the normal unit run
when the `e2e` extra isn't installed — otherwise collecting it would error in
the default CI job (which installs no Playwright). When Playwright IS present,
the e2e tests are still excluded by default via the `e2e` marker + the
`-m "not e2e"` addopts; run them explicitly with `pytest tests/e2e -m e2e`.
"""

collect_ignore_glob: list[str] = []
try:
    import playwright  # noqa: F401
except ImportError:
    collect_ignore_glob = ["e2e/*"]
