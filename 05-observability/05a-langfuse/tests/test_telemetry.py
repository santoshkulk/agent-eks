import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))
os.environ.setdefault("MEMORY_TABLE_NAME", "test-memory-table")

import mortgage_agent  # noqa: E402
import telemetry  # noqa: E402


class ParseHeadersTests(unittest.TestCase):
    def test_parses_comma_separated_pairs(self) -> None:
        headers = telemetry._parse_headers(
            "Authorization=Basic abc123,x-langfuse-ingestion-version=4"
        )
        self.assertEqual(
            headers,
            {"Authorization": "Basic abc123", "x-langfuse-ingestion-version": "4"},
        )

    def test_ignores_blank_and_malformed_entries(self) -> None:
        headers = telemetry._parse_headers(" , key=value , malformed , ")
        self.assertEqual(headers, {"key": "value"})

    def test_empty_string_yields_no_headers(self) -> None:
        self.assertEqual(telemetry._parse_headers(""), {})


class MaskContentEnabledTests(unittest.TestCase):
    def test_defaults_to_disabled(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TELEMETRY_MASK_CONTENT", None)
            self.assertFalse(telemetry._mask_content_enabled())

    def test_true_variants_are_case_insensitive(self) -> None:
        for value in ("true", "True", " TRUE "):
            with patch.dict(os.environ, {"TELEMETRY_MASK_CONTENT": value}):
                self.assertTrue(telemetry._mask_content_enabled())

    def test_other_values_are_disabled(self) -> None:
        with patch.dict(os.environ, {"TELEMETRY_MASK_CONTENT": "false"}):
            self.assertFalse(telemetry._mask_content_enabled())


class RedactingSpanExporterTests(unittest.TestCase):
    """Exercise _RedactingSpanExporter against real, ended SDK spans.

    Regression coverage for a bug caught only by manual smoke testing: once
    a span has ended, the OTel SDK makes `ReadableSpan.attributes` immutable,
    so a `SpanProcessor.on_end` hook that tries to mutate attributes in place
    (the original design of this module) silently fails -- item assignment
    raises `TypeError`, which the processor's own `try/except` swallows. A
    `SimpleNamespace(attributes={...})` fake, as used in earlier revisions of
    this test, does not catch that because a plain dict allows the mutation
    that a real span would refuse. Building real spans through a
    `TracerProvider` here ensures redaction is verified against the SDK's
    actual immutability behavior.
    """

    @staticmethod
    def _real_span(attributes: dict[str, str]) -> object:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        capture = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(capture))
        tracer = provider.get_tracer(__name__)
        with tracer.start_as_current_span("test-span") as span:
            for key, value in attributes.items():
                span.set_attribute(key, value)
        return capture.get_finished_spans()[0]

    def test_redacts_known_content_attributes_only(self) -> None:
        span = self._real_span(
            {
                "gen_ai.user.message": "secret prompt",
                "gen_ai.choice": "secret response",
                "tool.status": "success",
            }
        )
        redacted = telemetry._RedactingSpanExporter(object())._redact(span)
        self.assertEqual(redacted.attributes["gen_ai.user.message"], "[redacted]")
        self.assertEqual(redacted.attributes["gen_ai.choice"], "[redacted]")
        self.assertEqual(redacted.attributes["tool.status"], "success")

    def test_span_without_content_attributes_is_returned_unchanged(self) -> None:
        span = self._real_span({"tool.status": "success"})
        redacted = telemetry._RedactingSpanExporter(object())._redact(span)
        self.assertIs(redacted, span)

    def test_export_sends_redacted_spans_to_the_wrapped_exporter(self) -> None:
        span = self._real_span({"gen_ai.user.message": "secret"})
        wrapped = Mock()
        wrapped.export.return_value = "export-result"
        result = telemetry._RedactingSpanExporter(wrapped).export([span])
        self.assertEqual(result, "export-result")
        (exported_spans,), _ = wrapped.export.call_args
        self.assertEqual(exported_spans[0].attributes["gen_ai.user.message"], "[redacted]")

    def test_shutdown_and_force_flush_delegate_to_the_wrapped_exporter(self) -> None:
        wrapped = Mock()
        wrapped.force_flush.return_value = True
        exporter = telemetry._RedactingSpanExporter(wrapped)
        exporter.shutdown()
        wrapped.shutdown.assert_called_once()
        self.assertTrue(exporter.force_flush(1000))
        wrapped.force_flush.assert_called_once_with(1000)


