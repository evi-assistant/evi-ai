"""Tests for the OpenTelemetry layer (Phase 89).

The no-op path (otel not installed / not enabled) is the guaranteed-covered
one; the enabled path is smoke-tested only when opentelemetry is importable.
"""

from __future__ import annotations

import pytest

from evi import otel
from evi.config import TelemetrySettings


@pytest.fixture(autouse=True)
def _reset():
    otel.reset_for_tests()
    yield
    otel.reset_for_tests()


def test_disabled_by_default():
    assert otel.init_telemetry(TelemetrySettings()) is False
    assert otel.is_enabled() is False


def test_span_is_noop_when_disabled():
    # Must work as a context manager even with nothing configured.
    with otel.span("evi.tool", **{"tool.name": "fs.read"}):
        pass


def test_record_tool_noop_when_disabled():
    otel.record_tool("fs.read", ok=True, duration_ms=1.0)  # must not raise


def test_enabled_flag_without_otel_installed(monkeypatch):
    # Even if the user turns traces on, missing deps must not enable / raise.
    import importlib.util

    if importlib.util.find_spec("opentelemetry") is not None:
        pytest.skip("opentelemetry is installed; covered by the enabled-path test")
    st = TelemetrySettings(traces=True, otlp_endpoint="http://localhost:4318")
    assert otel.init_telemetry(st) is False


def test_enabled_path_smoke():
    pytest.importorskip("opentelemetry")
    pytest.importorskip("opentelemetry.sdk")
    st = TelemetrySettings(traces=True)  # no endpoint = no exporter, spans still created
    assert otel.init_telemetry(st) is True
    assert otel.is_enabled() is True
    with otel.span("evi.tool", **{"tool.name": "x"}):
        pass  # creating a span must not raise
