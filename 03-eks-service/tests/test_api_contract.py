import importlib
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))
os.environ.setdefault("MORTGAGE_API_KEY", "test-api-key")
os.environ["AWS_REGION"] = "us-west-2"
os.environ["KB_PARAMETER_NAME"] = (
    "/workshop/mortgage-assistant/bedrock/knowledge-base-id"
)
os.environ.pop("MODEL_ID", None)

import mortgage_agent  # noqa: E402
import mortgage_api  # noqa: E402


class ApiContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(mortgage_api.app)
        self.headers = {"Authorization": "Bearer test-api-key"}

    def test_canonical_model_and_explicit_knowledge_base_configuration(self) -> None:
        self.assertEqual(mortgage_agent.MODEL_ID, "us.anthropic.claude-sonnet-4-6")
        self.assertEqual(
            mortgage_agent.KB_PARAMETER_NAME,
            "/workshop/mortgage-assistant/bedrock/knowledge-base-id",
        )

    def test_startup_fails_without_api_key(self) -> None:
        try:
            for value in ("", "   "):
                with self.subTest(value=value), patch.dict(
                    os.environ, {"MORTGAGE_API_KEY": value}
                ):
                    with self.assertRaisesRegex(RuntimeError, "MORTGAGE_API_KEY"):
                        importlib.reload(mortgage_api)
        finally:
            os.environ["MORTGAGE_API_KEY"] = "test-api-key"
            importlib.reload(mortgage_api)

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

    def test_invoke_propagates_response_request_id(self) -> None:
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
        run_prompt.assert_called_once_with(
            "What is refinancing?", request_id=body["request_id"]
        )

    def test_invoke_rejects_invalid_prompts_without_running_agent(self) -> None:
        with patch("mortgage_api.run_prompt") as run_prompt:
            for body in (
                {"prompt": ""},
                {"prompt": "   \t\n"},
                {"prompt": "x" * 4001},
                {},
            ):
                with self.subTest(body=body):
                    response = self.client.post(
                        "/invoke", headers=self.headers, json=body
                    )
                    self.assertEqual(response.status_code, 422)
        run_prompt.assert_not_called()

    def test_invoke_failure_sanitizes_response_and_log(self) -> None:
        secret_detail = "AccessDeniedException: customer CUST-PRIVATE"
        with patch(
            "mortgage_api.run_prompt", side_effect=RuntimeError(secret_detail)
        ), self.assertLogs("mortgage_api", level="ERROR") as captured:
            response = self.client.post(
                "/invoke", headers=self.headers, json={"prompt": "What is refinancing?"}
            )
        self.assertEqual(response.status_code, 500)
        detail = response.json()["detail"]
        self.assertIn("request_id=", detail)
        self.assertNotIn(secret_detail, detail)
        self.assertNotIn(secret_detail, "\n".join(captured.output))
        self.assertIn("error_type=RuntimeError", "\n".join(captured.output))

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


