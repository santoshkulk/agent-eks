import importlib.util
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import Mock


MODULE_DIR = Path(__file__).resolve().parents[1]
APP_DIR = MODULE_DIR / "app"
sys.path.insert(0, str(APP_DIR))

import inspect_memory  # noqa: E402


def load_hydrate_memory():
    module_path = MODULE_DIR / "scripts" / "hydrate_memory.py"
    spec = importlib.util.spec_from_file_location("hydrate_memory", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hydrate_memory = load_hydrate_memory()


class ResourceDiscoveryTests(unittest.TestCase):
    def test_inspector_reads_canonical_ssm_parameter(self) -> None:
        ssm_client = Mock()
        ssm_client.get_parameter.return_value = {
            "Parameter": {"Value": " memory-table "}
        }
        value = inspect_memory.ssm_parameter(
            ssm_client,
            inspect_memory.MEMORY_TABLE_PARAMETER_NAME,
        )
        self.assertEqual(value, "memory-table")
        ssm_client.get_parameter.assert_called_once_with(
            Name="/workshop/mortgage-assistant/memory/table-name"
        )

    def test_hydrator_reads_canonical_ssm_parameter(self) -> None:
        ssm_client = Mock()
        ssm_client.get_parameter.return_value = {
            "Parameter": {"Value": " vector-index "}
        }
        value = hydrate_memory.ssm_parameter(
            ssm_client,
            hydrate_memory.MEMORY_VECTOR_INDEX_PARAMETER_NAME,
        )
        self.assertEqual(value, "vector-index")
        ssm_client.get_parameter.assert_called_once_with(
            Name="/workshop/mortgage-assistant/memory/vector-index-name"
        )

    def test_empty_ssm_parameter_is_rejected(self) -> None:
        ssm_client = Mock()
        ssm_client.get_parameter.return_value = {"Parameter": {"Value": " "}}
        with self.assertRaisesRegex(RuntimeError, "is empty"):
            inspect_memory.ssm_parameter(
                ssm_client,
                inspect_memory.MEMORY_TABLE_PARAMETER_NAME,
            )

    def test_direct_overrides_bypass_ssm_lookup(self) -> None:
        ssm_client = Mock()
        self.assertEqual(
            inspect_memory.resource_name(
                "direct-table",
                ssm_client,
                inspect_memory.MEMORY_TABLE_PARAMETER_NAME,
            ),
            "direct-table",
        )
        self.assertEqual(
            hydrate_memory.resource_name(
                "direct-index",
                ssm_client,
                hydrate_memory.MEMORY_VECTOR_INDEX_PARAMETER_NAME,
            ),
            "direct-index",
        )
        ssm_client.get_parameter.assert_not_called()

    def test_runtime_files_have_no_stack_dependency(self) -> None:
        runtime_paths = [
            MODULE_DIR / "scripts" / "deploy-mcp-integration.sh",
            MODULE_DIR / "scripts" / "cleanup-mcp-integration.sh",
            MODULE_DIR / "scripts" / "hydrate_memory.py",
            MODULE_DIR / "app" / "inspect_memory.py",
        ]
        combined = "\n".join(path.read_text() for path in runtime_paths).lower()
        for legacy_value in (
            "cloudformation",
            "describe-stacks",
            "--base-stack-name",
            "mortgage-assistant-workshop",
        ):
            self.assertNotIn(legacy_value, combined)

    def test_deploy_discovers_all_canonical_resources(self) -> None:
        deploy = (MODULE_DIR / "scripts" / "deploy-mcp-integration.sh").read_text()
        for parameter_path in (
            "/workshop/mortgage-assistant/eks/cluster-name",
            "/workshop/mortgage-assistant/ecr/repository-uri",
            "/workshop/mortgage-assistant/memory/table-name",
            "/workshop/mortgage-assistant/memory/vector-index-name",
            "/workshop/mortgage-assistant/bedrock/knowledge-base-id",
            "/workshop/mortgage-assistant/mcp/credit-score-url",
            "/workshop/mortgage-assistant/langfuse/otlp-endpoint",
            "/workshop/mortgage-assistant/langfuse/url",
        ):
            self.assertIn(parameter_path, deploy)
        self.assertIn("lab06-agent-", deploy)
        self.assertIn('uv run --project "$MODULE_DIR" --frozen', deploy)
        self.assertIn('"$MODULE_DIR/scripts/explore_credit_score_mcp.py" verify', deploy)
        self.assertIn("kubectl get secret langfuse-otel-auth", deploy)
        self.assertIn("{.data.otlp-headers}", deploy)
        self.assertIn("API key must not be empty", deploy)
        self.assertNotIn("secretsmanager get-secret-value", deploy)
        self.assertNotIn("API key: $API_KEY", deploy)
        self.assertIn("cannot be rendered safely", deploy)
        self.assertIn("unresolved template placeholders", deploy)
        validator = (MODULE_DIR / "scripts" / "validate_otlp_headers.py").read_text()
        self.assertIn("x-langfuse-ingestion-version", validator)
        self.assertIn("validate=True", validator)
        self.assertIn("validate_otlp_headers.py", deploy)
        self.assertIn("SMOKE_TRACE_ID", deploy)
        for placeholder in (
            "__CREDIT_SCORE_MCP_URL__",
            "__OTEL_EXPORTER_OTLP_ENDPOINT__",
            "__TELEMETRY_MASK_CONTENT__",
            "__FAULT_INJECTION_ENABLED__",
            "__FAULT_INJECTION_TOOL__",
            "__FAULT_INJECTION_MODE__",
            "__FAULT_INJECTION_DELAY_SECONDS__",
        ):
            self.assertIn(placeholder, deploy)

    def test_scripts_do_not_own_credit_services(self) -> None:
        deploy = (MODULE_DIR / "scripts" / "deploy-mcp-integration.sh").read_text()
        cleanup = (MODULE_DIR / "scripts" / "cleanup-mcp-integration.sh").read_text()
        combined = f"{deploy}\n{cleanup}"
        self.assertNotRegex(
            combined,
            re.compile(r"kubectl\s+(apply|create|delete|patch|replace).*credit-services"),
        )
        self.assertNotIn("kubectl apply -f credit-services", combined)
        self.assertIn("kubectl get deployment credit-score-mcp", deploy)
        self.assertIn("kubectl get endpoints credit-score-mcp", deploy)
        self.assertIn("kubectl delete namespace mortgage-assistant", cleanup)

    def test_python_utilities_create_one_reusable_ssm_client(self) -> None:
        for path in (
            MODULE_DIR / "scripts" / "hydrate_memory.py",
            MODULE_DIR / "app" / "inspect_memory.py",
        ):
            self.assertEqual(path.read_text().count('session.client("ssm"'), 1)


if __name__ == "__main__":
    unittest.main()
