"""Computer-use tools — let the agent drive the mouse, keyboard, and screen.

This is the riskiest tool category in eVi. The defaults reflect that:

- Category `computer` is **never** in `auto_approve_categories` — every
  call goes through the permission callback so a human sees the proposed
  action before it fires.
- `pyautogui.FAILSAFE = True` (the default in pyautogui): moving the mouse
  to (0, 0) aborts whatever the script is doing.
- We refuse to run if pyautogui isn't installed; we don't try to fake it.
- All actions are bounded — typing has a per-keystroke pause; screenshots
  go to a dedicated dir, not arbitrary paths.

If you decide you don't want this category at all, leave `tools.computer =
false` in config.toml (the default) and the tools don't even register.
"""

from __future__ import annotations

from datetime import datetime

from evi.config import SCREENSHOT_DIR, ensure_dirs
from evi.tools.base import tool


_TYPE_INTERVAL = 0.015   # seconds per keystroke
_MOVE_DURATION = 0.15    # smooth mouse moves


def _import_pyautogui():
    """Lazily import pyautogui; raise a clean error if it isn't installed."""
    try:
        import pyautogui  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "computer-use tools require pyautogui — "
            "install with: pip install 'evi-assistant[computer]'"
        ) from exc
    # Belt-and-braces: enforce the failsafe even if a user disabled it.
    pyautogui.FAILSAFE = True
    return pyautogui


@tool(
    description=(
        "Take a screenshot of the primary display. Saves a PNG to "
        "~/.evi/screenshots/<timestamp>.png and returns the path."
    ),
    category="computer",
)
def screenshot() -> str:
    try:
        pg = _import_pyautogui()
    except RuntimeError as exc:
        return f"ERROR: {exc}"
    ensure_dirs()
    path = SCREENSHOT_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    img = pg.screenshot()
    img.save(str(path))
    return str(path)


@tool(
    description=(
        "Move the mouse to (x, y) and click. Use integer pixel coords. "
        "`button` is 'left' (default), 'right', or 'middle'."
    ),
    category="computer",
)
def click(x: int, y: int, button: str = "left") -> str:
    try:
        pg = _import_pyautogui()
    except RuntimeError as exc:
        return f"ERROR: {exc}"
    if button not in ("left", "right", "middle"):
        return f"ERROR: invalid button {button!r}"
    pg.click(x=int(x), y=int(y), button=button, duration=_MOVE_DURATION)
    return f"clicked {button} at ({x},{y})"


@tool(
    description="Move the mouse to (x, y) without clicking.",
    category="computer",
)
def move(x: int, y: int) -> str:
    try:
        pg = _import_pyautogui()
    except RuntimeError as exc:
        return f"ERROR: {exc}"
    pg.moveTo(x=int(x), y=int(y), duration=_MOVE_DURATION)
    return f"moved to ({x},{y})"


@tool(
    description=(
        "Type `text` at the currently focused element. A small pause "
        "between keystrokes makes the input reliable across apps."
    ),
    category="computer",
)
def type_text(text: str) -> str:
    try:
        pg = _import_pyautogui()
    except RuntimeError as exc:
        return f"ERROR: {exc}"
    pg.write(text, interval=_TYPE_INTERVAL)
    return f"typed {len(text)} chars"


@tool(
    description=(
        "Press a single key by name (e.g. 'enter', 'tab', 'esc', 'f5'). "
        "Use type_text() for printable text."
    ),
    category="computer",
)
def key(name: str) -> str:
    try:
        pg = _import_pyautogui()
    except RuntimeError as exc:
        return f"ERROR: {exc}"
    pg.press(name)
    return f"pressed {name}"


@tool(
    description=(
        "Scroll the mouse wheel. Positive `amount` scrolls up, negative "
        "scrolls down. Optional (x, y) targets a specific area first."
    ),
    category="computer",
)
def scroll(amount: int, x: int = -1, y: int = -1) -> str:
    try:
        pg = _import_pyautogui()
    except RuntimeError as exc:
        return f"ERROR: {exc}"
    if x >= 0 and y >= 0:
        pg.moveTo(x=int(x), y=int(y), duration=_MOVE_DURATION)
    pg.scroll(int(amount))
    return f"scrolled {amount}"


@tool(
    description=(
        "Return current screen size as JSON `{width, height}`. Useful "
        "before computing click coordinates."
    ),
    category="computer",
)
def screen_size() -> str:
    import json

    try:
        pg = _import_pyautogui()
    except RuntimeError as exc:
        return f"ERROR: {exc}"
    w, h = pg.size()
    return json.dumps({"width": int(w), "height": int(h)})
