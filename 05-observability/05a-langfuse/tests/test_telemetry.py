import os
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, Mock, patch

APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))
os.environ.setdefault("MEMORY_TABLE_NAME", "test-memory-table")
os.environ.setdefault("KB_PARAMETER_NAME", "/workshop/mortgage-assistant/bedrock/knowledge-base-id")
os.environ.setdefault("AWS_REGION", "us-west-2")

import mortgage_agent  # noqa: E402
import telemetry  # noqa: E402


class ParseHeadersTests(unittest.TestCase):
    def test_parses_langfuse_headers_and_preserves_padding(self) -> None:
        self.assertEqual(
            telemetry._parse_headers(
                "Authorization=Basic abc==,x-langfuse-ingestion-version=4"
            ),
            {
                "Authorization": "Basic abc==",
                "x-langfuse-ingestion-version": "4",
            },
        )

    def test_ignores_blank_and_malformed_entries(self) -> None:
        self.assertEqual(telemetry._parse_headers(" , key=value , malformed , "), {"key": "value"})


class RedactingSpanExporterTests(unittest.TestCase):
    @staticmethod
    def _real_span(attributes, event_attributes=None):
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        capture = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(capture))
        tracer = provider.get_tracer(__name__)
        with tracer.start_as_current_span("test-span") as span:
            for key, value in attributes.items():
                span.set_attribute(key, value)
            if event_attributes is not None:
                span.add_event("gen_ai.user.message", event_attributes)
        provider.shutdown()
        return capture.get_finished_spans()[0]

    def test_redacts_content_and_retains_operational_attributes(self) -> None:
        span = self._real_span(
            {
                "gen_ai.input.messages": "secret prompt",
                "gen_ai.output.messages": "secret response",
                "gen_ai.tool.call.arguments": "secret customer ID",
                "gen_ai.tool.call.result": "secret result",
                "tool.status": "success",
            },
            {"content": "secret event", "role": "user"},
        )
        redacted = telemetry._RedactingSpanExporter(object())._redact(span)
        self.assertIsNotNone(redacted)
        for key in (
            "gen_ai.input.messages",
            "gen_ai.output.messages",
            "gen_ai.tool.call.arguments",
            "gen_ai.tool.call.result",
        ):
            self.assertEqual(redacted.attributes[key], "[redacted]")
        self.assertEqual(redacted.attributes["tool.status"], "success")
        self.assertEqual(redacted.events[0].attributes["content"], "[redacted]")
        self.assertEqual(redacted.events[0].attributes["role"], "user")

    def test_export_force_flush_and_shutdown_delegate(self) -> None:
        span = self._real_span({"gen_ai.input.messages": "secret"})
        wrapped = Mock()
        wrapped.export.return_value = "export-result"
        wrapped.force_flush.return_value = True
        exporter = telemetry._RedactingSpanExporter(wrapped)
        self.assertEqual(exporter.export([span]), "export-result")
        exported_spans = wrapped.export.call_args.args[0]
        self.assertEqual(exported_spans[0].attributes["gen_ai.input.messages"], "[redacted]")
        self.assertTrue(exporter.force_flush(123))
        wrapped.force_flush.assert_called_once_with(123)
        exporter.shutdown()
        wrapped.shutdown.assert_called_once()


