import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))
os.environ.setdefault("MEMORY_TABLE_NAME", "test-memory-table")
os.environ.setdefault(
    "CREDIT_SCORE_MCP_URL",
    "http://credit-score-mcp.credit-services.svc.cluster.local:8081/mcp",
)

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
        self.assertEqual(
            telemetry._parse_headers(" , key=value , malformed , "),
            {"key": "value"},
        )


class MaskContentEnabledTests(unittest.TestCase):
    def test_defaults_to_disabled(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TELEMETRY_MASK_CONTENT", None)
            self.assertFalse(telemetry._mask_content_enabled())

    def test_true_is_case_insensitive(self) -> None:
        with patch.dict(os.environ, {"TELEMETRY_MASK_CONTENT": " TRUE "}):
            self.assertTrue(telemetry._mask_content_enabled())


class RedactingSpanExporterTests(unittest.TestCase):
    @staticmethod
    def _real_span(
        attributes: dict[str, str],
        event_attributes: dict[str, str] | None = None,
    ) -> object:
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
            if event_attributes is not None:
                span.add_event("gen_ai.user.message", event_attributes)
        return capture.get_finished_spans()[0]

    def test_redacts_content_and_retains_operational_attributes(self) -> None:
        span = self._real_span(
            {
                "gen_ai.input.messages": "secret prompt",
                "gen_ai.output.messages": "secret response",
                "gen_ai.tool.call.arguments": "secret customer ID",
                "gen_ai.tool.call.result": "secret score",
                "tool.status": "success",
            },
            event_attributes={
                "content": "secret prompt event",
                "role": "user",
            },
        )
        redacted = telemetry._RedactingSpanExporter(object())._redact(span)
        self.assertIsNotNone(redacted)
        self.assertEqual(redacted.attributes["gen_ai.input.messages"], "[redacted]")
        self.assertEqual(redacted.attributes["gen_ai.output.messages"], "[redacted]")
        self.assertEqual(
            redacted.attributes["gen_ai.tool.call.arguments"],
            "[redacted]",
        )
        self.assertEqual(redacted.attributes["gen_ai.tool.call.result"], "[redacted]")
        self.assertEqual(redacted.attributes["tool.status"], "success")
        self.assertEqual(redacted.events[0].attributes["content"], "[redacted]")
        self.assertEqual(redacted.events[0].attributes["role"], "user")

    def test_export_delegates_redacted_spans(self) -> None:
        span = self._real_span({"gen_ai.input.messages": "secret"})
        wrapped = Mock()
        wrapped.export.return_value = "export-result"
        result = telemetry._RedactingSpanExporter(wrapped).export([span])
        self.assertEqual(result, "export-result")
        (exported_spans,), _ = wrapped.export.call_args
        self.assertEqual(exported_spans[0].attributes["gen_ai.input.messages"], "[redacted]")


class InitTelemetryTests(unittest.TestCase):
    def tearDown(self) -> None:
        telemetry._initialized = False

    def test_native_redaction_overrides_unredacted_patterns(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OTEL_SEMCONV_STABILITY_OPT_IN": (
                    "gen_ai_latest_experimental,"
                    "gen_ai_unredacted_attributes=gen_ai.input.*"
                )
            },
        ):
            telemetry._enable_native_strands_redaction()
            value = os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"]
            self.assertIn("gen_ai_latest_experimental", value)
            self.assertIn("gen_ai_unredacted_attributes=", value)
            self.assertNotIn("gen_ai.input.*", value)

    @patch("strands.telemetry.StrandsTelemetry.setup_otlp_exporter")
    @patch("telemetry.trace.set_tracer_provider")
    def test_registers_provider_and_otlp_exporter(
        self,
        set_tracer_provider,
        setup_otlp_exporter,
    ) -> None:
        telemetry._initialized = False
        with patch.dict(
            os.environ,
            {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://example.invalid:4318"},
        ):
            telemetry.init_telemetry()
        set_tracer_provider.assert_called_once()
        setup_otlp_exporter.assert_called_once()


class TraceContextTests(unittest.TestCase):
    def tearDown(self) -> None:
        telemetry.set_current_trace_attributes(None)

    def test_attributes_round_trip_and_can_be_cleared(self) -> None:
        attributes = telemetry.trace_attributes("actor-1", "session-1", "request-1")
        telemetry.set_current_trace_attributes(attributes)
        self.assertEqual(telemetry.current_trace_attributes(), attributes)
        telemetry.set_current_trace_attributes(None)
        self.assertIsNone(telemetry.current_trace_attributes())

    def test_request_attributes_do_not_leak_after_replacement(self) -> None:
        first = telemetry.trace_attributes("actor-1", "session-1", "request-1")
        second = telemetry.trace_attributes("actor-2", "session-2", "request-2")
        telemetry.set_current_trace_attributes(first)
        telemetry.set_current_trace_attributes(second)
        self.assertEqual(telemetry.current_trace_attributes(), second)
        self.assertNotEqual(telemetry.current_trace_attributes(), first)

    def test_request_context_is_restored_after_success_and_failure(self) -> None:
        initial = {"session.id": "outer"}
        request = telemetry.trace_attributes("actor-1", "session-1", "request-1")
        telemetry.set_current_trace_attributes(initial)
        with telemetry.use_trace_attributes(request):
            self.assertEqual(telemetry.current_trace_attributes(), request)
        self.assertEqual(telemetry.current_trace_attributes(), initial)

        with self.assertRaisesRegex(ValueError, "boom"):
            with telemetry.use_trace_attributes(request):
                raise ValueError("boom")
        self.assertEqual(telemetry.current_trace_attributes(), initial)

    def test_safe_span_does_not_suppress_application_errors(self) -> None:
        with self.assertRaisesRegex(ValueError, "boom"):
            with telemetry.start_request_span("test-span"):
                raise ValueError("boom")


class FaultInjectionTests(unittest.TestCase):
    def test_disabled_by_default_is_no_op(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FAULT_INJECTION_ENABLED", None)
            mortgage_agent.maybe_inject_fault("get_mortgage_details")

    def test_error_mode_raises_for_targeted_tool(self) -> None:
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
    def test_delay_mode_uses_configured_duration(self, sleep) -> None:
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
