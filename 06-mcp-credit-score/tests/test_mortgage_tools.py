"""Focused tests for Lab 6 mortgage retrieval and local data validation."""

import os
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault(
    "KB_PARAMETER_NAME",
    "/workshop/mortgage-assistant/bedrock/knowledge-base-id",
)
os.environ.setdefault("MEMORY_TABLE_NAME", "test-memory-table")

import mortgage_agent  # noqa: E402


class MortgageConfigurationTests(unittest.TestCase):
    def tearDown(self) -> None:
        mortgage_agent.get_knowledge_base_id.cache_clear()

    def test_region_is_required(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(
                RuntimeError,
                "AWS_REGION or AWS_DEFAULT_REGION",
            ):
                mortgage_agent._get_aws_region()

    def test_knowledge_base_parameter_name_is_required(self) -> None:
        with patch.object(mortgage_agent, "KB_PARAMETER_NAME", None):
            with self.assertRaisesRegex(
                RuntimeError,
                "KB_PARAMETER_NAME must be configured",
            ):
                mortgage_agent.get_knowledge_base_id()

    def test_knowledge_base_id_uses_configured_parameter_and_region(self) -> None:
        ssm = MagicMock()
        ssm.get_parameter.return_value = {"Parameter": {"Value": " kb-123 "}}
        with (
            patch.object(
                mortgage_agent,
                "KB_PARAMETER_NAME",
                "/workshop/mortgage-assistant/bedrock/knowledge-base-id",
            ),
            patch("mortgage_agent.boto3.client", return_value=ssm) as client,
        ):
            self.assertEqual(mortgage_agent.get_knowledge_base_id(), "kb-123")
        client.assert_called_once_with("ssm", region_name="us-west-2")
        ssm.get_parameter.assert_called_once_with(
            Name="/workshop/mortgage-assistant/bedrock/knowledge-base-id"
        )


class MortgageToolTests(unittest.TestCase):
    def test_retrieve_wrapper_sends_explicit_grounding_configuration(self) -> None:
        result = {
            "status": "success",
            "content": [{"text": " Grounded passage. "}],
        }
        with (
            patch("mortgage_agent.get_knowledge_base_id", return_value="kb-123"),
            patch("mortgage_agent._get_aws_region", return_value="us-west-2"),
            patch("mortgage_agent.retrieve", return_value=result) as retrieve,
        ):
            text = mortgage_agent.retrieve_mortgage_knowledge("  fixed rate  ")

        self.assertEqual(text, "Grounded passage.")
        payload = retrieve.call_args.args[0]
        self.assertTrue(payload["toolUseId"].startswith("mortgage-knowledge-"))
        self.assertEqual(
            payload["input"],
            {
                "text": "fixed rate",
                "knowledgeBaseId": "kb-123",
                "region": "us-west-2",
                "numberOfResults": 5,
                "score": 0.4,
            },
        )

    def test_retrieve_wrapper_rejects_empty_query_and_tool_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            mortgage_agent.retrieve_mortgage_knowledge("   ")
        with self.assertRaisesRegex(RuntimeError, "provider failed"):
            mortgage_agent._extract_retrieval_text(
                {"status": "error", "content": [{"text": "provider failed"}]}
            )

    def test_customer_tools_normalize_and_validate_customer_id(self) -> None:
        details = mortgage_agent.get_mortgage_details(" CUST-123_ABC ")
        self.assertEqual(details["customer_id"], "CUST-123_ABC")
        self.assertEqual(details["account_number"], "MORTGAGE-CUST-123_ABC")

        statuses = mortgage_agent.get_mortgage_application_document_statuses(
            " CUST-123_ABC "
        )
        self.assertEqual(statuses["customer_id"], "CUST-123_ABC")
        self.assertEqual(len(statuses["documents"]), 4)

        with self.assertRaisesRegex(ValueError, "Customer ID"):
            mortgage_agent.get_application_details("not allowed!")

    def test_customer_id_generation_uses_synthetic_cust_prefix(self) -> None:
        generated = mortgage_agent.create_customer_id()
        self.assertRegex(generated, r"^CUST-[0-9A-F]{8}$")

    def test_application_validates_all_fields_and_uses_plural_expenses(self) -> None:
        result = mortgage_agent.create_loan_application(
            customer_id=" CUST-123 ",
            name=" Sam ",
            age=30,
            annual_income=90_000,
            annual_expenses=40_000,
        )
        self.assertIn("customer CUST-123", result)
        self.assertIn("annual_expenses=40000", result)

        invalid_inputs = (
            {"name": " ", "age": 30, "annual_income": 90_000, "annual_expenses": 1},
            {"name": "Sam", "age": 17, "annual_income": 90_000, "annual_expenses": 1},
            {"name": "Sam", "age": 30, "annual_income": 0, "annual_expenses": 0},
            {
                "name": "Sam",
                "age": 30,
                "annual_income": 90_000,
                "annual_expenses": 90_001,
            },
        )
        for values in invalid_inputs:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    mortgage_agent.create_loan_application(
                        customer_id="CUST-123",
                        **values,
                    )

    def test_specialist_names_change_without_changing_persistent_agent_ids(self) -> None:
        self.assertEqual(
            set(mortgage_agent.SPECIALISTS),
            {
                "mortgage_education_specialist",
                "existing_mortgage_specialist",
                "mortgage_application_specialist",
            },
        )
        self.assertEqual(
            {name: spec["agent_id"] for name, spec in mortgage_agent.SPECIALISTS.items()},
            {
                "mortgage_education_specialist": "general",
                "existing_mortgage_specialist": "existing",
                "mortgage_application_specialist": "new_application",
            },
        )
        self.assertIs(
            mortgage_agent.SPECIALISTS["mortgage_education_specialist"]["tools"][0],
            mortgage_agent.retrieve_mortgage_knowledge,
        )


if __name__ == "__main__":
    unittest.main()
