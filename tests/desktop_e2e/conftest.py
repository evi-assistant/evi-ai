"""Harness for the native-desktop smoke suite (opt-in, marker ``desktop``).

Unlike ``tests/e2e`` (Playwright against the web server in headless Chromium),
this drives the **actual built ``evi-desktop.exe``** through ``tauri-driver`` +
Microsoft Edge WebDriver (``msedgedriver``) against the real WebView2 runtime —
the only layer that can prove the Tauri shell itself works: window creation,
WebView2 rendering, the ``window.__TAURI__`` invoke bridge, and the native-menu
→ ``window.eviUI`` dispatch path. None of that is reachable from plain Playwright
(``withGlobalTauri`` only injects ``__TAURI__`` inside the real webview).

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

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

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


@pytest.fixture(scope="session")
def desktop_server():
    """A minimal eVi web server on a free port with an isolated ``EVI_HOME``.

    No model backend is configured — these tests smoke the *shell* + webview, not
    chat — so the UI loads (banner and all) and every read endpoint answers. The
    desktop shell is pointed here via ``EVI_REMOTE_URL`` (see ``desktop_driver``).
    """
    home = Path(tempfile.mkdtemp(prefix="evi-desktop-e2e-"))
    port = _free_port()
    env = {**os.environ, "EVI_HOME": str(home)}
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


@pytest.fixture()
def desktop_driver(desktop_server):
    """A Selenium session driving the real ``evi-desktop.exe`` via tauri-driver.

    Function-scoped: each test gets a fresh shell process (WebView2 sessions
    don't reliably survive reuse). Skips if any toolchain piece is missing.
    """
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
        "EVI_REMOTE_URL": desktop_server,   # shell loads OUR server, no sidecar
        "EVI_AUTO_UPDATE": "0",             # no updater noise mid-test
        "EVI_SIDECAR_UPDATE": "0",
    }
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
