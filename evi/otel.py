"""OpenTelemetry traces + metrics (Phase 89). Opt-in, OFF by default.

Enabled via ``[telemetry] traces``/``metrics`` plus an ``otlp_endpoint`` (or the
``EVI_OTLP_ENDPOINT`` env var). Everything degrades to a no-op when OTel isn't
installed or isn't enabled, so hot paths can wrap unconditionally:

    from evi import otel
    with otel.span("evi.tool", tool="fs.read"):
        ...
    otel.record_tool("fs.read", ok=True, duration_ms=12.0)

Install the deps with ``pip install 'evi-assistant[otel]'``. eVi never starts an
exporter on its own — without an endpoint nothing leaves the process.
"""

from __future__ import annotations

import contextlib
import os
from typing import Any, Iterator

_ENABLED = False
_TRACER: Any = None
_TOOL_COUNTER: Any = None
_TOOL_DURATION: Any = None


def is_enabled() -> bool:
    return _ENABLED


def init_telemetry(settings: Any = None) -> bool:
    """Configure OTLP tracing/metrics from telemetry settings. Returns whether
    telemetry ended up enabled. Safe + idempotent; never raises."""
    global _ENABLED, _TRACER, _TOOL_COUNTER, _TOOL_DURATION
    if settings is None:
        try:
            from evi.config import Config

            settings = Config.load().telemetry
        except Exception:
            return False

    want_traces = bool(getattr(settings, "traces", False))
    want_metrics = bool(getattr(settings, "metrics", False))
    if not (want_traces or want_metrics):
        return False
    endpoint = (
        os.environ.get("EVI_OTLP_ENDPOINT")
        or getattr(settings, "otlp_endpoint", "")
        or ""
    ).rstrip("/")

    try:
        from opentelemetry import metrics, trace
        from opentelemetry.sdk.resources import Resource
    except ImportError:
        return False

    service = getattr(settings, "service_name", "") or "evi"
    resource = Resource.create({"service.name": service})

    if want_traces:
        try:
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            provider = TracerProvider(resource=resource)
            if endpoint:
                try:
                    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                        OTLPSpanExporter,
                    )

                    provider.add_span_processor(
                        BatchSpanProcessor(
                            OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces")
                        )
                    )
                except ImportError:
                    pass
            trace.set_tracer_provider(provider)
            _TRACER = trace.get_tracer("evi")
        except Exception:
            _TRACER = None

    if want_metrics:
        try:
            from opentelemetry.sdk.metrics import MeterProvider

            readers = []
            if endpoint:
                try:
                    from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                        OTLPMetricExporter,
                    )
                    from opentelemetry.sdk.metrics.export import (
                        PeriodicExportingMetricReader,
                    )

                    readers.append(
                        PeriodicExportingMetricReader(
                            OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics")
                        )
                    )
                except ImportError:
                    pass
            metrics.set_meter_provider(
                MeterProvider(resource=resource, metric_readers=readers)
            )
            meter = metrics.get_meter("evi")
            _TOOL_COUNTER = meter.create_counter(
                "evi.tool.calls", description="tool invocations"
            )
            _TOOL_DURATION = meter.create_histogram(
                "evi.tool.duration", unit="ms", description="tool call duration"
            )
        except Exception:
            _TOOL_COUNTER = _TOOL_DURATION = None

    _ENABLED = _TRACER is not None or _TOOL_COUNTER is not None
    return _ENABLED


@contextlib.contextmanager
def span(name: str, **attrs: Any) -> Iterator[None]:
    """A span context manager — a plain no-op when telemetry is off."""
    if _TRACER is None:
        yield
        return
    with _TRACER.start_as_current_span(name) as sp:
        for k, v in attrs.items():
            try:
                sp.set_attribute(k, v)
            except Exception:
                pass
        yield


def record_tool(name: str, *, ok: bool, duration_ms: float) -> None:
    """Record a tool invocation in the metrics pipeline (no-op when off)."""
    if _TOOL_COUNTER is not None:
        try:
            _TOOL_COUNTER.add(1, {"tool": name, "ok": str(ok).lower()})
        except Exception:
            pass
    if _TOOL_DURATION is not None:
        try:
            _TOOL_DURATION.record(duration_ms, {"tool": name})
        except Exception:
            pass


def reset_for_tests() -> None:
    """Clear module state (test isolation only)."""
    global _ENABLED, _TRACER, _TOOL_COUNTER, _TOOL_DURATION
    _ENABLED = False
    _TRACER = _TOOL_COUNTER = _TOOL_DURATION = None
