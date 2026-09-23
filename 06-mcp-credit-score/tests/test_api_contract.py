import os
from pathlib import Path
import sys
import unittest
from unittest.mock import ANY, patch

from fastapi.testclient import TestClient


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))
os.environ.setdefault("MEMORY_TABLE_NAME", "test-memory-table")
os.environ.setdefault("MORTGAGE_API_KEY", "test-api-key")
os.environ.setdefault(
    "CREDIT_SCORE_MCP_URL",
    "http://credit-score-mcp.credit-services.svc.cluster.local:8081/mcp",
)

import mortgage_agent  # noqa: E402
import mortgage_api  # noqa: E402


class ApiContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(mortgage_api.app)
        self.headers = {"Authorization": "Bearer test-api-key"}

    def test_canonical_model_and_knowledge_base_defaults(self) -> None:
        self.assertEqual(mortgage_agent.MODEL_ID, "us.anthropic.claude-sonnet-4-6")
        self.assertEqual(
            mortgage_agent.KB_PARAMETER_NAME,
            "/workshop/mortgage-assistant/bedrock/knowledge-base-id",
        )

    @patch("mortgage_api.run_prompt", return_value="remembered response")
    def test_invoke_returns_context_and_propagates_request_id(self, run_prompt) -> None:
        response = self.client.post(
            "/invoke",
            headers=self.headers,
            json={
                "prompt": "What did I tell you?",
                "actor_id": "participant-123456789012",
                "session_id": "session-1",
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["actor_id"], "participant-123456789012")
        self.assertEqual(body["session_id"], "session-1")
        self.assertEqual(body["response"], "remembered response")
        self.assertIn("trace_id", body)
        run_prompt.assert_called_once_with(
            "What did I tell you?",
            actor_id="participant-123456789012",
            session_id="session-1",
            request_id=ANY,
        )

    @patch("mortgage_api.telemetry.current_trace_id", return_value="a" * 32)
    @patch("mortgage_api.run_prompt", return_value="traced response")
    def test_invoke_returns_active_trace_id(self, _, __) -> None:
        response = self.client.post(
            "/invoke",
            headers=self.headers,
            json={
                "prompt": "Trace this",
                "actor_id": "participant-123456789012",
                "session_id": "session-1",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["trace_id"], "a" * 32)

    @patch("mortgage_api.run_prompt", side_effect=RuntimeError("provider unavailable"))
    def test_mcp_failure_returns_safe_request_error(self, _) -> None:
        response = self.client.post(
            "/invoke",
            headers=self.headers,
            json={
                "prompt": "General mortgage question",
                "actor_id": "participant-123456789012",
                "session_id": "session-1",
            },
        )
        self.assertEqual(response.status_code, 500)
        self.assertIn("request_id=", response.json()["detail"])
        self.assertNotIn("provider unavailable", response.text)

    def test_missing_or_invalid_bearer_token_is_rejected(self) -> None:
        request = {
            "prompt": "Hello",
            "actor_id": "participant-123456789012",
            "session_id": "session-1",
        }
        self.assertEqual(self.client.post("/invoke", json=request).status_code, 401)
        self.assertEqual(
            self.client.post(
                "/invoke",
                headers={"Authorization": "Bearer incorrect"},
                json=request,
            ).status_code,
            401,
        )

    def test_invalid_actor_is_rejected(self) -> None:
        response = self.client.post(
            "/invoke",
            headers=self.headers,
            json={
                "prompt": "Hello",
                "actor_id": "participant/other",
                "session_id": "session-1",
            },
        )
        self.assertEqual(response.status_code, 422)

    @patch("mortgage_api.get_knowledge_base_id", return_value="kb-test")
    def test_readiness_reports_mcp_without_live_discovery_or_url(self, _) -> None:
        response = self.client.get("/health/ready")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["credit_score_mcp"], "configured")
        self.assertNotIn("credit_score_mcp_url", body)
        self.assertNotIn("credit-score.test", response.text)


if __name__ == "__main__":
    unittest.main()