class KnowledgeBaseContractTests(unittest.TestCase):
    def setUp(self) -> None:
        mortgage_agent.get_knowledge_base_id.cache_clear()
        self.addCleanup(mortgage_agent.get_knowledge_base_id.cache_clear)

    def test_reads_configured_parameter_in_configured_region(self) -> None:
        with patch("mortgage_agent.boto3.client") as client:
            client.return_value.get_parameter.return_value = {
                "Parameter": {"Value": " KB12345 \n"}
            }
            self.assertEqual(mortgage_agent.get_knowledge_base_id(), "KB12345")
        client.assert_called_once_with("ssm", region_name="us-west-2")
        client.return_value.get_parameter.assert_called_once_with(
            Name=mortgage_agent.KB_PARAMETER_NAME
        )

    def test_knowledge_base_parameter_name_is_required(self) -> None:
        with patch.object(mortgage_agent, "KB_PARAMETER_NAME", None):
            with self.assertRaisesRegex(RuntimeError, "KB_PARAMETER_NAME"):
                mortgage_agent.get_knowledge_base_id()

    def test_aws_region_is_required(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "AWS_REGION"):
                mortgage_agent._get_aws_region()

    def test_empty_parameter_is_an_error(self) -> None:
        with patch("mortgage_agent.boto3.client") as client:
            client.return_value.get_parameter.return_value = {
                "Parameter": {"Value": "  "}
            }
            with self.assertRaises(RuntimeError):
                mortgage_agent.get_knowledge_base_id()

    def test_fixed_retrieval_wrapper_uses_resolved_configuration(self) -> None:
        tool_result = {"status": "success", "content": [{"text": " grounded "}]}
        with patch(
            "mortgage_agent.get_knowledge_base_id", return_value="KB12345"
        ), patch("mortgage_agent.retrieve", return_value=tool_result) as retrieve:
            result = mortgage_agent.retrieve_mortgage_knowledge("  refinance  ")
        self.assertEqual(result, "grounded")
        request = retrieve.call_args.args[0]
        self.assertRegex(request["toolUseId"], r"^mortgage-knowledge-[0-9a-f]{32}$")
        self.assertEqual(
            request["input"],
            {
                "text": "refinance",
                "knowledgeBaseId": "KB12345",
                "region": "us-west-2",
                "numberOfResults": 5,
                "score": 0.4,
            },
        )

    def test_retrieval_wrapper_calls_the_real_retrieve_tool(self) -> None:
        # Only the AWS client is faked, so a non-callable `retrieve` import fails here.
        runtime = MagicMock()
        runtime.retrieve.return_value = {
            "retrievalResults": [
                {
                    "content": {"text": "A 15-year term builds equity faster."},
                    "score": 0.49,
                    "location": {},
                }
            ]
        }
        with patch(
            "mortgage_agent.get_knowledge_base_id", return_value="KB12345"
        ), patch("strands_tools.retrieve.boto3.client", return_value=runtime):
            result = mortgage_agent.retrieve_mortgage_knowledge("15-year benefits")
        self.assertIn("builds equity faster", result)
        runtime.retrieve.assert_called_once()
        self.assertEqual(runtime.retrieve.call_args.kwargs["knowledgeBaseId"], "KB12345")

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
        self.assertEqual(details["customer_id"], "CUST-123_test")
        self.assertEqual(details["account_number"], "MORTGAGE-CUST-123_test")
        self.assertGreater(details["next_payment_amount"], 0)
        statuses = mortgage_agent.get_mortgage_application_document_statuses(
            "CUST-123_test"
        )
        application = mortgage_agent.get_application_details("CUST-123_test")
        self.assertEqual(statuses["customer_id"], "CUST-123_test")
        self.assertEqual(application["customer_id"], "CUST-123_test")

    def test_customer_id_rejects_invalid_values(self) -> None:
        for value in ("", "-starts-with-hyphen", "contains space", "a" * 65):
            with self.subTest(value=value), self.assertRaises(ValueError):
                mortgage_agent.get_mortgage_details(value)

    def test_generated_customer_id_has_synthetic_shape(self) -> None:
        self.assertRegex(mortgage_agent.generate_customer_id(), r"^CUST-[0-9A-F]{8}$")

    def test_prepare_application_is_validated_and_non_persistent(self) -> None:
        result = mortgage_agent.prepare_mortgage_application(
            "CUST-123", " Synthetic Person ", 35, 120000, 45000
        )
        self.assertIn("prepared for Synthetic Person", result)
        self.assertIn("has not been submitted or persisted", result)

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
                mortgage_agent.prepare_mortgage_application(*arguments)


