"""Contracts for grounded retrieval, synthetic fixtures, and agent boundaries."""

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch

from strands.hooks import BeforeInvocationEvent

APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))
os.environ.setdefault("MEMORY_TABLE_NAME", "test-memory-table")

import mortgage_agent  # noqa: E402
from resilience import CurrentPromptHook  # noqa: E402


class KnowledgeBaseConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        mortgage_agent.get_knowledge_base_id.cache_clear()

    def tearDown(self) -> None:
        mortgage_agent.get_knowledge_base_id.cache_clear()

    def test_region_and_parameter_name_are_required(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "AWS_REGION or AWS_DEFAULT_REGION"):
                mortgage_agent._get_aws_region()

        with patch.object(mortgage_agent, "KB_PARAMETER_NAME", None):
            with self.assertRaisesRegex(RuntimeError, "KB_PARAMETER_NAME"):
                mortgage_agent.get_knowledge_base_id()

    def test_knowledge_base_id_uses_configured_parameter_and_region(self) -> None:
        ssm = MagicMock()
        ssm.get_parameter.return_value = {"Parameter": {"Value": " kb-123 "}}
        with (
            patch.object(mortgage_agent, "KB_PARAMETER_NAME", "/configured/kb"),
            patch.dict(os.environ, {"AWS_REGION": "us-west-2"}, clear=True),
            patch("mortgage_agent.boto3.client", return_value=ssm) as client,
        ):
            self.assertEqual(mortgage_agent.get_knowledge_base_id(), "kb-123")

        client.assert_called_once_with("ssm", region_name="us-west-2")
        ssm.get_parameter.assert_called_once_with(Name="/configured/kb")

    def test_retrieval_wrapper_passes_fixed_grounding_configuration(self) -> None:
        raw_result = {"status": "success", "content": [{"text": "grounded passage"}]}
        with (
            patch("mortgage_agent.get_knowledge_base_id", return_value="kb-123"),
            patch("mortgage_agent._get_aws_region", return_value="us-west-2"),
            patch("mortgage_agent.retrieve", return_value=raw_result) as retrieve,
        ):
            result = mortgage_agent.retrieve_mortgage_knowledge(" refinance ")

        self.assertEqual(result, "grounded passage")
        retrieve.assert_called_once_with(
            {
                "toolUseId": ANY,
                "input": {
                    "text": "refinance",
                    "knowledgeBaseId": "kb-123",
                    "region": "us-west-2",
                    "numberOfResults": 5,
                    "score": 0.4,
                },
            }
        )

    def test_retrieval_wrapper_handles_blank_failure_and_empty_success(self) -> None:
        with self.assertRaises(ValueError):
            mortgage_agent.retrieve_mortgage_knowledge("   ")
        with self.assertRaisesRegex(RuntimeError, "retrieval failed"):
            mortgage_agent._extract_retrieval_text(
                {"status": "error", "content": [{"text": "safe failure"}]}
            )
        self.assertEqual(
            mortgage_agent._extract_retrieval_text(
                {"status": "success", "content": [{"json": {"ignored": True}}]}
            ),
            "No relevant results were found in the mortgage knowledge base.",
        )


