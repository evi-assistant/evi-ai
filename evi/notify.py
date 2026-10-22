"""Local completion notifications — ping when a turn finishes or blocks.

eVi runs long *local*-model turns; without a ping you have to babysit the
terminal. This sends a best-effort desktop notification + sound when a turn
completes, so you can walk away. Everything is best-effort and never raises —
a missing `notify-send` just means no toast.

Channels, by platform:
- **sound**: winsound (Windows), `afplay` (macOS), `paplay`/`aplay` (Linux).
- **desktop toast**: `osascript` (macOS), `notify-send` (Linux). Windows visual
  toasts are left to the eVi desktop/web UI (browser Notification API in the
  Tauri webview), which is the reliable dep-free path there.
- **url**: an [ntfy](https://ntfy.sh) topic URL or any webhook — POSTed so a
  *remote* turn can still reach your phone. On-brand: self-host ntfy.

Driven by `[notify]` config (see :class:`evi.config.NotifySettings`); off by
default. The web/desktop UI fires its own browser Notification on the `Done`
SSE event, so this module is the CLI/headless path.
"""

from __future__ import annotations

import base64
import json
import platform
import shutil
import subprocess


def _play_sound() -> bool:
    """Beep; whether a player ran."""
    sysname = platform.system()
    try:
        if sysname == "Windows":
            import winsound

            winsound.MessageBeep(winsound.MB_OK)
            return True
        if sysname == "Darwin":
            if shutil.which("afplay"):
                subprocess.run(
                    ["afplay", "/System/Library/Sounds/Glass.aiff"],
                    timeout=5, capture_output=True,
                )
                return True
            return False
        # Linux / other: try a couple of common players + sound files.
        candidates = [
            ["paplay", "/usr/share/sounds/freedesktop/stereo/complete.oga"],
            ["aplay", "-q", "/usr/share/sounds/alsa/Front_Center.wav"],
        ]
        for cmd in candidates:
            if shutil.which(cmd[0]):
                subprocess.run(cmd, timeout=5, capture_output=True)
                return True
    except Exception:
        return False  # never let a beep break the turn
    return False


# The toast text reaches AppleScript as `argv` of a fixed `run` handler, never
# as script source: quotes, backslashes or `& (do shell script ...)` in a model
# reply are just characters to display, with no escaping to get wrong. The
# handler reads the LAST two items, so it shows the right text whether or not
# osascript passes the "--" separator through to argv.
_OSASCRIPT_TOAST = (
    "-e", "on run argv",
    "-e", "display notification (item -1 of argv) with title (item -2 of argv)",
    "-e", "end run",
)


def _osascript_toast_argv(title: str, body: str) -> list[str]:
    # "--" ends option parsing, so text starting with "-" stays an argument
    # instead of being read as an osascript flag.
    return ["osascript", *_OSASCRIPT_TOAST, "--", title, body]


def _desktop_toast(title: str, body: str) -> bool:
    """Show a toast; whether one was shown (never on Windows, see above)."""
    sysname = platform.system()
    try:
        if sysname == "Darwin" and shutil.which("osascript"):
            subprocess.run(
                _osascript_toast_argv(title, body), timeout=5, capture_output=True,
            )
            return True
        if sysname == "Linux" and shutil.which("notify-send"):
            # "--" so a title/body starting with "-" is text, not an option.
            subprocess.run(["notify-send", "--", title, body], timeout=5, capture_output=True)
            return True
        # Windows visual toast: handled by the desktop/web UI, not here.
    except Exception:
        return False
    return False


def _header_text(text: str) -> str:
    """`text` as an HTTP header value: one line, and RFC 2047 encoded when it
    isn't plain ASCII (http.client sends headers as latin-1, so an em dash or
    an emoji would fail the whole request; ntfy decodes the encoded form)."""
    line = " ".join(str(text or "").split())
    if line.isascii():
        return line
    return "=?UTF-8?B?" + base64.b64encode(line.encode("utf-8")).decode("ascii") + "?="


def _post_url(url: str, title: str, body: str) -> bool:
    """POST to an ntfy topic / webhook. ntfy reads the body as the message and a
    `Title` header; a generic webhook also gets a JSON body. Whether it was
    taken (a 2xx answer)."""
    import urllib.request

    try:
        req = urllib.request.Request(
            url,
            data=body.encode("utf-8"),
            method="POST",
            headers={
                "Title": _header_text(title),
                "Content-Type": "text/plain; charset=utf-8",
                "User-Agent": "evi-notify",
                # JSON mirror in a header-free way: ntfy ignores it, webhooks can read the body.
                "X-Evi-Payload": json.dumps({"title": title, "body": body})[:512],
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 (user-configured)
            return 200 <= int(getattr(resp, "status", 200) or 200) < 300
    except Exception:
        return False  # best-effort


def notify(
    title: str,
    body: str,
    *,
    sound: bool = True,
    desktop: bool = True,
    url: str = "",
) -> list[str]:
    """Fire a notification through every enabled channel. Never raises.
    Returns the channels that delivered ("sound", "desktop", "url")."""
    done: list[str] = []
    if sound and _play_sound():
        done.append("sound")
    if desktop and _desktop_toast(title, body):
        done.append("desktop")
    if url and _post_url(url, title, body):
        done.append("url")
    return done


def notify_if_enabled(title: str, body: str, *, config=None) -> bool:
    """Notify per `[notify]` config. Returns True if enabled (and attempted),
    False when notifications are off. Loads config if not supplied."""
    try:
        if config is None:
            from evi.config import Config

            config = Config.load()
        n = config.notify
    except Exception:
        return False
    if not getattr(n, "enabled", False):
        return False
    notify(
        title,
        body,
        sound=getattr(n, "sound", True),
        desktop=getattr(n, "desktop", True),
        url=(getattr(n, "url", "") or "").strip(),
    )
    return True
