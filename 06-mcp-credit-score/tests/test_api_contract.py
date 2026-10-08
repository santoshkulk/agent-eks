import os
from pathlib import Path
import sys
import unittest
from unittest.mock import ANY, MagicMock, patch

from fastapi.testclient import TestClient


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))
os.environ.setdefault(
    "KB_PARAMETER_NAME",
    "/workshop/mortgage-assistant/bedrock/knowledge-base-id",
)
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("MEMORY_TABLE_NAME", "test-memory-table")
os.environ.setdefault("MORTGAGE_API_KEY", "test-api-key")
os.environ.setdefault(
    "CREDIT_SCORE_MCP_URL",
    "http://credit-score-mcp.credit-services.svc.cluster.local:8081/mcp",
)

import mortgage_agent  # noqa: E402
import mortgage_api  # noqa: E402
from execution import COMPLETED, INTERRUPTED, Execution, SessionBusy  # noqa: E402
from service import Outcome  # noqa: E402


class ApiContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(mortgage_api.app)
        self.headers = {"Authorization": "Bearer test-api-key"}

    def test_canonical_model_and_configured_knowledge_base_parameter(self) -> None:
        self.assertEqual(mortgage_agent.MODEL_ID, "us.anthropic.claude-sonnet-4-6")
        self.assertEqual(
            mortgage_agent.KB_PARAMETER_NAME,
            "/workshop/mortgage-assistant/bedrock/knowledge-base-id",
        )

    def _orchestrator(self, outcome=None, error=None):
        orchestrator = MagicMock()
        if error is not None:
            orchestrator.invoke.side_effect = error
        else:
            orchestrator.invoke.return_value = outcome
        orchestrator.explanation.return_value = {"route": [], "records": 3}
        return orchestrator

    def _make_outcome(self, status=COMPLETED, response="remembered response", interrupts=None):
        execution = Execution(
            "participant-123456789012", "session-1", "req-1", 1, status, "p", "h"
        )
        return Outcome(execution, response=response, interrupts=interrupts or [])

    def _body(self, **extra):
        return {
            "prompt": "What did I tell you?",
            "actor_id": "participant-123456789012",
            "session_id": "session-1",
            **extra,
        }

    def test_invoke_returns_context_explanation_and_uses_client_request_id(self) -> None:
        orchestrator = self._orchestrator(self._make_outcome())
        with patch("mortgage_api.get_orchestrator", return_value=orchestrator):
            response = self.client.post(
                "/invoke", headers=self.headers, json=self._body(request_id="req-1")
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["response"], "remembered response")
        self.assertEqual(body["explanation"]["records"], 3)
        self.assertIn("trace_id", body)
        orchestrator.invoke.assert_called_once_with(
            "participant-123456789012",
            "session-1",
            "req-1",
            "What did I tell you?",
            ANY,
        )

    def test_invoke_generates_request_id_when_absent(self) -> None:
        orchestrator = self._orchestrator(self._make_outcome())
        with patch("mortgage_api.get_orchestrator", return_value=orchestrator):
            self.client.post("/invoke", headers=self.headers, json=self._body())
        self.assertTrue(orchestrator.invoke.call_args.args[2])

    @patch("mortgage_api.telemetry.current_trace_id", return_value="a" * 32)
    def test_invoke_returns_active_trace_id(self, _) -> None:
        orchestrator = self._orchestrator(self._make_outcome())
        with patch("mortgage_api.get_orchestrator", return_value=orchestrator):
            response = self.client.post("/invoke", headers=self.headers, json=self._body())
        self.assertEqual(response.json()["trace_id"], "a" * 32)

    def test_pending_approval_returns_202_with_interrupts(self) -> None:
        interrupts = [{"id": "i-1", "name": "approve_create_loan_application", "reason": {}}]
        orchestrator = self._orchestrator(
            self._make_outcome(status=INTERRUPTED, response=None, interrupts=interrupts)
        )
        with patch("mortgage_api.get_orchestrator", return_value=orchestrator):
            response = self.client.post("/invoke", headers=self.headers, json=self._body())
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "awaiting_approval")
        self.assertEqual(response.json()["interrupts"], interrupts)

    def test_session_conflict_returns_409(self) -> None:
        orchestrator = self._orchestrator(error=SessionBusy("session busy"))
        with patch("mortgage_api.get_orchestrator", return_value=orchestrator):
            response = self.client.post("/invoke", headers=self.headers, json=self._body())
        self.assertEqual(response.status_code, 409)

    def test_failure_returns_safe_error_that_names_the_resume_key(self) -> None:
        orchestrator = self._orchestrator(error=RuntimeError("provider unavailable"))
        with patch("mortgage_api.get_orchestrator", return_value=orchestrator):
            response = self.client.post(
                "/invoke", headers=self.headers, json=self._body(request_id="req-9")
            )
        self.assertEqual(response.status_code, 500)
        self.assertIn("request_id=req-9", response.json()["detail"])
        self.assertNotIn("provider unavailable", response.text)

    def test_approvals_endpoint_passes_decisions(self) -> None:
        orchestrator = self._orchestrator()
        orchestrator.decide.return_value = self._make_outcome()
        with patch("mortgage_api.get_orchestrator", return_value=orchestrator):
            response = self.client.post(
                "/executions/req-1/approvals",
                headers=self.headers,
                json={
                    "actor_id": "participant-123456789012",
                    "session_id": "session-1",
                    "decisions": [
                        {"interrupt_id": "i-1", "approved": True, "reviewer": "pat"}
                    ],
                },
            )
        self.assertEqual(response.status_code, 200)
        decisions = orchestrator.decide.call_args.args[3]
        self.assertEqual(decisions[0]["interrupt_id"], "i-1")
        self.assertTrue(decisions[0]["approved"])

    def test_trail_endpoint_requires_auth_and_scopes_by_actor(self) -> None:
        orchestrator = self._orchestrator()
        orchestrator.trail.return_value = {"chain_valid": True, "records": []}
        params = {"actor_id": "participant-123456789012", "session_id": "session-1"}
        with patch("mortgage_api.get_orchestrator", return_value=orchestrator):
            self.assertEqual(
                self.client.get("/executions/req-1", params=params).status_code, 401
            )
            ok = self.client.get("/executions/req-1", params=params, headers=self.headers)
        self.assertEqual(ok.status_code, 200)
        orchestrator.trail.assert_called_once_with(
            "participant-123456789012", "session-1", "req-1"
        )

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
