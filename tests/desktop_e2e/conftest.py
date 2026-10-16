"""Harness for the native-desktop smoke suite (opt-in, marker ``desktop``).

Unlike ``tests/e2e`` (Playwright against the web server in headless Chromium),
this drives the **actual built ``evi-desktop.exe``** through ``tauri-driver`` +
Microsoft Edge WebDriver (``msedgedriver``) against the real WebView2 runtime —
the only layer that can prove the Tauri shell itself works: window creation,
WebView2 rendering, the IPC bridge (``__TAURI_INTERNALS__.invoke``, scoped to the
desktop commands — there is no ``window.__TAURI__``), the navigation guard, and
the native-menu → ``window.eviUI`` dispatch path. None of that is reachable from
plain Playwright (Tauri injects its IPC only inside the real webview).

Windows-only (WebView2/msedgedriver). Everything is best-effort and **skips**
cleanly when a piece of the toolchain is absent, so a normal `pytest` (or a
non-Windows box) is never broken by it. Select it with::

    pytest tests/desktop_e2e -m desktop

Prereqs (see TESTING.md): a built ``evi-desktop.exe``, ``tauri-driver`` on PATH
or at ``~/.cargo/bin``, and an ``msedgedriver`` matching the installed WebView2
runtime under ``.webdrivers/``. Override any path via env: ``EVI_DESKTOP_EXE``,
``EVI_TAURI_DRIVER``, ``EVI_MSEDGEDRIVER``.
"""
from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import pytest

pytest.importorskip("selenium")

if sys.platform != "win32":  # WebView2 + msedgedriver are Windows-only
    pytest.skip("desktop e2e is Windows-only", allow_module_level=True)

_REPO = Path(__file__).resolve().parents[2]


def _first_existing(*cands: Path) -> Path | None:
    return next((p for p in cands if p and p.exists()), None)


def _exe() -> Path | None:
    env = os.environ.get("EVI_DESKTOP_EXE")
    return _first_existing(
        Path(env) if env else None,
        _REPO / "desktop" / "src-tauri" / "target" / "release" / "evi-desktop.exe",
    )


def _tauri_driver() -> Path | None:
    env = os.environ.get("EVI_TAURI_DRIVER")
    from shutil import which

    onpath = which("tauri-driver")
    return _first_existing(
        Path(env) if env else None,
        Path(onpath) if onpath else None,
        Path(os.path.expanduser("~/.cargo/bin/tauri-driver.exe")),
    )


