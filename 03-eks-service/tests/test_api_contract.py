import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))
os.environ.setdefault("MORTGAGE_API_KEY", "test-api-key")
# Assert the shipped defaults, not whatever the developer's shell exports.
os.environ.pop("MODEL_ID", None)
os.environ.pop("KB_PARAMETER_NAME", None)

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

    def test_startup_fails_without_api_key(self) -> None:
        for value in ("", "   "):
            result = subprocess.run(
                [sys.executable, "-c", "import mortgage_api"],
                cwd=APP_DIR,
                env={**os.environ, "MORTGAGE_API_KEY": value},
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("MORTGAGE_API_KEY is required", result.stderr)

    def test_invoke_requires_bearer_token(self) -> None:
        with patch("mortgage_api.run_prompt") as run_prompt:
            for headers in (
                {},
                {"Authorization": "Bearer wrong-key"},
                {"Authorization": "test-api-key"},
            ):
                response = self.client.post(
                    "/invoke", headers=headers, json={"prompt": "What is refinancing?"}
                )
                self.assertEqual(response.status_code, 401)
                self.assertEqual(
                    response.json(), {"detail": "Invalid or missing bearer token"}
                )
        run_prompt.assert_not_called()

    def test_invoke_returns_response_request_id_and_duration(self) -> None:
        with patch("mortgage_api.run_prompt", return_value="answer") as run_prompt:
            response = self.client.post(
                "/invoke", headers=self.headers, json={"prompt": "What is refinancing?"}
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), {"request_id", "response", "duration_ms"})
        self.assertEqual(body["response"], "answer")
        self.assertTrue(body["request_id"])
        self.assertGreaterEqual(body["duration_ms"], 0)
        run_prompt.assert_called_once_with("What is refinancing?")

    def test_invoke_rejects_invalid_prompts(self) -> None:
        with patch("mortgage_api.run_prompt") as run_prompt:
            for body in ({"prompt": ""}, {"prompt": "x" * 4001}, {}):
                response = self.client.post("/invoke", headers=self.headers, json=body)
                self.assertEqual(response.status_code, 422)
        run_prompt.assert_not_called()

    def test_invoke_failure_returns_request_id_without_internal_detail(self) -> None:
        with patch(
            "mortgage_api.run_prompt",
            side_effect=RuntimeError("AccessDeniedException: secret internals"),
        ), self.assertLogs("mortgage_api", level="ERROR"):
            response = self.client.post(
                "/invoke", headers=self.headers, json={"prompt": "What is refinancing?"}
            )
        self.assertEqual(response.status_code, 500)
        detail = response.json()["detail"]
        self.assertIn("request_id=", detail)
        self.assertNotIn("secret internals", detail)

    def test_health_endpoints_do_not_require_authentication(self) -> None:
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
        with patch("mortgage_api.get_knowledge_base_id", return_value="KB12345"):
            response = self.client.get("/health/ready")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "status": "ready",
                "knowledge_base_id": "KB12345",
                "model_id": mortgage_agent.MODEL_ID,
            },
        )

    def test_readiness_fails_when_knowledge_base_id_is_unavailable(self) -> None:
        client = TestClient(mortgage_api.app, raise_server_exceptions=False)
        with patch(
            "mortgage_api.get_knowledge_base_id",
            side_effect=RuntimeError("Unable to retrieve the Knowledge Base ID"),
        ):
            response = client.get("/health/ready")
        self.assertEqual(response.status_code, 500)


class KnowledgeBaseLookupTests(unittest.TestCase):
    def setUp(self) -> None:
        mortgage_agent.get_knowledge_base_id.cache_clear()
        self.addCleanup(mortgage_agent.get_knowledge_base_id.cache_clear)

    def test_reads_parameter_and_exports_it_for_the_retrieve_tool(self) -> None:
        with patch("mortgage_agent.boto3.client") as client, patch.dict(os.environ):
            client.return_value.get_parameter.return_value = {
                "Parameter": {"Value": " KB12345 \n"}
            }
            self.assertEqual(mortgage_agent.get_knowledge_base_id(), "KB12345")
            self.assertEqual(os.environ["KNOWLEDGE_BASE_ID"], "KB12345")
        client.return_value.get_parameter.assert_called_once_with(
            Name=mortgage_agent.KB_PARAMETER_NAME
        )

    def test_empty_parameter_is_an_error(self) -> None:
        with patch("mortgage_agent.boto3.client") as client:
            client.return_value.get_parameter.return_value = {
                "Parameter": {"Value": "  "}
            }
            with self.assertRaises(RuntimeError):
                mortgage_agent.get_knowledge_base_id()

    def test_run_prompt_rejects_blank_prompt(self) -> None:
        with self.assertRaises(ValueError):
            mortgage_agent.run_prompt("   ")


if __name__ == "__main__":
    unittest.main()