class SyntheticFixtureTests(unittest.TestCase):
    def test_customer_id_validation_and_coherent_fixtures(self) -> None:
        details = mortgage_agent.get_mortgage_details("  CUST-123_test  ")
        statuses = mortgage_agent.get_mortgage_application_document_statuses(
            "CUST-123_test"
        )
        application = mortgage_agent.get_application_details("CUST-123_test")

        self.assertEqual(details["customer_id"], "CUST-123_test")
        self.assertEqual(details["account_number"], "MORTGAGE-CUST-123_test")
        self.assertGreater(details["next_payment_amount"], 0)
        self.assertEqual(statuses["customer_id"], "CUST-123_test")
        self.assertEqual(application["customer_id"], "CUST-123_test")

    def test_customer_id_rejects_invalid_values(self) -> None:
        for value in ("", "-starts-with-hyphen", "contains space", "a" * 65):
            with self.subTest(value=value), self.assertRaises(ValueError):
                mortgage_agent.get_mortgage_details(value)

    def test_created_customer_id_has_synthetic_shape(self) -> None:
        self.assertRegex(mortgage_agent.create_customer_id(), r"^CUST-[0-9A-F]{8}$")

    def test_create_application_validates_bounds_and_uses_plural_expenses(self) -> None:
        result = mortgage_agent.create_loan_application(
            " CUST-123 ", " Synthetic Person ", 35, 120000, 45000
        )
        self.assertIn("Loan application created for Synthetic Person", result)
        self.assertIn("annual_expenses=45000", result)

        invalid_arguments = (
            ("CUST-123", " ", 35, 120000, 45000),
            ("CUST-123", "Synthetic", 17, 120000, 45000),
            ("CUST-123", "Synthetic", 101, 120000, 45000),
            ("CUST-123", "Synthetic", 35, 0, 0),
            ("CUST-123", "Synthetic", 35, 10_000_001, 0),
            ("CUST-123", "Synthetic", 35, 120000, -1),
            ("CUST-123", "Synthetic", 35, 120000, 120001),
        )
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                mortgage_agent.create_loan_application(*arguments)


class AgentContractTests(unittest.TestCase):
    def test_names_ids_prompts_and_tool_boundaries_stay_stable(self) -> None:
        self.assertEqual(
            {name: spec["agent_id"] for name, spec in mortgage_agent.SPECIALISTS.items()},
            {
                "mortgage_education_specialist": "general",
                "existing_mortgage_specialist": "existing",
                "mortgage_application_specialist": "new_application",
            },
        )
        self.assertEqual(
            mortgage_agent.SPECIALISTS["mortgage_education_specialist"]["tools"],
            [mortgage_agent.retrieve_mortgage_knowledge],
        )
        self.assertIn("Routing rationale:", mortgage_agent.SUPERVISOR_PROMPT)
        self.assertIn("retrieve_mortgage_knowledge exactly once", mortgage_agent.GENERAL_PROMPT)
        application_prompt = " ".join(mortgage_agent.NEW_APPLICATION_PROMPT.split())
        self.assertIn("never ask for a supplied field again", application_prompt)
        self.assertIn("one question at a time", application_prompt)
        self.assertIn("before the corresponding tool executes successfully", application_prompt)

    def test_specialist_is_agent_as_tool_without_memory_managers(self) -> None:
        wrapped_tool = MagicMock()
        agent_instance = MagicMock()
        agent_instance.as_tool.return_value = wrapped_tool
        with patch("mortgage_agent.Agent", return_value=agent_instance) as agent:
            result = mortgage_agent.create_specialist_tool(
                "mortgage_application_specialist",
                model=MagicMock(),
            )

        configuration = agent.call_args.kwargs
        self.assertEqual(configuration["agent_id"], "new_application")
        self.assertNotIn("session_manager", configuration)
        self.assertNotIn("memory_manager", configuration)
        self.assertIs(
            configuration["structured_output_model"],
            mortgage_agent.SpecialistReport,
        )
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
            result = mortgage_agent.create_supervisor_agent("actor-1", "session-1")

        sessions.assert_called_once_with("actor-1", "session-1")
        memories.assert_called_once_with("actor-1")
        configuration = agent.call_args.kwargs
        self.assertEqual(configuration["name"], "mortgage_supervisor")
        self.assertEqual(configuration["agent_id"], "supervisor")
        self.assertIs(configuration["session_manager"], session_manager)
        self.assertIs(configuration["memory_manager"], memory_manager)
        self.assertIs(result, supervisor)

    def test_current_prompt_hook_replaces_restored_prompt(self) -> None:
        agent = SimpleNamespace(system_prompt="stored old prompt")
        hook = CurrentPromptHook("currently deployed prompt")
        hook._before_invocation(BeforeInvocationEvent(agent=agent, messages=[]))
        self.assertEqual(agent.system_prompt, "currently deployed prompt")


if __name__ == "__main__":
    unittest.main()