def _msedgedriver() -> Path | None:
    env = os.environ.get("EVI_MSEDGEDRIVER")
    from shutil import which

    onpath = which("msedgedriver")
    return _first_existing(
        Path(env) if env else None,
        Path(onpath) if onpath else None,
        _REPO / ".webdrivers" / "msedgedriver.exe",
    )


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_http(url: str, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:  # noqa: S310 (localhost)
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def _taskkill(*images: str) -> None:
    for image in images:
        subprocess.run(["taskkill", "/F", "/IM", image], capture_output=True)


@contextlib.contextmanager
def _evi_server(extra_env: dict[str, str] | None = None, config: str | None = None):
    """Run an eVi web server on a free port with an isolated ``EVI_HOME``
    (``config`` becomes its config.toml)."""
    home = Path(tempfile.mkdtemp(prefix="evi-desktop-e2e-"))
    if config is not None:
        (home / "config.toml").write_text(config, encoding="utf-8")
    port = _free_port()
    env = {**os.environ, "EVI_HOME": str(home), **(extra_env or {})}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "evi.apps.web.server:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(_REPO), env=env,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        if not _wait_http(base + "/api/health", 40):
            proc.terminate()
            pytest.skip("eVi web server did not start for the desktop harness")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


@pytest.fixture(scope="session")
def desktop_server():
    """A minimal eVi web server on a free port with an isolated ``EVI_HOME``.

    No model backend is configured — these tests smoke the *shell* + webview, not
    chat — so the UI loads (banner and all) and every read endpoint answers. The
    desktop shell is pointed here via ``EVI_REMOTE_URL`` (see ``desktop_driver``).
    """
    with _evi_server() as base:
        yield base


# The access token of `desktop_token_server` (a 64-hex value, like the shell's).
REMOTE_TOKEN = "d" * 64


@pytest.fixture(scope="session")
def remote_token() -> str:
    return REMOTE_TOKEN


@pytest.fixture(scope="session")
def desktop_token_server():
    """Like ``desktop_server``, but the server requires a token from every
    client, this machine included: ``[web] auth_token`` (a launch token alone
    would gate other machines only)."""
    with _evi_server(config=f'[web]\nauth_token = "{REMOTE_TOKEN}"\n') as base:
        yield base


@contextlib.contextmanager
def _desktop_session(env_overrides: dict[str, str | None]):
    """A Selenium session driving the real ``evi-desktop.exe`` via tauri-driver,
    launched with ``env_overrides`` (None removes a variable). Skips if any
    toolchain piece is missing."""
    exe, td, med = _exe(), _tauri_driver(), _msedgedriver()
    missing = [n for n, p in (("evi-desktop.exe", exe),
                              ("tauri-driver", td),
                              ("msedgedriver", med)) if p is None]
    if missing:
        pytest.skip(f"desktop toolchain missing: {', '.join(missing)}")

    from selenium import webdriver
    from selenium.webdriver.common.options import ArgOptions

    td_port = _free_port()
    td_env = {
        **os.environ,
        "EVI_AUTO_UPDATE": "0",             # no updater noise mid-test
        "EVI_SIDECAR_UPDATE": "0",
    }
    for key, value in env_overrides.items():
        if value is None:
            td_env.pop(key, None)
        else:
            td_env[key] = value
    td_proc = subprocess.Popen(
        [str(td), "--port", str(td_port), "--native-driver", str(med)],
        env=td_env,
    )
    time.sleep(2.0)  # let the intermediary bind before we dial it

    opts = ArgOptions()
    opts.set_capability("browserName", "wry")
    opts.set_capability("tauri:options", {"application": str(exe)})

    driver = None
    try:
        driver = webdriver.Remote(
            command_executor=f"http://127.0.0.1:{td_port}", options=opts
        )
        yield driver
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        td_proc.terminate()
        # WebView2/host processes can outlive the session — reap strays.
        _taskkill("evi-desktop.exe", "msedgedriver.exe")


@pytest.fixture()
def desktop_driver(desktop_server):
    """A Selenium session driving the real ``evi-desktop.exe`` via tauri-driver,
    in thin-client mode against ``desktop_server`` (no sidecar).

    Function-scoped: each test gets a fresh shell process (WebView2 sessions
    don't reliably survive reuse). Skips if any toolchain piece is missing.
    """
    with _desktop_session({"EVI_REMOTE_URL": desktop_server,  # shell loads OUR server
                           "EVI_REMOTE_TOKEN": None}) as driver:
        yield driver


@pytest.fixture()
def desktop_token_driver(desktop_token_server):
    """Thin-client mode against a server that requires a token, which the shell
    is given in EVI_REMOTE_TOKEN (and injects into the webview)."""
    with _desktop_session({"EVI_REMOTE_URL": desktop_token_server,
                           "EVI_REMOTE_TOKEN": REMOTE_TOKEN}) as driver:
        yield driver


@pytest.fixture()
def lan_desktop(tmp_path):
    """The shell in local mode with ``[federation] bind_lan = true``: it spawns
    its own server (this checkout, this interpreter) on 0.0.0.0 and must hand
    it a generated token. Yields (driver, evi_home)."""
    home = tmp_path / "evi-home"
    home.mkdir()
    (home / "config.toml").write_text("[federation]\nbind_lan = true\n", encoding="utf-8")
    env = {
        "EVI_HOME": str(home),
        "EVI_REPO_ROOT": str(_REPO),
        "EVI_PYTHON": sys.executable,
        "EVI_REMOTE_URL": None,
        "EVI_REMOTE_TOKEN": None,
        "EVI_WEB_AUTH_TOKEN": None,
    }
    port = None
    try:
        with _desktop_session(env) as driver:
            try:
                yield driver, home
            finally:
                try:
                    port = urlsplit(driver.current_url).port
                except Exception:
                    port = None
    finally:
        # The shell kills its server on a clean exit, but the session ends by
        # killing the shell — reap the server it spawned.
        if port:
            _kill_listener(port)


def _kill_listener(port: int) -> None:
    """Kill whatever process listens on TCP `port` (Windows netstat)."""
    out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                         text=True).stdout
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(f":{port}"):
            subprocess.run(["taskkill", "/F", "/T", "/PID", parts[4]], capture_output=True)


@pytest.fixture()
def wait_js():
    """A ``wait_js(driver, script, timeout=30)`` helper: poll a JS predicate until
    truthy. The shim→server redirect and UI boot are async, so tests can't read
    the DOM synchronously right after the session opens."""

    def _wait(driver, script: str, timeout: float = 30.0):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                last = driver.execute_script(script)
                if last:
                    return last
            except Exception:
                pass
            time.sleep(0.5)
        return last

    return _wait
