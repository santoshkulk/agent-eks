import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))
os.environ.setdefault("MEMORY_TABLE_NAME", "test-memory-table")
os.environ.setdefault("MORTGAGE_API_KEY", "test-api-key")

import mortgage_agent  # noqa: E402
import mortgage_api  # noqa: E402


class ApiContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(mortgage_api.app)
        self.headers = {"Authorization": "Bearer test-api-key"}

    def _body(self, **extra):
        return {
            "prompt": "What did I tell you?",
            "actor_id": "participant-123456789012",
            "session_id": "session-1",
            **extra,
        }

    @patch("mortgage_api.telemetry.current_trace_id", return_value="a" * 32)
    @patch("mortgage_api.uuid.uuid4", return_value="generated-request")
    @patch("mortgage_api.run_supervisor", return_value="remembered response")
    def test_invoke_returns_reduced_traced_contract(self, run, _, __) -> None:
        response = self.client.post("/invoke", headers=self.headers, json=self._body())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            set(body),
            {"request_id", "actor_id", "session_id", "response", "duration_ms", "trace_id"},
        )
        self.assertEqual(body["request_id"], "generated-request")
        self.assertEqual(body["trace_id"], "a" * 32)
        run.assert_called_once_with(
            "What did I tell you?",
            "participant-123456789012",
            "session-1",
            "generated-request",
        )

    @patch("mortgage_api.run_supervisor", return_value="ok")
    def test_root_span_has_safe_name_and_correlation_attributes(self, _) -> None:
        context_manager = MagicMock()
        with (
            patch("mortgage_api.uuid.uuid4", return_value="request-1"),
            patch("mortgage_api.telemetry.start_request_span", return_value=context_manager) as span,
        ):
            self.client.post("/invoke", headers=self.headers, json=self._body())
        span.assert_called_once_with(
            "mortgage_assistant.invoke",
            mortgage_api.telemetry.trace_attributes(
                "participant-123456789012",
                "session-1",
                "request-1",
            ),
        )

    @patch("mortgage_api.telemetry.current_trace_id", return_value=None)
    @patch("mortgage_api.run_supervisor", return_value="ok")
    def test_telemetry_disabled_returns_nullable_trace_id(self, _, __) -> None:
        response = self.client.post("/invoke", headers=self.headers, json=self._body())
        self.assertIsNone(response.json()["trace_id"])

    @patch("mortgage_api.run_supervisor", side_effect=RuntimeError("provider secret"))
    def test_failure_returns_safe_error_with_correlation_id(self, _) -> None:
        with patch("mortgage_api.uuid.uuid4", return_value="generated-request"):
            response = self.client.post("/invoke", headers=self.headers, json=self._body())
        self.assertEqual(response.status_code, 500)
        self.assertIn("request_id=generated-request", response.json()["detail"])
        self.assertNotIn("provider secret", response.text)
        self.assertNotIn("resume", response.text.lower())

    def test_missing_invalid_and_non_ascii_tokens_are_rejected(self) -> None:
        self.assertEqual(self.client.post("/invoke", json=self._body()).status_code, 401)
        self.assertEqual(
            self.client.post(
                "/invoke", headers={"Authorization": "Bearer incorrect"}, json=self._body()
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.post(
                "/invoke",
                headers={"Authorization": "Bearer ключ".encode("utf-8")},
                json=self._body(),
            ).status_code,
            401,
        )

    def test_inputs_are_bounded_and_client_request_id_is_forbidden(self) -> None:
        for body in (
            self._body(prompt=""),
            self._body(prompt="x" * 4001),
            self._body(actor_id="participant/other"),
            self._body(session_id="x" * 129),
            self._body(request_id="removed-client-id"),
        ):
            with self.subTest(body=body):
                self.assertEqual(
                    self.client.post("/invoke", headers=self.headers, json=body).status_code,
                    422,
                )

    def test_agent_slots_are_capped_with_429(self) -> None:
        slots = threading.BoundedSemaphore(1)
        slots.acquire()
        with patch.object(mortgage_api, "AGENT_SLOTS", slots):
            response = self.client.post("/invoke", json=self._body(), headers=self.headers)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["retry-after"], "5")

    def test_only_health_readiness_and_invoke_routes_exist(self) -> None:
        routes = {
            (route.path, method)
            for route in mortgage_api.app.routes
            for method in getattr(route, "methods", set())
            if route.path not in {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
        }
        self.assertEqual(
            routes,
            {("/health", "GET"), ("/health/ready", "GET"), ("/invoke", "POST")},
        )

    @patch("mortgage_api.get_knowledge_base_id", return_value="kb-test")
    def test_health_and_readiness_report_configuration(self, _) -> None:
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
        body = self.client.get("/health/ready").json()
        self.assertEqual(body["knowledge_base_id"], "kb-test")
        self.assertEqual(body["memory_table"], "test-memory-table")
        self.assertNotIn("credit_score_mcp", body)


if __name__ == "__main__":
    unittest.main()
