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
    collect_ignore_glob += ["e2e/*"]

# Same guard for the native-desktop suite (tests/desktop_e2e/, Selenium +
# tauri-driver). It's opt-in via the `desktop` marker and Windows-only, but if
# Selenium isn't installed even collecting it would error, so keep it out of the
# default unit run. Run it with `pytest tests/desktop_e2e -m desktop`.
try:
    import selenium  # noqa: F401
except ImportError:
    collect_ignore_glob += ["desktop_e2e/*"]

# Python's http.server calls socket.getfqdn() during server_bind, which does a
# reverse-DNS lookup that hangs for 30s+ on macOS CI runners — enough to trip
# pytest --timeout and fail the whole run (test_a2a / test_federation / test_hooks
# all spin up a throwaway HTTPServer). No eVi code uses getfqdn, so short-circuit
# it in the test process to the passed host (localhost). Harmless everywhere;
# only macOS actually stalled.
import socket as _socket  # noqa: E402

_socket.getfqdn = lambda name="": name or "localhost"


# Per-session state lives in ContextVars (the session working folder, the
# active skill's tool scope). A test that runs a CLI command in-process can set
# one in this very thread and never reset it, and the next test on the same
# xdist worker then resolves paths against a stale folder. Give every test a
# clean slate and restore afterwards.
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_session_contextvars():
    from evi import skillscope, workdir

    cwd_token = workdir.set_cwd(None)
    scope_token = skillscope._SCOPE.set(None)
    try:
        yield
    finally:
        skillscope._SCOPE.reset(scope_token)
        workdir.reset(cwd_token)
