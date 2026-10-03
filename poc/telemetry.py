"""Tracing: OpenTelemetry spans exported to Application Insights when a connection string is set.

Spans carry metadata only (IDs, document IDs, tool names, states, tokens, latency) — not prompt or document text.
"""
import contextlib
import os

_tracer = None


def _init():
    global _tracer
    if _tracer is not None:
        return _tracer
    conn = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip()
    try:
        from opentelemetry import trace
        if conn:
            from azure.monitor.opentelemetry import configure_azure_monitor
            configure_azure_monitor(connection_string=conn)
        _tracer = trace.get_tracer("enterprise-assistant-poc")
    except Exception:
        _tracer = False          # telemetry must never break the request path
    return _tracer


class _NullSpan:
    def set_attribute(self, *a, **k):
        pass


@contextlib.contextmanager
def span(name: str, attributes: dict | None = None):
    tracer = _init()
    if not tracer:
        yield _NullSpan()
        return
    with tracer.start_as_current_span(name) as sp:
        for k, v in (attributes or {}).items():
            if v is not None:
                sp.set_attribute(k, v if isinstance(v, (str, int, float, bool)) else str(v))
        yield sp