class InitTelemetryTests(unittest.TestCase):
    def tearDown(self) -> None:
        telemetry._initialized = False

    @patch("strands.telemetry.StrandsTelemetry.setup_otlp_exporter")
    @patch("telemetry.trace.set_tracer_provider")
    def test_registers_the_custom_tracer_provider_globally(
        self, mock_set_tracer_provider, mock_setup_otlp_exporter
    ) -> None:
        # Regression test: StrandsTelemetry only registers a tracer provider
        # globally itself when no tracer_provider is passed to its
        # constructor. Because a custom tracer_provider is required here (to
        # insert the redacting span processor ahead of the OTLP processor),
        # init_telemetry must register it globally itself -- otherwise
        # neither Strands' own agent/tool/model instrumentation nor this
        # module's own tracer would ever reach the OTLP exporter.
        telemetry._initialized = False
        with patch.dict(
            os.environ,
            {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://example.invalid:4318"},
        ):
            telemetry.init_telemetry()
        mock_set_tracer_provider.assert_called_once()
        mock_setup_otlp_exporter.assert_called_once()


class TraceIdTests(unittest.TestCase):
    def test_returns_none_without_an_active_span(self) -> None:
        self.assertIsNone(telemetry.current_trace_id())


class TraceAttributesContextTests(unittest.TestCase):
    def test_round_trips_through_the_context_variable(self) -> None:
        attributes = {"session.id": "s1", "user.id": "u1", "tags": ["x"]}
        telemetry.set_current_trace_attributes(attributes)
        self.assertEqual(telemetry.current_trace_attributes(), attributes)
        telemetry.set_current_trace_attributes(None)
        self.assertIsNone(telemetry.current_trace_attributes())


class StartRequestSpanTests(unittest.TestCase):
    def test_context_manager_does_not_raise_without_configured_tracing(self) -> None:
        # No OTEL_EXPORTER_OTLP_ENDPOINT is set in the test environment, so
        # this exercises OpenTelemetry's default no-op tracer path.
        with telemetry.start_request_span("test-span", {"key": "value"}):
            pass

    def test_exceptions_from_caller_are_not_suppressed(self) -> None:
        with self.assertRaises(ValueError):
            with telemetry.start_request_span("test-span"):
                raise ValueError("boom")


class FaultInjectionTests(unittest.TestCase):
    def test_disabled_by_default_is_a_no_op(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FAULT_INJECTION_ENABLED", None)
            mortgage_agent.maybe_inject_fault("get_mortgage_details")

    def test_ignores_calls_for_a_different_tool(self) -> None:
        with patch.dict(
            os.environ,
            {
                "FAULT_INJECTION_ENABLED": "true",
                "FAULT_INJECTION_TOOL": "get_mortgage_details",
                "FAULT_INJECTION_MODE": "error",
            },
        ):
            mortgage_agent.maybe_inject_fault("some_other_tool")

    def test_error_mode_raises_for_the_targeted_tool(self) -> None:
        with patch.dict(
            os.environ,
            {
                "FAULT_INJECTION_ENABLED": "true",
                "FAULT_INJECTION_TOOL": "get_mortgage_details",
                "FAULT_INJECTION_MODE": "error",
            },
        ):
            with self.assertRaises(RuntimeError):
                mortgage_agent.maybe_inject_fault("get_mortgage_details")

    @patch("mortgage_agent.time.sleep")
    def test_delay_mode_sleeps_for_the_configured_duration(self, sleep) -> None:
        with patch.dict(
            os.environ,
            {
                "FAULT_INJECTION_ENABLED": "true",
                "FAULT_INJECTION_TOOL": "get_mortgage_details",
                "FAULT_INJECTION_MODE": "delay",
                "FAULT_INJECTION_DELAY_SECONDS": "3",
            },
        ):
            mortgage_agent.maybe_inject_fault("get_mortgage_details")
        sleep.assert_called_once_with(3.0)


if __name__ == "__main__":
    unittest.main()
