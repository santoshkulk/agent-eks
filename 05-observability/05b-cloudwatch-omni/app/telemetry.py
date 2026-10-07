"""Attach the Strands mortgage assistant to the ADOT tracer provider for CloudWatch Omni.

The application is started under `opentelemetry-instrument` with the AWS Distro
for OpenTelemetry (ADOT). The distro registers the process-wide tracer provider
and exports every span to CloudWatch over the regional X-Ray OTLP endpoint,
signed with the pod's EKS Pod Identity role. CloudWatch Omni reads those traces
through Transaction Search.

OpenTelemetry uses only the first tracer provider registered, so this module
never creates one. It hands the distro's provider to Strands so the agent,
model, and tool spans reach it, and it adds a root span per API request.

The CloudWatch export itself is configured by the ADOT environment variables in
k8s/service.template.yaml (AGENT_OBSERVABILITY_ENABLED, OTEL_*), not here.
OTEL_EXPORTER_OTLP_ENDPOINT must stay unset: ADOT would use it instead of the
X-Ray endpoint.

When ADOT is not running (for example in unit tests or a local run), tracing is
disabled: no exporter is created and no network calls are made. Any failure
while configuring or using telemetry is logged and swallowed; it never blocks
request handling or readiness.

Prompt and response capture
----------------------------
Strands records prompt and response text as span attributes such as
`gen_ai.user.message`, `gen_ai.assistant.message`, and `gen_ai.choice`.
AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT=true keeps that text on the spans so Agent
traces and evaluators in Omni can show it. Use only synthetic data in this lab.
For real customer data, leave that variable unset so ADOT moves content off the
spans, and see the CloudWatch Omni data protection guidance.
"""

from __future__ import annotations

import contextvars
import logging
from contextlib import AbstractContextManager
from types import TracebackType

from opentelemetry import trace


logger = logging.getLogger("telemetry")

_trace_attributes_var: contextvars.ContextVar[dict[str, object] | None] = (
    contextvars.ContextVar("trace_attributes", default=None)
)

_initialized = False
_tracer = trace.get_tracer(__name__)


def init_telemetry() -> None:
    """Hand the ADOT tracer provider to Strands, once per process. Never raises."""
    global _initialized, _tracer
    if _initialized:
        return
    _initialized = True

    try:
        provider = trace.get_tracer_provider()
        # ADOT (opentelemetry-instrument) has already registered a real
        # provider. Without it there is nothing to export to, so stay disabled
        # rather than creating a second provider.
        if not hasattr(provider, "add_span_processor"):
            logger.info("ADOT is not running; tracing is disabled.")
            return

        from strands.telemetry import StrandsTelemetry

        # Passing the provider makes StrandsTelemetry skip its own global
        # registration; a provider of its own would never export.
        StrandsTelemetry(tracer_provider=provider)
        _tracer = trace.get_tracer(__name__)
        logger.info("Tracing configured: spans are exported to CloudWatch by ADOT.")
    except Exception:
        logger.exception("Failed to configure tracing; continuing without it.")


def trace_attributes(
    actor_id: str,
    session_id: str,
    request_id: str,
) -> dict[str, object]:
    """Build the Strands `trace_attributes` mapping used by every agent.

    Applying the same mapping to the supervisor and every specialist agent
    keeps actor/session grouping consistent across the whole request, and
    across both EKS replicas, since grouping in CloudWatch Omni is derived from
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
