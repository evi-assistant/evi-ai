"""The e2e harness itself (tests/e2e/conftest.py), checked without a browser."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def test_quarantine_marker_is_registered(pytestconfig):
    """e2e.yml and TESTING.md send a known-flaky UI test to a non-blocking step
    with `@pytest.mark.quarantine`; unregistered, that mark only warns, and it
    would fail the whole run under --strict-markers."""
    names = [line.split(":", 1)[0].strip() for line in pytestconfig.getini("markers")]
    assert "quarantine" in names, names
