"""Export Strands agent traces to a self-hosted Langfuse instance over OTLP/HTTP.

This module configures OpenTelemetry exactly once per process, as early as
possible, using Strands Agents' native OTLP integration. There is one exporter
configuration and no separate console or X-Ray exporter.

Configuration is environment-driven and optional. If
OTEL_EXPORTER_OTLP_ENDPOINT is unset, tracing is disabled without making
network calls. Telemetry failures are logged and never block request handling
or readiness.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
import logging
import os
import typing
from contextlib import AbstractContextManager, contextmanager
from types import TracebackType

from opentelemetry import trace
from opentelemetry.sdk.trace import Event, ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult


logger = logging.getLogger("telemetry")

_trace_attributes_var: contextvars.ContextVar[dict[str, object] | None] = (
    contextvars.ContextVar("trace_attributes", default=None)
)

_CONTENT_ATTRIBUTES = (
    # Current OpenTelemetry semantic conventions emitted by Strands.
    "gen_ai.input.messages",
    "gen_ai.output.messages",
    "gen_ai.system_instructions",
    "gen_ai.tool.call.arguments",
    "gen_ai.tool.call.result",
    # Legacy names retained for compatibility with older Strands traces.
    "gen_ai.user.message",
    "gen_ai.assistant.message",
    "gen_ai.choice",
    "gen_ai.choice.message",
    "gen_ai.choice.tool.result",
    "system_prompt",
)

_GENERIC_CONTENT_ATTRIBUTES = frozenset(
    {"content", "message", "query", "results"}
)


def _redact_attributes(
    attributes: typing.Mapping[str, object] | None,
) -> dict[str, object]:
    """Copy attributes while masking known conversational and memory content."""
    redacted = dict(attributes or {})
    for key in redacted:
        if key in _CONTENT_ATTRIBUTES or key in _GENERIC_CONTENT_ATTRIBUTES:
            redacted[key] = "[redacted]"
    return redacted


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
    """Wrap an exporter and redact conversational span attributes."""

    def __init__(self, wrapped: SpanExporter) -> None:
        self._wrapped = wrapped

    def export(self, spans: typing.Sequence[ReadableSpan]) -> SpanExportResult:
        redacted_spans = [
            redacted
            for span in spans
            if (redacted := self._redact(span)) is not None
        ]
        return self._wrapped.export(redacted_spans)

    def _redact(self, span: ReadableSpan) -> ReadableSpan | None:
        try:
            attributes = _redact_attributes(span.attributes)
            events = tuple(
                Event(
                    name=event.name,
                    attributes=_redact_attributes(event.attributes),
                    timestamp=event.timestamp,
                )
                for event in span.events
            )
            return ReadableSpan(
                name=span.name,
                context=span.context,
                parent=span.parent,
                resource=span.resource,
                attributes=attributes,
                events=events,
                links=span.links,
                kind=span.kind,
                status=span.status,
                start_time=span.start_time,
                end_time=span.end_time,
                instrumentation_scope=getattr(span, "instrumentation_scope", None),
            )
        except Exception:
            logger.exception(
                "Dropping span %s because content masking failed",
                span.name,
            )
            return None

    def shutdown(self) -> None:
        self._wrapped.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        force_flush = getattr(self._wrapped, "force_flush", None)
        if callable(force_flush):
            return bool(force_flush(timeout_millis))
        return True


def _enable_native_strands_redaction() -> None:
    """Tell pinned Strands to redact every sensitive GenAI field at source."""
    key = "OTEL_SEMCONV_STABILITY_OPT_IN"
    tokens = [
        token.strip()
        for token in os.environ.get(key, "").split(",")
        if token.strip()
        and not token.strip().startswith("gen_ai_unredacted_attributes=")
    ]
    tokens.append("gen_ai_unredacted_attributes=")
    os.environ[key] = ",".join(tokens)


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
        if _mask_content_enabled():
            _enable_native_strands_redaction()

        from strands.telemetry import StrandsTelemetry

        tracer_provider = TracerProvider()
        trace.set_tracer_provider(tracer_provider)

        strands_telemetry = StrandsTelemetry(tracer_provider=tracer_provider)
        headers = _parse_headers(os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", ""))
        if _mask_content_enabled():
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
    """Build the trace attributes shared by every agent in one request."""
    return {
        "session.id": session_id,
        "user.id": actor_id,
        "tags": ["mortgage-assistant", f"request:{request_id}"],
    }


def set_current_trace_attributes(attributes: dict[str, object] | None) -> None:
    """Replace request trace attributes for the current context."""
    _trace_attributes_var.set(attributes)


@contextmanager
def use_trace_attributes(attributes: dict[str, object]) -> Iterator[None]:
    """Install request attributes temporarily and always restore the context."""
    token = _trace_attributes_var.set(attributes)
    try:
        yield
    finally:
        _trace_attributes_var.reset(token)


def current_trace_attributes() -> dict[str, object] | None:
    """Return trace attributes for the current request context."""
    return _trace_attributes_var.get()


def current_trace_id() -> str | None:
    """Return the active span's 32-character hexadecimal trace ID."""
    context = trace.get_current_span().get_span_context()
    if context is None or context.trace_id == 0:
        return None
    return format(context.trace_id, "032x")


class _SafeSpan(AbstractContextManager):
    """Start a span without allowing telemetry errors to reach callers."""

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
    """Start a safe root span for one API request."""
    return _SafeSpan(name, attributes)


def record_fault_injection(tool_name: str, mode: str) -> None:
    """Add a marker to the active span when a test fault is injected."""
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
