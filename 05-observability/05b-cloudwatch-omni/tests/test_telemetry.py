from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

import telemetry  # noqa: E402


class InitTelemetryTests(unittest.TestCase):
    def setUp(self) -> None:
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
        with patch("telemetry.trace.get_tracer_provider", return_value=object()):
            telemetry.init_telemetry()
        mock_set_tracer_provider.assert_not_called()
        strands_telemetry.assert_not_called()

    @patch("telemetry.trace.set_tracer_provider")
    def test_hands_the_adot_provider_to_strands_without_replacing_it(
        self, mock_set_tracer_provider
    ) -> None:
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

    def test_module_does_not_own_provider_exporter_or_langfuse_configuration(self) -> None:
        source = (APP_DIR / "telemetry.py").read_text()
        for absent in (
            "TELEMETRY_MASK_CONTENT",
            "setup_otlp_exporter",
            "TracerProvider(",
            "set_tracer_provider(",
            "x-langfuse",
            'os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"',
        ):
            self.assertNotIn(absent, source)


class TraceContextTests(unittest.TestCase):
    def tearDown(self) -> None:
        telemetry.set_current_trace_attributes(None)

    def test_attributes_round_trip_and_can_be_cleared(self) -> None:
        attributes = telemetry.trace_attributes("actor-1", "session-1", "request-1")
        telemetry.set_current_trace_attributes(attributes)
        self.assertEqual(telemetry.current_trace_attributes(), attributes)
        telemetry.set_current_trace_attributes(None)
        self.assertIsNone(telemetry.current_trace_attributes())

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

    def test_trace_id_lookup_failure_is_fail_open(self) -> None:
        with patch("telemetry.trace.get_current_span", side_effect=RuntimeError("boom")):
            self.assertIsNone(telemetry.current_trace_id())

    def test_fault_helper_and_event_are_absent(self) -> None:
        source = (APP_DIR / "telemetry.py").read_text()
        self.assertNotIn("record_fault_injection", source)
        self.assertNotIn("fault_injection", source)
        self.assertNotIn("add_event(", source)


if __name__ == "__main__":
    unittest.main()
