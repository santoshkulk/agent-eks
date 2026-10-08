import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))
os.environ.setdefault("MEMORY_TABLE_NAME", "test-memory-table")
os.environ.setdefault("KB_PARAMETER_NAME", "/workshop/mortgage-assistant/bedrock/knowledge-base-id")
os.environ.setdefault("AWS_REGION", "us-west-2")

import mortgage_agent  # noqa: E402
import telemetry  # noqa: E402


class InitTelemetryTests(unittest.TestCase):
    def setUp(self) -> None:
        # init_telemetry() replaces the module-level tracer, so restore it or a
        # mocked tracer leaks into the tests that run afterwards.
        self._original_tracer = telemetry._tracer
        telemetry._initialized = False

    def tearDown(self) -> None:
        telemetry._initialized = False
        telemetry._tracer = self._original_tracer

    @patch("strands.telemetry.StrandsTelemetry")
    @patch("telemetry.trace.set_tracer_provider")
    def test_disabled_when_adot_is_not_running(
        self, mock_set_tracer_provider, strands_telemetry
    ) -> None:
        # Without opentelemetry-instrument the global provider is the API's proxy
        # provider, which has no add_span_processor: stay disabled, never create one.
        with patch("telemetry.trace.get_tracer_provider", return_value=object()):
            telemetry.init_telemetry()
        mock_set_tracer_provider.assert_not_called()
        strands_telemetry.assert_not_called()

    @patch("telemetry.trace.set_tracer_provider")
    def test_hands_the_adot_provider_to_strands_without_replacing_it(
        self, mock_set_tracer_provider
    ) -> None:
        # OpenTelemetry keeps only the first provider registered, so a second
        # one would silently drop the CloudWatch export ADOT set up.
        adot_provider = Mock()
        with patch(
            "telemetry.trace.get_tracer_provider", return_value=adot_provider
        ), patch("strands.telemetry.StrandsTelemetry") as strands_telemetry:
            telemetry.init_telemetry()
        mock_set_tracer_provider.assert_not_called()
        strands_telemetry.assert_called_once_with(tracer_provider=adot_provider)
        adot_provider.add_span_processor.assert_not_called()

    def test_runs_only_once(self) -> None:
        adot_provider = Mock()
        with patch(
            "telemetry.trace.get_tracer_provider", return_value=adot_provider
        ), patch("strands.telemetry.StrandsTelemetry") as strands_telemetry:
            telemetry.init_telemetry()
            telemetry.init_telemetry()
        strands_telemetry.assert_called_once()

    def test_configuration_failure_is_swallowed(self) -> None:
        with patch(
            "telemetry.trace.get_tracer_provider", return_value=Mock()
        ), patch("strands.telemetry.StrandsTelemetry", side_effect=RuntimeError("boom")):
            telemetry.init_telemetry()

    def test_module_does_not_use_langfuse_or_otlp_endpoint_configuration(self) -> None:
        source = (APP_DIR / "telemetry.py").read_text()
        self.assertNotIn("TELEMETRY_MASK_CONTENT", source)
        self.assertNotIn("setup_otlp_exporter", source)
        self.assertNotIn("TracerProvider(", source)
        self.assertNotIn("set_tracer_provider(", source)
        self.assertNotIn("x-langfuse", source)


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
