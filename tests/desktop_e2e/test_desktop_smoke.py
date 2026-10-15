"""Native-desktop smoke tests — drive the real ``evi-desktop.exe`` in WebView2.

These prove the parts of the desktop app that live in the Rust/Tauri shell and
are therefore invisible to the Playwright web suite:

* the shell launches a window and WebView2 renders the eVi UI,
* the ``__EVI_PORT__``/health handshake loads the served app (here via
  ``EVI_REMOTE_URL``), and
* the two desktop-only JS surfaces exist and work: ``window.__TAURI__`` (the
  invoke bridge) and ``window.eviUI.handleMenu`` (the native-menu dispatch
  target), and
* the access-token handshake (SHELL_ABI 2): with ``bind_lan`` the shell's own
  server gets a generated token the UI signs in with, and a thin client injects
  ``EVI_REMOTE_TOKEN`` for its server.

Opt-in, Windows-only; see ``conftest.py``. Run: ``pytest tests/desktop_e2e -m desktop``.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.desktop


def test_shell_launches_and_renders_ui(desktop_driver, desktop_server, wait_js):
    # Window titled "eVi", the real chat UI present (not just the loading shim).
    has_input = wait_js(desktop_driver, "return !!document.querySelector('#input')")
    assert has_input, "eVi chat UI (#input) never rendered inside WebView2"
    assert desktop_driver.title == "eVi"
    # We loaded the served app, not a frozen local page.
    assert desktop_driver.current_url.rstrip("/") == desktop_server.rstrip("/")


def test_tauri_invoke_bridge_is_present(desktop_driver, wait_js):
    # The one thing plain Playwright CANNOT verify: inside the real webview
    # `withGlobalTauri` injects window.__TAURI__, so isDesktop() is true and the
    # desktop commands are wired. A regression here silently breaks
    # updates/logs/open-external in the shipped app.
    wait_js(desktop_driver, "return !!window.__TAURI__")
    bridge_ok = desktop_driver.execute_script(
        "return !!(window.__TAURI__ && window.__TAURI__.core "
        "&& typeof window.__TAURI__.core.invoke === 'function')"
    )
    assert bridge_ok, "window.__TAURI__.core.invoke missing in the desktop webview"
    # eviUI (the menu/command bridge) exposes the desktop entry points.
    eviui_ok = desktop_driver.execute_script(
        "return !!(window.eviUI "
        "&& typeof window.eviUI.checkForUpdates === 'function' "
        "&& typeof window.eviUI.handleMenu === 'function')"
    )
    assert eviui_ok, "window.eviUI desktop bridge not fully wired"


def test_native_menu_bridge_opens_settings(desktop_driver, wait_js):
    # The native File → Settings menu dispatches window.eviUI.handleMenu('settings')
    # (main.rs run_webview_action). Exercise that exact bridge and assert the
    # settings overlay opens with its nav populated — a dead handler here means a
    # native menu item that silently does nothing.
    assert wait_js(desktop_driver, "return !!document.querySelector('#input')"), \
        "UI never booted"
    desktop_driver.execute_script("window.eviUI.handleMenu('settings')")
    shown = wait_js(
        desktop_driver,
        "return document.querySelector('#settings-overlay')"
        "  && document.querySelector('#settings-overlay').classList.contains('shown')"
        "  && document.querySelector('#settings-nav')"
        "  && document.querySelector('#settings-nav').children.length > 0",
        timeout=10,
    )
    assert shown, "handleMenu('settings') did not open a populated settings overlay"


def _status(url: str, token: str | None = None) -> int:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310 (localhost)
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def _lan_ip() -> str | None:
    """This host's non-loopback IPv4 address, or None (no route out). A UDP
    connect only picks the route; nothing is sent."""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    return None if ip.startswith("127.") or ip == "0.0.0.0" else ip


_OVERLAY_SHOWN = (
    "return !!(document.querySelector('#auth-overlay')"
    " && document.querySelector('#auth-overlay').classList.contains('shown'))"
)


def test_bind_lan_starts_with_a_generated_token(lan_desktop, wait_js):
    # `[federation] bind_lan = true`: the shell spawns its server on 0.0.0.0 and
    # must hand it a token — generated at first need, persisted, injected into
    # the webview — so the UI works with no login and other machines need it.
    driver, home = lan_desktop
    assert wait_js(driver, "return !!document.querySelector('#input')", timeout=120), \
        "eVi chat UI never rendered (the shell's own server did not come up)"
    token = (home / "desktop-token").read_text(encoding="utf-8").strip()
    assert len(token) == 64, token
    assert driver.execute_script("return window.__EVI_TOKEN__ || ''") == token
    assert not wait_js(driver, _OVERLAY_SHOWN, timeout=3), "the UI asked for a token"
    base = driver.execute_script("return location.origin")
    # This machine needs no token (an older VS Code extension keeps working)...
    assert _status(base + "/api/dispatch") == 200
    # ...another machine does: reach the server through this host's LAN address.
    lan_ip = _lan_ip()
    if lan_ip:
        lan = base.replace("127.0.0.1", lan_ip)
        assert _status(lan + "/api/dispatch") == 401
        assert _status(lan + "/api/dispatch", token) == 200


def test_remote_thin_client_signs_in_with_its_token(desktop_token_driver,
                                                    desktop_token_server, remote_token,
                                                    wait_js):
    # EVI_REMOTE_URL against a token-gated server: the shell injects
    # EVI_REMOTE_TOKEN for that origin only, so the thin client still connects
    # with no login prompt.
    driver = desktop_token_driver
    assert wait_js(driver, "return !!document.querySelector('#input')"), "UI never rendered"
    assert driver.current_url.rstrip("/").startswith(desktop_token_server)
    assert driver.execute_script("return window.__EVI_TOKEN__ || ''") == remote_token
    assert not wait_js(driver, _OVERLAY_SHOWN, timeout=3), "the thin client asked for a token"
    assert _status(desktop_token_server + "/api/dispatch") == 401