class AgentAndTraceContractTests(unittest.TestCase):
    def test_supervisor_uses_production_names_and_renamed_tools(self) -> None:
        with patch("mortgage_agent.Agent") as agent:
            mortgage_agent.create_supervisor_agent()
        configuration = agent.call_args.kwargs
        self.assertEqual(configuration["name"], "mortgage_supervisor")
        tool_names = [tool.__name__ for tool in configuration["tools"]]
        self.assertEqual(
            tool_names[:3],
            [
                "answer_general_mortgage_questions",
                "answer_existing_mortgage_questions",
                "answer_mortgage_application_questions",
            ],
        )
        self.assertIn("exactly one matching", configuration["system_prompt"])
        self.assertFalse(hasattr(mortgage_agent, "create_loan_application"))
        self.assertFalse(hasattr(mortgage_agent, "create_customer_id"))
        self.assertFalse(hasattr(mortgage_agent, "get_mortgage_app_doc_status"))

    def test_general_specialist_receives_only_fixed_retrieval_wrapper(self) -> None:
        with patch("mortgage_agent.Agent") as agent:
            agent.return_value.return_value = "answer"
            mortgage_agent.answer_general_mortgage_questions("question")
        configuration = agent.call_args.kwargs
        self.assertEqual(configuration["name"], "mortgage_education_specialist")
        self.assertEqual(configuration["tools"], [mortgage_agent.retrieve_mortgage_knowledge])

    def test_trace_logs_only_allowlisted_metadata_and_ignores_model_text(self) -> None:
        callback = mortgage_agent.create_trace_callback("mortgage_supervisor")
        secret = "CUST-SECRET annual_income=999999"
        token = mortgage_agent._REQUEST_ID.set("request-123")
        try:
            with self.assertLogs("mortgage_agent", level="INFO") as captured:
                callback(event={"contentBlockDelta": {"delta": {"text": secret}}})
                callback(
                    event={
                        "contentBlockStart": {
                            "start": {
                                "toolUse": {
                                    "name": "answer_existing_mortgage_questions",
                                    "input": {"customer_id": secret},
                                }
                            }
                        }
                    }
                )
        finally:
            mortgage_agent._REQUEST_ID.reset(token)
        output = "\n".join(captured.output)
        self.assertIn("request_id=request-123", output)
        self.assertIn("[delegate]", output)
        self.assertIn("specialist=existing_mortgage_specialist", output)
        self.assertNotIn(secret, output)

    def test_run_prompt_scopes_and_resets_trace_request_id(self) -> None:
        observed_ids: list[str | None] = []

        def create_agent():
            def invoke(prompt: str) -> str:
                observed_ids.append(mortgage_agent._REQUEST_ID.get())
                return prompt

            return invoke

        with patch("mortgage_agent.create_supervisor_agent", side_effect=create_agent):
            self.assertEqual(
                mortgage_agent.run_prompt(" hello ", request_id="request-456"), "hello"
            )
        self.assertEqual(observed_ids, ["request-456"])
        self.assertIsNone(mortgage_agent._REQUEST_ID.get())

    def test_run_prompt_keeps_concurrent_request_ids_isolated(self) -> None:
        barrier = threading.Barrier(2)
        observed: dict[str, str | None] = {}
        lock = threading.Lock()

        def create_agent():
            def invoke(prompt: str) -> str:
                barrier.wait(timeout=5)
                with lock:
                    observed[prompt] = mortgage_agent._REQUEST_ID.get()
                return prompt

            return invoke

        with patch("mortgage_agent.create_supervisor_agent", side_effect=create_agent):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(mortgage_agent.run_prompt, "first", "request-1"),
                    executor.submit(mortgage_agent.run_prompt, "second", "request-2"),
                ]
                self.assertEqual([future.result() for future in futures], ["first", "second"])

        self.assertEqual(
            observed, {"first": "request-1", "second": "request-2"}
        )
        self.assertIsNone(mortgage_agent._REQUEST_ID.get())

    def test_run_prompt_resets_context_after_failure(self) -> None:
        def invoke(prompt: str) -> str:
            raise RuntimeError("failure")

        with patch("mortgage_agent.create_supervisor_agent", return_value=invoke):
            with self.assertRaises(RuntimeError):
                mortgage_agent.run_prompt("hello", request_id="request-failure")
        self.assertIsNone(mortgage_agent._REQUEST_ID.get())

    def test_run_prompt_rejects_blank_prompt(self) -> None:
        with self.assertRaises(ValueError):
            mortgage_agent.run_prompt("   ")


if __name__ == "__main__":
    unittest.main()
