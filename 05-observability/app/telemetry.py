"""Export Strands agent traces to a self-hosted Langfuse instance over OTLP/HTTP.

This module configures OpenTelemetry exactly once per process, as early as
possible, using Strands Agents' native OTLP integration
(https://strandsagents.com/docs/user-guide/observability-evaluation/traces/).
There is a single exporter configuration; there is no separate console
exporter, X-Ray exporter, or second tracer provider anywhere in the
application.

Configuration is entirely environment-driven and optional:

- OTEL_EXPORTER_OTLP_ENDPOINT   Langfuse OTLP/HTTP endpoint, for example
                                 http://<langfuse-host>:3000/api/public/otel
- OTEL_EXPORTER_OTLP_HEADERS    "Authorization=Basic <base64>,x-langfuse-ingestion-version=4"
- OTEL_SERVICE_NAME             Resource service name (default: mortgage-assistant)
- TELEMETRY_MASK_CONTENT        "true" to redact prompt/response attribute
                                 values before spans are exported (see below)

If OTEL_EXPORTER_OTLP_ENDPOINT is not set, tracing is disabled entirely: no
exporter is created and no network calls are attempted. Any failure while
configuring or using telemetry is logged and swallowed; it never blocks
request handling or readiness.

Prompt and response capture
----------------------------
Strands' automatic instrumentation records prompt and response text as span
attributes such as `gen_ai.user.message`, `gen_ai.assistant.message`,
`gen_ai.choice`, and `system_prompt`. Those attributes are visible in
Langfuse alongside latency, token usage, and tool calls. For this workshop's
mock data that visibility is the point of the lab. A deployment handling real
customer data should choose one of:

- Leave OTEL_EXPORTER_OTLP_ENDPOINT unset to disable tracing entirely.
- Set TELEMETRY_MASK_CONTENT=true so this module strips the prompt/response
  attribute values (see `_CONTENT_ATTRIBUTES`) before any span leaves the
  process. Latency, token counts, tool names, and error status are still
  exported; the conversational content is not.
- Apply redaction or access controls further downstream in the
  observability backend instead of, or in addition to, the above.
"""

from __future__ import annotations

import contextvars
import logging
import os
import typing
from contextlib import AbstractContextManager
from types import TracebackType

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult


logger = logging.getLogger("telemetry")

_trace_attributes_var: contextvars.ContextVar[dict[str, object] | None] = (
    contextvars.ContextVar("trace_attributes", default=None)
)

_CONTENT_ATTRIBUTES = (
    "gen_ai.user.message",
    "gen_ai.assistant.message",
    "gen_ai.choice",
    "gen_ai.choice.message",
    "gen_ai.choice.tool.result",
    "system_prompt",
)

_initialized = False
_tracer = trace.get_tracer(__name__)


def _mask_content_enabled() -> bool:
    return os.environ.get("TELEMETRY_MASK_CONTENT", "false").strip().lower() == "true"


