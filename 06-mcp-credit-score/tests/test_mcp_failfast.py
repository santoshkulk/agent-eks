import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))
os.environ.setdefault("MEMORY_TABLE_NAME", "test-memory-table")
os.environ.setdefault("CREDIT_SCORE_MCP_URL", "http://credit-score-mcp.credit-services.svc.cluster.local:8081/mcp")

from strands.hooks import AfterToolCallEvent  # noqa: E402

import mortgage_agent  # noqa: E402
from audit import AuditTrail, use_audit_trail  # noqa: E402
from store import InMemoryItemStore  # noqa: E402


def _event(exception, status="error"):
    return AfterToolCallEvent(
        agent=MagicMock(),
        selected_tool=SimpleNamespace(tool_type="mcp"),
        tool_use={"name": "get_credit_score", "toolUseId": "t", "input": {}},
        invocation_state={},
        result={"toolUseId": "t", "status": status, "content": [{"text": "x"}]},
        exception=exception,
    )


class McpFailFastTests(unittest.TestCase):
    def setUp(self) -> None:
        self.trail = AuditTrail(InMemoryItemStore(), "a", "s", "r")

    def test_a_raised_transport_error_fails_the_request(self) -> None:
        with use_audit_trail(self.trail):
            mortgage_agent.FailFastHook()._after_tool_call(_event(ConnectionError("reset")))
        self.assertIn("ConnectionError", self.trail.abort_reason)

    def test_a_returned_error_result_is_left_for_the_model_to_report(self) -> None:
        with use_audit_trail(self.trail):
            mortgage_agent.FailFastHook()._after_tool_call(_event(None))
        self.assertIsNone(self.trail.abort_reason)


if __name__ == "__main__":
    unittest.main()