class InitTelemetryTests(unittest.TestCase):
    def setUp(self) -> None:
        telemetry._initialized = False

    def tearDown(self) -> None:
        telemetry._initialized = False

    def test_native_redaction_overrides_unredacted_patterns(self) -> None:
        with patch.dict(
            os.environ,
            {"OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental,gen_ai_unredacted_attributes=gen_ai.input.*"},
        ):
            telemetry._enable_native_strands_redaction()
            value = os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"]
            self.assertIn("gen_ai_latest_experimental", value)
            self.assertIn("gen_ai_unredacted_attributes=", value)
            self.assertNotIn("gen_ai.input.*", value)

    def test_missing_endpoint_is_noop_and_initializes_once(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=False),
            patch("telemetry.trace.set_tracer_provider") as set_provider,
        ):
            os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
            telemetry.init_telemetry()
            telemetry.init_telemetry()
        set_provider.assert_not_called()
        self.assertTrue(telemetry._initialized)

    @patch("strands.telemetry.StrandsTelemetry.setup_otlp_exporter")
    @patch("telemetry.trace.set_tracer_provider")
    def test_registers_one_provider_and_otlp_http_exporter(self, set_provider, setup) -> None:
        with patch.dict(os.environ, {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://example.invalid:4318"}):
            telemetry.init_telemetry()
            telemetry.init_telemetry()
        set_provider.assert_called_once()
        setup.assert_called_once()

    def test_masked_path_wraps_otlp_http_exporter(self) -> None:
        wrapped = Mock()
        processor = Mock()
        provider = MagicMock()
        with (
            patch.dict(
                os.environ,
                {
                    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://example.invalid:4318",
                    "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Basic abc==",
                    "TELEMETRY_MASK_CONTENT": "true",
                },
            ),
            patch("telemetry.TracerProvider", return_value=provider),
            patch("telemetry.trace.set_tracer_provider"),
            patch("strands.telemetry.StrandsTelemetry"),
            patch(
                "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter",
                return_value=wrapped,
            ) as exporter,
            patch("opentelemetry.sdk.trace.export.BatchSpanProcessor", return_value=processor),
        ):
            telemetry.init_telemetry()
        exporter.assert_called_once_with(
            endpoint="http://example.invalid:4318",
            headers={"Authorization": "Basic abc=="},
        )
        added = provider.add_span_processor.call_args.args[0]
        self.assertIs(added, processor)

    def test_initialization_failure_never_raises(self) -> None:
        with (
            patch.dict(os.environ, {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://example.invalid"}),
            patch("telemetry.TracerProvider", side_effect=RuntimeError("broken")),
        ):
            telemetry.init_telemetry()
        self.assertTrue(telemetry._initialized)


class TraceContextTests(unittest.TestCase):
    def tearDown(self) -> None:
        telemetry.set_current_trace_attributes(None)

    def test_attributes_have_session_user_and_request_tag(self) -> None:
        self.assertEqual(
            telemetry.trace_attributes("actor-1", "session-1", "request-1"),
            {
                "session.id": "session-1",
                "user.id": "actor-1",
                "tags": ["mortgage-assistant", "request:request-1"],
            },
        )

    def test_context_is_isolated_and_restored_after_success_and_failure(self) -> None:
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

    def test_current_trace_id_is_hex_nullable_and_fail_open(self) -> None:
        span = Mock()
        span.get_span_context.return_value = Mock(trace_id=15)
        with patch("telemetry.trace.get_current_span", return_value=span):
            self.assertEqual(telemetry.current_trace_id(), "0" * 31 + "f")
        span.get_span_context.return_value = Mock(trace_id=0)
        with patch("telemetry.trace.get_current_span", return_value=span):
            self.assertIsNone(telemetry.current_trace_id())
        with patch("telemetry.trace.get_current_span", side_effect=RuntimeError("broken")):
            self.assertIsNone(telemetry.current_trace_id())

    def test_safe_span_does_not_suppress_application_or_tracer_errors(self) -> None:
        with self.assertRaisesRegex(ValueError, "boom"):
            with telemetry.start_request_span("test-span"):
                raise ValueError("boom")
        broken_tracer = Mock()
        broken_tracer.start_as_current_span.side_effect = RuntimeError("broken")
        with patch.object(telemetry, "_tracer", broken_tracer):
            with telemetry.start_request_span("test-span"):
                pass


class ShutdownTests(unittest.TestCase):
    def test_shutdown_flushes_provider_and_never_raises(self) -> None:
        provider = Mock()
        with patch("telemetry.trace.get_tracer_provider", return_value=provider):
            telemetry.shutdown_telemetry()
        provider.shutdown.assert_called_once()
        provider.shutdown.side_effect = RuntimeError("broken")
        telemetry.shutdown_telemetry()


class SourceTelemetryContractTests(unittest.TestCase):
    def test_api_initializes_telemetry_before_importing_agent(self) -> None:
        api_source = (APP_DIR / "mortgage_api.py").read_text()
        self.assertLess(
            api_source.index("telemetry.init_telemetry()"),
            api_source.index("from mortgage_agent import"),
        )

    def test_custom_fault_event_is_absent(self) -> None:
        telemetry_source = (APP_DIR / "telemetry.py").read_text()
        agent_source = (APP_DIR / "mortgage_agent.py").read_text()
        self.assertNotIn("record_fault_injection", telemetry_source)
        self.assertNotIn("fault_injection", telemetry_source)
        self.assertNotIn("FAULT_INJECTION", agent_source)
        self.assertNotIn("maybe_inject_fault", agent_source)


if __name__ == "__main__":
    unittest.main()
