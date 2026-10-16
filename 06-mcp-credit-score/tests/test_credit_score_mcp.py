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


class ToolPage(list):
    def __init__(self, tools, pagination_token=None):
        super().__init__(tools)
        self.pagination_token = pagination_token


class CreditScoreMCPTests(unittest.TestCase):
    @patch("credit_score_mcp.streamable_http_client", return_value="transport")
    def test_transport_factory_uses_streamable_http_for_fixed_provider(self, transport) -> None:
        result = credit_score_mcp._transport_factory(
            credit_score_mcp.EXPECTED_PROVIDER_URL
        )
        self.assertEqual(result, "transport")
        transport.assert_called_once_with(credit_score_mcp.EXPECTED_PROVIDER_URL)

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
    def test_expected_tool_is_discovered_and_context_managed(self, client_class) -> None:
        tool = Mock(tool_name="get_credit_score")
        client = client_class.return_value
        client.list_tools_sync.return_value = ToolPage([tool])

        with credit_score_mcp.credit_score_tool() as selected:
            self.assertIs(selected, tool)

        client.__enter__.assert_called_once()
        client.__exit__.assert_called_once()
        self.assertEqual(client_class.call_args.kwargs["startup_timeout"], 30)

    @patch("credit_score_mcp.MCPClient")
    def test_every_invocation_creates_a_fresh_client(self, client_class) -> None:
        tool = Mock(tool_name="get_credit_score")
        client_class.return_value.list_tools_sync.return_value = ToolPage([tool])
        with credit_score_mcp.credit_score_tool():
            pass
        with credit_score_mcp.credit_score_tool():
            pass
        self.assertEqual(client_class.call_count, 2)

    @patch("credit_score_mcp.MCPClient")
    def test_full_pagination_accepts_single_tool(self, client_class) -> None:
        tool = Mock(tool_name="get_credit_score")
        client_class.return_value.list_tools_sync.side_effect = [
            ToolPage([], "page-2"),
            ToolPage([tool]),
        ]
        with credit_score_mcp.credit_score_tool() as selected:
            self.assertIs(selected, tool)
        self.assertEqual(
            client_class.return_value.list_tools_sync.call_args_list[1].kwargs,
            {"pagination_token": "page-2"},
        )

    @patch("credit_score_mcp.MCPClient")
    def test_extra_tool_on_later_page_is_rejected(self, client_class) -> None:
        expected = Mock(tool_name="get_credit_score")
        extra = Mock(tool_name="unexpected_tool")
        client_class.return_value.list_tools_sync.side_effect = [
            ToolPage([expected], "page-2"),
            ToolPage([extra]),
        ]
        with self.assertRaisesRegex(
            credit_score_mcp.CreditScoreMCPError,
            "must expose exactly one tool",
        ):
            with credit_score_mcp.credit_score_tool():
                self.fail("Additional later-page tool should not be yielded")

    @patch("credit_score_mcp.MCPClient")
    def test_exceptions_from_supervisor_body_are_not_relabelled(self, client_class) -> None:
        tool = Mock(tool_name="get_credit_score")
        client_class.return_value.list_tools_sync.return_value = ToolPage([tool])
        with self.assertRaisesRegex(ValueError, "memory failure"):
            with credit_score_mcp.credit_score_tool():
                raise ValueError("memory failure")

    @patch("credit_score_mcp.MCPClient")
    def test_initialization_failure_is_safely_wrapped(self, client_class) -> None:
        client_class.return_value.__enter__.side_effect = RuntimeError("credentials leaked")
        with self.assertRaisesRegex(
            credit_score_mcp.CreditScoreMCPError,
            "unavailable or has an invalid contract",
        ) as raised:
            with credit_score_mcp.credit_score_tool():
                pass
        self.assertNotIn("credentials leaked", str(raised.exception))

    @patch("mortgage_agent.get_knowledge_base_id", return_value="kb-test")
    @patch("mortgage_agent.create_memory_manager", return_value="memory-manager")
    @patch("mortgage_agent.create_session_manager", return_value="session-manager")
    @patch("mortgage_agent.Agent")
    def test_remote_tool_is_supervisor_only_with_one_manager_pair(
        self,
        agent_class,
        session_manager,
        memory_manager,
        _,
    ) -> None:
        remote_tool = object()
        mortgage_agent.create_supervisor_agent(
            actor_id="participant-1",
            session_id="session-1",
            request_id="request-1",
            remote_credit_score_tool=remote_tool,
        )

        self.assertEqual(agent_class.call_count, 4)
        specialist_calls = agent_class.call_args_list[:3]
        for call in specialist_calls:
            self.assertNotIn("session_manager", call.kwargs)
            self.assertNotIn("memory_manager", call.kwargs)
            self.assertNotIn(remote_tool, call.kwargs["tools"])
        supervisor_config = agent_class.call_args_list[-1].kwargs
        self.assertIs(supervisor_config["tools"][-1], remote_tool)
        self.assertEqual(supervisor_config["agent_id"], "supervisor")
        session_manager.assert_called_once_with("participant-1", "session-1")
        memory_manager.assert_called_once_with("participant-1")

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
    def test_run_supervisor_constructs_and_invokes_inside_mcp_context(
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

        def construct(*args, **kwargs):
            self.assertTrue(context_active["value"])
            self.assertIs(kwargs["remote_credit_score_tool"], remote_tool)

            def invoke(prompt: str, **invoke_kwargs) -> str:
                self.assertTrue(context_active["value"])
                self.assertEqual(prompt, "Get my credit score")
                self.assertEqual(invoke_kwargs["invocation_state"]["request_id"], "request-1")
                return "score response"

            return invoke

        tool_context.return_value = ToolContext()
        create_supervisor.side_effect = construct
        result = mortgage_agent.run_supervisor(
            "Get my credit score",
            actor_id="participant-1",
            session_id="session-1",
            request_id="request-1",
        )
        self.assertEqual(result, "score response")
        self.assertFalse(context_active["value"])
        tool_context.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
