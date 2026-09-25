import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))
os.environ.setdefault("MEMORY_TABLE_NAME", "test-memory-table")
os.environ.setdefault(
    "CREDIT_SCORE_MCP_URL",
    "http://credit-score-mcp.credit-services.svc.cluster.local:8081/mcp",
)

import credit_score_mcp  # noqa: E402
import mortgage_agent  # noqa: E402


class CreditScoreMCPTests(unittest.TestCase):
    def test_required_url_is_enforced(self) -> None:
        with patch.dict(os.environ, {"CREDIT_SCORE_MCP_URL": ""}):
            with self.assertRaisesRegex(
                credit_score_mcp.CreditScoreMCPError,
                "CREDIT_SCORE_MCP_URL is required",
            ):
                credit_score_mcp.get_credit_score_mcp_url()

    def test_url_rejects_embedded_credentials(self) -> None:
        with patch.dict(
            os.environ,
            {"CREDIT_SCORE_MCP_URL": "http://user:password@provider/mcp"},
        ):
            with self.assertRaisesRegex(
                credit_score_mcp.CreditScoreMCPError,
                "must not contain embedded credentials",
            ):
                credit_score_mcp.get_credit_score_mcp_url()

    def test_url_rejects_any_provider_other_than_fixed_service(self) -> None:
        with patch.dict(
            os.environ,
            {"CREDIT_SCORE_MCP_URL": "http://other-provider.example/mcp"},
        ):
            with self.assertRaisesRegex(
                credit_score_mcp.CreditScoreMCPError,
                "fixed workshop MCP server",
            ):
                credit_score_mcp.get_credit_score_mcp_url()

    @patch("credit_score_mcp.MCPClient")
    def test_exceptions_from_supervisor_body_are_not_relabelled(
        self,
        client_class,
    ) -> None:
        tool = Mock(tool_name="get_credit_score")
        client_class.return_value.list_tools_sync.return_value = [tool]

        with self.assertRaisesRegex(ValueError, "memory failure"):
            with credit_score_mcp.credit_score_tool():
                raise ValueError("memory failure")

    @patch("credit_score_mcp.MCPClient")
    def test_expected_tool_is_discovered_and_context_managed(
        self,
        client_class,
    ) -> None:
        tool = Mock()
        tool.tool_name = "get_credit_score"
        client = client_class.return_value
        client.list_tools_sync.return_value = [tool]

        with credit_score_mcp.credit_score_tool() as selected:
            self.assertIs(selected, tool)

        client.__enter__.assert_called_once()
        client.__exit__.assert_called_once()
        self.assertEqual(client_class.call_args.kwargs["startup_timeout"], 30)

    @patch("credit_score_mcp.MCPClient")
    def test_unexpected_remote_tools_are_rejected(self, client_class) -> None:
        first = Mock(tool_name="get_credit_score")
        second = Mock(tool_name="unexpected_tool")
        client_class.return_value.list_tools_sync.return_value = [first, second]

        with self.assertRaisesRegex(
            credit_score_mcp.CreditScoreMCPError,
            "must expose exactly one tool",
        ):
            with credit_score_mcp.credit_score_tool():
                self.fail("Unexpected tool contract should not be yielded")

    @patch("mortgage_agent.Agent")
    @patch("mortgage_agent.create_memory_components")
    def test_supervisor_registers_remote_tool_with_existing_tools(
        self,
        memory_components,
        agent_class,
    ) -> None:
        memory_components.return_value = ("session-manager", "memory-manager")
        remote_tool = object()

        mortgage_agent.create_supervisor_agent(
            actor_id="participant-1",
            session_id="session-1",
            request_id="request-1",
            remote_credit_score_tool=remote_tool,
        )

        tools = agent_class.call_args.kwargs["tools"]
        self.assertEqual(len(tools), 5)
        self.assertIs(tools[-1], remote_tool)
        self.assertIn(mortgage_agent.answer_general_mortgage_questions, tools)
        self.assertIn(mortgage_agent.answer_existing_mortgage_questions, tools)
        self.assertIn(mortgage_agent.answer_new_loan_application_questions, tools)
        self.assertEqual(
            agent_class.call_args.kwargs["trace_attributes"],
            {
                "session.id": "session-1",
                "user.id": "participant-1",
                "tags": ["mortgage-assistant", "request:request-1"],
            },
        )
        memory_components.assert_called_once_with(
            actor_id="participant-1",
            session_id="session-1",
        )

    def test_supervisor_prompt_has_credit_and_memory_safety_policy(self) -> None:
        policy = mortgage_agent.SUPERVISOR_PROMPT.lower()
        for required_text in (
            "explicitly requests a credit score",
            "provides a customer id",
            "approval",
            "denial",
            "pricing",
            "financial advice",
            "never add customer ids",
            "credit scores",
            "long-term memory",
            "report the tool error",
        ):
            self.assertIn(required_text, policy)

    @patch("mortgage_agent.create_supervisor_agent")
    @patch("mortgage_agent.credit_score_tool")
    def test_run_prompt_invokes_supervisor_inside_mcp_context(
        self,
        tool_context,
        create_supervisor,
    ) -> None:
        remote_tool = object()
        context_active = {"value": False}

        class ToolContext:
            def __enter__(self):
                context_active["value"] = True
                return remote_tool

            def __exit__(self, *_):
                context_active["value"] = False

        def invoke(prompt: str) -> str:
            self.assertTrue(context_active["value"])
            self.assertEqual(prompt, "Get my credit score")
            return "score response"

        tool_context.return_value = ToolContext()
        create_supervisor.return_value = invoke

        result = mortgage_agent.run_prompt(
            " Get my credit score ",
            actor_id="participant-1",
            session_id="session-1",
            request_id="request-1",
        )

        self.assertEqual(result, "score response")
        self.assertFalse(context_active["value"])
        create_supervisor.assert_called_once_with(
            actor_id="participant-1",
            session_id="session-1",
            request_id="request-1",
            remote_credit_score_tool=remote_tool,
        )


if __name__ == "__main__":
    unittest.main()
