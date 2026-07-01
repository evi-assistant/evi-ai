"""Regression tests for non-UTF-8 stdout handling.

On Windows, redirecting or piping a command whose output contains Rich status
glyphs (✓ ✗ ⚠ — `evi lint`, `evi doctor`, `evi stats`, permission prompts)
crashes with ``UnicodeEncodeError: 'charmap' codec can't encode character
'✓'`` because the redirected stream defaults to the cp1252 locale codec.

``evi.apps.cli.main._force_utf8_io`` runs at import time and upgrades the std
streams to UTF-8 so the glyphs survive a redirect. These tests cover the
helper directly and exercise the end-to-end path by forcing a cp1252 stdout on
any platform via ``PYTHONIOENCODING``.
"""

from __future__ import annotations

import io
import subprocess
import sys

import pytest

from evi.apps.cli.main import _force_utf8_io

GLYPHS = "✓ ✗ ⚠"
CHECK_BYTES = "✓".encode("utf-8")  # b"\xe2\x9c\x93"


class _FakeStream:
    """Minimal stand-in for a TextIOWrapper that records reconfigure() calls."""

    def __init__(self, encoding: str) -> None:
        self.encoding = encoding
        self.reconfigured: tuple[str | None, str | None] | None = None

    def reconfigure(self, *, encoding: str | None = None, errors: str | None = None) -> None:
        self.reconfigured = (encoding, errors)
        if encoding is not None:
            self.encoding = encoding


def test_force_utf8_io_upgrades_cp1252(monkeypatch: pytest.MonkeyPatch) -> None:
    out, err = _FakeStream("cp1252"), _FakeStream("cp1252")
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    _force_utf8_io()

    assert out.reconfigured == ("utf-8", "replace")
    assert err.reconfigured == ("utf-8", "replace")


@pytest.mark.parametrize("encoding", ["utf-8", "UTF-8", "utf8", "utf_16"])
def test_force_utf8_io_leaves_utf_streams_untouched(
    monkeypatch: pytest.MonkeyPatch, encoding: str
) -> None:
    out = _FakeStream(encoding)
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", _FakeStream(encoding))

    _force_utf8_io()

    assert out.reconfigured is None  # already encodes the glyphs


def test_force_utf8_io_skips_streams_without_reconfigure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # io.StringIO (e.g. pytest capture) has no reconfigure(); must be a no-op,
    # not an AttributeError.
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())

    _force_utf8_io()  # should not raise


def _run_cli(args: list[str], **popen_kwargs) -> subprocess.CompletedProcess:
    """Run `python -m evi …` with a forced cp1252 stdout and a piped (non-tty)
    stream — the exact condition that triggers the crash on Windows."""
    env = {**__import__("os").environ, "PYTHONIOENCODING": "cp1252"}
    return subprocess.run(
        [sys.executable, "-m", "evi", *args],
        env=env,
        capture_output=True,  # pipes => redirected, non-tty stdout
        timeout=120,
        **popen_kwargs,
    )


def test_console_print_survives_piped_cp1252_stdout() -> None:
    """Importing main reconfigures stdout, so the shared console can print
    glyphs to a cp1252 pipe without UnicodeEncodeError."""
    code = (
        "from evi.apps.cli.main import console; "
        f"console.print({GLYPHS!r})"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env={**__import__("os").environ, "PYTHONIOENCODING": "cp1252"},
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert b"UnicodeEncodeError" not in proc.stderr
    assert CHECK_BYTES in proc.stdout  # glyph preserved as UTF-8, not mangled


def test_lint_survives_piped_cp1252_stdout(tmp_path) -> None:
    """`evi lint --path <empty dir>` prints the green ✓ summary; piping it to a
    cp1252 stream used to crash."""
    proc = _run_cli(["lint", "--path", str(tmp_path)])
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert b"UnicodeEncodeError" not in proc.stderr
    assert CHECK_BYTES in proc.stdout
