"""Native-desktop smoke tests — drive the real ``evi-desktop.exe`` in WebView2.

These prove the parts of the desktop app that live in the Rust/Tauri shell and
are therefore invisible to the Playwright web suite:

* the shell launches a window and WebView2 renders the eVi UI,
* the ``__EVI_PORT__``/health handshake loads the served app (here via
  ``EVI_REMOTE_URL``), and
* the two desktop-only JS surfaces exist and work: ``window.__TAURI__`` (the
  invoke bridge) and ``window.eviUI.handleMenu`` (the native-menu dispatch
  target).

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
