import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))
os.environ.setdefault("MEMORY_TABLE_NAME", "test-memory-table")

import mortgage_agent  # noqa: E402
import mortgage_api  # noqa: E402


class ApiContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(mortgage_api.app)

    def test_canonical_model_and_knowledge_base_defaults(self) -> None:
        self.assertEqual(
            mortgage_agent.MODEL_ID,
            "us.anthropic.claude-sonnet-4-6",
        )
        self.assertEqual(
            mortgage_agent.KB_PARAMETER_NAME,
            "/workshop/mortgage-assistant/bedrock/knowledge-base-id",
        )

    @patch("mortgage_api.run_prompt", return_value="remembered response")
    def test_invoke_returns_actor_and_session(self, run_prompt) -> None:
        response = self.client.post(
            "/invoke",
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
        run_prompt.assert_called_once_with(
            "What did I tell you?",
            actor_id="participant-123456789012",
            session_id="session-1",
        )

    def test_invalid_actor_is_rejected(self) -> None:
        response = self.client.post(
            "/invoke",
            json={
                "prompt": "Hello",
                "actor_id": "participant/other",
                "session_id": "session-1",
            },
        )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
