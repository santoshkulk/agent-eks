"""Contracts for supervisor-only persistence and trace propagation."""

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from strands.hooks import BeforeInvocationEvent

APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))
os.environ.setdefault("MEMORY_TABLE_NAME", "test-memory-table")

import mortgage_agent  # noqa: E402
import telemetry  # noqa: E402
from resilience import CurrentPromptHook, ToolResultCapHook  # noqa: E402


class AgentContractTests(unittest.TestCase):
    def test_specialist_is_traced_agent_as_tool_without_managers(self) -> None:
        wrapped_tool = MagicMock()
        agent_instance = MagicMock()
        agent_instance.as_tool.return_value = wrapped_tool
        attributes = telemetry.trace_attributes("actor-1", "session-1", "request-1")
        with (
            telemetry.use_trace_attributes(attributes),
            patch("mortgage_agent.Agent", return_value=agent_instance) as agent,
        ):
            result = mortgage_agent.create_specialist_tool(
                "mortgage_application_specialist", model=MagicMock()
            )
        configuration = agent.call_args.kwargs
        self.assertEqual(configuration["agent_id"], "new_application")
        self.assertNotIn("session_manager", configuration)
        self.assertNotIn("memory_manager", configuration)
        self.assertNotIn("hooks", configuration)
        self.assertEqual(configuration["trace_attributes"], attributes)
        self.assertIs(configuration["structured_output_model"], mortgage_agent.SpecialistReport)
        agent_instance.as_tool.assert_called_once_with(
            name="mortgage_application_specialist",
            description=configuration["description"],
            preserve_context=True,
        )
        self.assertIs(result, wrapped_tool)

    def test_only_supervisor_receives_one_session_and_memory_manager(self) -> None:
        session_manager = object()
        memory_manager = object()
        supervisor = MagicMock()
        with (
            patch("mortgage_agent.create_specialist_tool", return_value=MagicMock()),
            patch("mortgage_agent.create_session_manager", return_value=session_manager) as sessions,
            patch("mortgage_agent.create_memory_manager", return_value=memory_manager) as memories,
            patch("mortgage_agent.Agent", return_value=supervisor) as agent,
        ):
            result = mortgage_agent.create_supervisor_agent(
                "actor-1", "session-1", "request-1"
            )
        sessions.assert_called_once_with("actor-1", "session-1")
        memories.assert_called_once_with("actor-1")
        configuration = agent.call_args.kwargs
        self.assertEqual(configuration["name"], "mortgage_supervisor")
        self.assertEqual(configuration["agent_id"], "supervisor")
        self.assertIs(configuration["session_manager"], session_manager)
        self.assertIs(configuration["memory_manager"], memory_manager)
        self.assertEqual(
            [type(hook) for hook in configuration["hooks"]],
            [CurrentPromptHook, ToolResultCapHook],
        )
        self.assertEqual(
            configuration["trace_attributes"],
            telemetry.trace_attributes("actor-1", "session-1", "request-1"),
        )
        self.assertIs(result, supervisor)

    def test_current_prompt_hook_replaces_restored_prompt(self) -> None:
        agent = SimpleNamespace(system_prompt="stored old prompt")
        hook = CurrentPromptHook("currently deployed prompt")
        hook._before_invocation(BeforeInvocationEvent(agent=agent, messages=[]))
        self.assertEqual(agent.system_prompt, "currently deployed prompt")

    def test_request_trace_context_is_active_for_construction_and_invocation(self) -> None:
        attributes = telemetry.trace_attributes("actor-1", "session-1", "request-1")
        supervisor = MagicMock(return_value="result")
        with (
            patch("mortgage_agent.create_supervisor_agent", return_value=supervisor) as create,
            patch("mortgage_agent.telemetry.trace_attributes", return_value=attributes),
        ):
            result = mortgage_agent.run_supervisor(
                "hello", "actor-1", "session-1", "request-1"
            )
        self.assertEqual(result, "result")
        create.assert_called_once_with("actor-1", "session-1", "request-1")
        self.assertIsNone(telemetry.current_trace_attributes())


if __name__ == "__main__":
    unittest.main()
