"""OpenTelemetry tracing, viewable in Arize Phoenix.

Off unless PHOENIX_COLLECTOR_ENDPOINT is set (e.g. http://localhost:6006). Then each run is
one trace: a root span per run, a span per graph node, and child spans for every model call
and MCP tool call. Attribute names follow OpenInference conventions so Phoenix shows model
names, token counts, inputs, and outputs. Spans are written by our own wrappers rather than
auto-instrumentation, so they cover exactly the calls we make.

    uvx --from arize-phoenix phoenix serve          # UI at http://localhost:6006
"""

import json
import os
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode

ENDPOINT_ENV = "PHOENIX_COLLECTOR_ENDPOINT"
PROJECT = "covenant-monitor"
MAX_ATTRIBUTE_CHARS = 4000

_tracer: trace.Tracer = trace.NoOpTracer()


def setup(processor: SpanProcessor | None = None) -> TracerProvider | None:
    """Start exporting spans. Returns the provider (call shutdown() to flush), or None when
    tracing is off. Tests pass their own processor."""
    global _tracer
    if processor is None:
        endpoint = os.getenv(ENDPOINT_ENV)
        if not endpoint:
            return None
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        processor = BatchSpanProcessor(
            OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces")
        )
    provider = TracerProvider(
        resource=Resource.create({"service.name": PROJECT, "openinference.project.name": PROJECT})
    )
    provider.add_span_processor(processor)
    _tracer = provider.get_tracer("covenant_monitor")
    return provider


def reset() -> None:
    global _tracer
    _tracer = trace.NoOpTracer()


def _clip(value):
    if isinstance(value, (bool, int, float)):
        return value
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= MAX_ATTRIBUTE_CHARS else text[:MAX_ATTRIBUTE_CHARS] + "…"


@contextmanager
def span(name: str, kind: str, **attributes):
    """kind is an OpenInference span kind: CHAIN, LLM, or TOOL."""
    with _tracer.start_as_current_span(name) as s:
        s.set_attribute("openinference.span.kind", kind)
        set_attributes(s, **attributes)
        yield s


def set_attributes(s, **attributes) -> None:
    for key, value in attributes.items():
        if value is not None:
            s.set_attribute(key.replace("__", "."), _clip(value))


def mark_error(s, message: str) -> None:
    s.set_status(Status(StatusCode.ERROR, message))
