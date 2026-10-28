"""Production-style mortgage semantics shared by both Lab 5 variants."""

import os
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import MagicMock, patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))
os.environ.setdefault("MEMORY_TABLE_NAME", "test-memory-table")
os.environ.setdefault("KB_PARAMETER_NAME", "/workshop/mortgage-assistant/bedrock/knowledge-base-id")
os.environ.setdefault("AWS_REGION", "us-west-2")

import mortgage_agent  # noqa: E402


class MortgageAgentSemanticsTests(unittest.TestCase):
    def tearDown(self) -> None:
        mortgage_agent.get_knowledge_base_id.cache_clear()

    def test_specialist_display_names_preserve_stable_agent_ids(self) -> None:
        self.assertEqual(
            mortgage_agent.SPECIALIST_TOOL_NAMES,
            {
                "mortgage_education_specialist",
                "existing_mortgage_specialist",
                "mortgage_application_specialist",
            },
        )
        self.assertEqual(
            {
                name: spec["agent_id"]
                for name, spec in mortgage_agent.SPECIALISTS.items()
            },
            {
                "mortgage_education_specialist": "general",
                "existing_mortgage_specialist": "existing",
                "mortgage_application_specialist": "new_application",
            },
        )
        education_tools = mortgage_agent.SPECIALISTS[
            "mortgage_education_specialist"
        ]["tools"]
        self.assertEqual(
            [tool.tool_name for tool in education_tools],
            ["retrieve_mortgage_knowledge"],
        )
        application_tools = mortgage_agent.SPECIALISTS[
            "mortgage_application_specialist"
        ]["tools"]
        self.assertIn(
            "get_mortgage_application_document_statuses",
            [tool.tool_name for tool in application_tools],
        )

    def test_knowledge_base_configuration_is_required_and_region_is_explicit(self) -> None:
        client = MagicMock()
        client.get_parameter.return_value = {"Parameter": {"Value": " kb-test "}}
        with (
            patch.object(mortgage_agent, "KB_PARAMETER_NAME", None),
            patch.object(mortgage_agent.boto3, "client", return_value=client),
        ):
            with self.assertRaisesRegex(RuntimeError, "KB_PARAMETER_NAME"):
                mortgage_agent.get_knowledge_base_id()
            mortgage_agent.boto3.client.assert_not_called()

        mortgage_agent.get_knowledge_base_id.cache_clear()
        with (
            patch.object(mortgage_agent, "KB_PARAMETER_NAME", "/test/kb"),
            patch.dict(os.environ, {}, clear=True),
            patch.object(mortgage_agent.boto3, "client", return_value=client),
        ):
            with self.assertRaisesRegex(RuntimeError, "AWS_REGION"):
                mortgage_agent.get_knowledge_base_id()

        mortgage_agent.get_knowledge_base_id.cache_clear()
        with (
            patch.object(mortgage_agent, "KB_PARAMETER_NAME", "/test/kb"),
            patch.dict(os.environ, {"AWS_REGION": "us-west-2"}, clear=True),
            patch.object(mortgage_agent.boto3, "client", return_value=client) as boto_client,
        ):
            self.assertEqual(mortgage_agent.get_knowledge_base_id(), "kb-test")
        boto_client.assert_called_with(
            "ssm", region_name="us-west-2"
        )
        client.get_parameter.assert_called_with(Name="/test/kb")

    def test_retrieval_wrapper_passes_fixed_grounding_configuration(self) -> None:
        result = {
            "status": "success",
            "content": [{"text": " first "}, {"json": {}}, {"text": "second"}],
        }
        with (
            patch.object(mortgage_agent, "get_knowledge_base_id", return_value="kb-1"),
            patch.object(mortgage_agent, "_get_aws_region", return_value="us-west-2"),
            patch.object(mortgage_agent, "retrieve", return_value=result) as retrieve,
        ):
            self.assertEqual(
                mortgage_agent.retrieve_mortgage_knowledge(" refinance "),
                "first \nsecond",
            )
        payload = retrieve.call_args.args[0]
        self.assertRegex(
            payload["toolUseId"], r"^mortgage-knowledge-[0-9a-f]{32}$"
        )
        self.assertEqual(
            payload["input"],
            {
                "text": "refinance",
                "knowledgeBaseId": "kb-1",
                "region": "us-west-2",
                "numberOfResults": 5,
                "score": 0.4,
            },
        )

    def test_retrieval_wrapper_handles_invalid_failed_and_empty_results(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            mortgage_agent.retrieve_mortgage_knowledge("  ")
        with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
            mortgage_agent._extract_retrieval_text(
                {"status": "error", "content": [{"text": "provider unavailable"}]}
            )
        self.assertEqual(
            mortgage_agent._extract_retrieval_text(
                {"status": "success", "content": []}
            ),
            "No relevant results were found in the mortgage knowledge base.",
        )

    def test_customer_read_fixtures_share_normalized_identity(self) -> None:
        details = mortgage_agent.get_mortgage_details(" customer-1 ")
        documents = mortgage_agent.get_mortgage_application_document_statuses(
            " customer-1 "
        )
        application = mortgage_agent.get_application_details(" customer-1 ")
        self.assertEqual(details["customer_id"], "customer-1")
        self.assertEqual(details["account_number"], "MORTGAGE-customer-1")
        self.assertEqual(documents["customer_id"], "customer-1")
        self.assertEqual(application["customer_id"], "customer-1")
        self.assertNotIn("name", application)

    def test_customer_id_validation_is_generic(self) -> None:
        for invalid in ("", " ", "customer/1", "x" * 65):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    mortgage_agent.get_application_details(invalid)

    def test_application_side_effects_validate_and_use_plural_expenses(self) -> None:
        customer_id = mortgage_agent.create_customer_id()
        self.assertTrue(re.fullmatch(r"CUST-[0-9A-F]{8}", customer_id))
        result = mortgage_agent.create_loan_application(
            " CUST-ABC12345 ", " Workshop Customer ", 30, 90_000, 40_000
        )
        self.assertIn("customer CUST-ABC12345", result)
        self.assertIn("annual_expenses=40000", result)
        invalid_cases = (
            ("C1", "", 30, 90_000, 40_000),
            ("C1", "Name", 17, 90_000, 40_000),
            ("C1", "Name", 30, 0, 0),
            ("C1", "Name", 30, 90_000, -1),
            ("C1", "Name", 30, 90_000, 90_001),
        )
        for args in invalid_cases:
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    mortgage_agent.create_loan_application(*args)


if __name__ == "__main__":
    unittest.main()
