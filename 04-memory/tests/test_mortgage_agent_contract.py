"""Contracts for grounded retrieval, synthetic fixtures, and specialist identity."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))
os.environ.setdefault("MEMORY_TABLE_NAME", "test-memory-table")

import mortgage_agent  # noqa: E402


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


class SpecialistContractTests(unittest.TestCase):
    def test_display_names_change_while_agent_ids_stay_stable(self) -> None:
        self.assertEqual(
            {name: spec["agent_id"] for name, spec in mortgage_agent.SPECIALISTS.items()},
            {
                "mortgage_education_specialist": "general",
                "existing_mortgage_specialist": "existing",
                "mortgage_application_specialist": "new_application",
            },
        )
        education_tools = mortgage_agent.SPECIALISTS["mortgage_education_specialist"][
            "tools"
        ]
        self.assertEqual(education_tools, [mortgage_agent.retrieve_mortgage_knowledge])

    def test_specialist_keeps_persistent_as_tool_contract(self) -> None:
        session_manager = MagicMock()
        wrapped_tool = MagicMock()
        agent_instance = MagicMock()
        agent_instance.as_tool.return_value = wrapped_tool
        with patch("mortgage_agent.Agent", return_value=agent_instance) as agent:
            result = mortgage_agent.create_specialist_tool(
                "mortgage_application_specialist",
                "actor-1",
                "session-1",
                model=MagicMock(),
                session_factory=lambda: session_manager,
            )

        configuration = agent.call_args.kwargs
        self.assertEqual(configuration["agent_id"], "new_application")
        self.assertIs(configuration["session_manager"], session_manager)
        self.assertIs(
            configuration["structured_output_model"],
            mortgage_agent.SpecialistReport,
        )
        self.assertIsNone(configuration["callback_handler"])
        agent_instance.as_tool.assert_called_once_with(
            name="mortgage_application_specialist",
            description=configuration["description"],
            preserve_context=True,
        )
        self.assertIs(result, wrapped_tool)

    def test_application_prompt_preserves_multiturn_collection_and_safe_claims(self) -> None:
        prompt = " ".join(mortgage_agent.NEW_APPLICATION_PROMPT.split())
        self.assertIn("never ask for a supplied field again", prompt)
        self.assertIn("one question at a time", prompt)
        self.assertIn("annual expenses", prompt)
        self.assertIn("before the corresponding tool executes successfully", prompt)


if __name__ == "__main__":
    unittest.main()
