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


class InitTelemetryTests(unittest.TestCase):
    def setUp(self) -> None:
        # init_telemetry() replaces the module-level tracer, so restore it or a
        # mocked tracer leaks into the tests that run afterwards.
        self._original_tracer = telemetry._tracer
        telemetry._initialized = False

    def tearDown(self) -> None:
        telemetry._tracer = self._original_tracer
        telemetry._initialized = False

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
        # No ADOT is not running in the test environment, so
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