def _parse_headers(raw: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        key, _, value = pair.partition("=")
        headers[key.strip()] = value.strip()
    return headers


class _RedactingSpanExporter(SpanExporter):
    """Wrap a real span exporter, redacting prompt/response attributes.

    A `SpanProcessor.on_end` hook cannot do this redaction: once a span has
    ended, the OTel SDK makes its `ReadableSpan.attributes` immutable (item
    assignment raises `TypeError`), specifically so processors only read
    finished spans rather than mutate them. Wrapping the exporter instead
    rebuilds each span with masked attribute values -- using only public
    `ReadableSpan` fields -- immediately before the real exporter turns it
    into an OTLP request; this is the last point before the data leaves the
    process. Never raises; a failure to redact must not prevent export or
    crash the process.
    """

    def __init__(self, wrapped: SpanExporter) -> None:
        self._wrapped = wrapped

    def export(self, spans: typing.Sequence[ReadableSpan]) -> SpanExportResult:
        return self._wrapped.export([self._redact(span) for span in spans])

    def _redact(self, span: ReadableSpan) -> ReadableSpan:
        try:
            attributes = dict(span.attributes or {})
        except Exception:
            return span
        if not any(key in attributes for key in _CONTENT_ATTRIBUTES):
            return span
        for key in _CONTENT_ATTRIBUTES:
            if key in attributes:
                attributes[key] = "[redacted]"
        try:
            return ReadableSpan(
                name=span.name,
                context=span.context,
                parent=span.parent,
                resource=span.resource,
                attributes=attributes,
                events=span.events,
                links=span.links,
                kind=span.kind,
                status=span.status,
                start_time=span.start_time,
                end_time=span.end_time,
                instrumentation_scope=getattr(span, "instrumentation_scope", None),
            )
        except Exception:
            logger.debug("Unable to redact span %s; exporting it unmasked.", span.name)
            return span

    def shutdown(self) -> None:
        self._wrapped.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        force_flush = getattr(self._wrapped, "force_flush", None)
        if callable(force_flush):
            return bool(force_flush(timeout_millis))
        return True


def init_telemetry() -> None:
    """Configure OpenTelemetry tracing once per process. Never raises."""
    global _initialized, _tracer
    if _initialized:
        return
    _initialized = True

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        logger.info("OTEL_EXPORTER_OTLP_ENDPOINT is not set; tracing is disabled.")
        return

    try:
        from strands.telemetry import StrandsTelemetry

        tracer_provider = TracerProvider()

        # Passing a pre-built tracer_provider makes StrandsTelemetry skip its
        # own global registration step, so it must be registered here
        # instead. Both Strands' own agent/model/tool instrumentation and
        # this module's start_request_span() look up the tracer through
        # opentelemetry.trace's global provider, not through the local
        # `tracer_provider` variable; without this call neither one would
        # ever reach the OTLP exporter attached below.
        trace.set_tracer_provider(tracer_provider)

        strands_telemetry = StrandsTelemetry(tracer_provider=tracer_provider)
        headers = _parse_headers(os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", ""))
        if _mask_content_enabled():
            # StrandsTelemetry.setup_otlp_exporter() only builds an
            # unwrapped OTLPSpanExporter, with no hook to redact attributes
            # first. Constructing the same exporter here and wrapping it in
            # _RedactingSpanExporter is the only way to mask content before
            # it leaves the process; see that class's docstring for why a
            # SpanProcessor cannot do this instead.
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            otlp_exporter = OTLPSpanExporter(endpoint=endpoint, headers=headers)
            tracer_provider.add_span_processor(
                BatchSpanProcessor(_RedactingSpanExporter(otlp_exporter))
            )
        else:
            strands_telemetry.setup_otlp_exporter(endpoint=endpoint, headers=headers)
        _tracer = trace.get_tracer(__name__)
        logger.info("OTLP tracing configured for endpoint %s", endpoint)
    except Exception:
        logger.exception("Failed to configure OTLP tracing; continuing without it.")


def trace_attributes(
    actor_id: str,
    session_id: str,
    request_id: str,
) -> dict[str, object]:
    """Build the Strands `trace_attributes` mapping used by every agent.

    Applying the same mapping to the supervisor and every specialist agent
    keeps actor/session grouping consistent across the whole request, and
    across both EKS replicas, since grouping in Langfuse is derived from
    these span attributes rather than from any in-process state.
    """
    return {
        "session.id": session_id,
        "user.id": actor_id,
        "tags": ["mortgage-assistant", f"request:{request_id}"],
    }


def set_current_trace_attributes(attributes: dict[str, object] | None) -> None:
    """Store this request's trace attributes for the current task/thread.

    Specialist agents are created inside `@tool` functions, which only
    receive the arguments the supervisor's model chooses to pass. This
    context variable is how those functions recover the actor/session/
    request attributes for the request they are part of, without needing
    them threaded through every tool signature.
    """
    _trace_attributes_var.set(attributes)


def current_trace_attributes() -> dict[str, object] | None:
    """Return the trace attributes set by `set_current_trace_attributes`."""
    return _trace_attributes_var.get()


def current_trace_id() -> str | None:
    """Return the 32-hex-character trace ID of the active span, if any."""
    context = trace.get_current_span().get_span_context()
    if context is None or context.trace_id == 0:
        return None
    return format(context.trace_id, "032x")


class _SafeSpan(AbstractContextManager):
    """Start a span without letting telemetry errors reach the caller.

    `__enter__` never raises: if starting the span fails, it logs and yields
    `None` so callers can proceed without tracing. `__exit__` delegates to
    the real span's exit so exceptions raised by the caller's own code are
    never suppressed, but any error while closing the span itself is caught
    and logged instead of replacing the caller's exception.
    """

    def __init__(self, name: str, attributes: dict[str, object] | None = None) -> None:
        self._name = name
        self._attributes = attributes or {}
        self._cm: AbstractContextManager | None = None
        self.span: object | None = None

    def __enter__(self) -> object | None:
        try:
            self._cm = _tracer.start_as_current_span(self._name)
            self.span = self._cm.__enter__()
            for key, value in self._attributes.items():
                try:
                    self.span.set_attribute(key, value)  # type: ignore[union-attr]
                except Exception:
                    logger.debug("Unable to set span attribute %s", key)
        except Exception:
            logger.exception("Failed to start telemetry span %s", self._name)
            self._cm = None
            self.span = None
        return self.span

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        if self._cm is None:
            return False
        try:
            return bool(self._cm.__exit__(exc_type, exc, tb))
        except Exception:
            logger.exception("Failed to close telemetry span %s", self._name)
            return False


def start_request_span(
    name: str,
    attributes: dict[str, object] | None = None,
) -> _SafeSpan:
    """Start a root span for one API request.

    Safe to use even if tracing was never configured: OpenTelemetry's
    default no-op tracer still yields a valid, non-recording span object in
    that case, so callers can set attributes on it unconditionally. Nothing
    is exported and no network calls are made unless `init_telemetry` set up
    a real exporter first.
    """
    return _SafeSpan(name, attributes)


def record_fault_injection(tool_name: str, mode: str) -> None:
    """Add a visible marker to the active span when a fault is injected."""
    try:
        trace.get_current_span().add_event(
            "fault_injection",
            {"tool.name": tool_name, "fault.mode": mode},
        )
    except Exception:
        logger.debug("Unable to record fault-injection span event.")


def shutdown_telemetry() -> None:
    """Flush and shut down the tracer provider. Never raises."""
    try:
        provider = trace.get_tracer_provider()
        shutdown = getattr(provider, "shutdown", None)
        if callable(shutdown):
            shutdown()
    except Exception:
        logger.exception("Failed to shut down the tracer provider cleanly.")
