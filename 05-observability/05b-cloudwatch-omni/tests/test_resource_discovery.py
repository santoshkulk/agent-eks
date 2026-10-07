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
            MODULE_DIR / "scripts" / "deploy-omni.sh",
            MODULE_DIR / "scripts" / "cleanup-omni.sh",
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
        deploy = (MODULE_DIR / "scripts" / "deploy-omni.sh").read_text()
        for parameter_path in (
            "/workshop/mortgage-assistant/eks/cluster-name",
            "/workshop/mortgage-assistant/ecr/repository-uri",
            "/workshop/mortgage-assistant/memory/table-name",
            "/workshop/mortgage-assistant/memory/vector-index-name",
            "/workshop/mortgage-assistant/bedrock/knowledge-base-id",
            "/workshop/mortgage-assistant/cloudwatch/trace-log-group",
            "/workshop/mortgage-assistant/cloudwatch/omni-domain-url",
        ):
            self.assertIn(parameter_path, deploy)
        self.assertIn("lab05b-", deploy)
        self.assertIn('uv run --project "$MODULE_DIR" --frozen', deploy)
        self.assertIn("xray get-trace-segment-destination", deploy)
        self.assertIn('"CloudWatchLogs"$\'\\t\'"ACTIVE"', deploy)
        self.assertIn("logs create-log-stream", deploy)
        self.assertIn("--log-stream-name mortgage-assistant-omni", deploy)
        self.assertIn("API key must not be empty", deploy)
        self.assertNotIn("API key: $API_KEY", deploy)
        self.assertIn("cannot be rendered safely", deploy)
        self.assertIn("unresolved template placeholders", deploy)
        self.assertIn("SMOKE_TRACE_ID", deploy)
        for placeholder in (
            "__TRACE_LOG_GROUP__",
            "__FAULT_INJECTION_ENABLED__",
            "__FAULT_INJECTION_TOOL__",
            "__FAULT_INJECTION_MODE__",
            "__FAULT_INJECTION_DELAY_SECONDS__",
        ):
            self.assertIn(placeholder, deploy)

    def test_every_template_placeholder_is_substituted_by_deploy(self) -> None:
        deploy = (MODULE_DIR / "scripts" / "deploy-omni.sh").read_text()
        template = (MODULE_DIR / "k8s" / "service.template.yaml").read_text()
        placeholders = set(re.findall(r"__[A-Z0-9_]+__", template))
        self.assertTrue(placeholders)
        for placeholder in placeholders:
            self.assertIn(f"s|{placeholder}|", deploy)

    def test_no_langfuse_credentials_or_otlp_endpoint_are_involved(self) -> None:
        deploy = (MODULE_DIR / "scripts" / "deploy-omni.sh").read_text()
        template = (MODULE_DIR / "k8s" / "service.template.yaml").read_text()
        for text in (deploy, template):
            # Comments may compare against Lab 5a; code and manifest values may not.
            text = "\n".join(
                line for line in text.splitlines() if not line.lstrip().startswith("#")
            )
            self.assertNotIn("langfuse", text.lower())
            self.assertNotIn("secretsmanager", text)
            self.assertNotIn("__OTEL_EXPORTER_OTLP_ENDPOINT__", text)
            self.assertNotIn("OTLP_HEADERS|", text)

    def test_omni_resources_are_separate_from_lab_5a(self) -> None:
        deploy = (MODULE_DIR / "scripts" / "deploy-omni.sh").read_text()
        cleanup = (MODULE_DIR / "scripts" / "cleanup-omni.sh").read_text()
        client = (MODULE_DIR / "app" / "invoke_eks.py").read_text()
        self.assertIn("deployment/mortgage-assistant-omni", deploy)
        self.assertIn("service mortgage-assistant-omni", deploy)
        self.assertIn("mortgage-assistant-omni-api-key", deploy)
        self.assertIn('DEFAULT_SERVICE = "mortgage-assistant-omni"', client)
        self.assertIn('DEFAULT_SECRET = "mortgage-assistant-omni-api-key"', client)
        # Cleanup removes only the Omni objects, never the shared namespace.
        self.assertIn("deployment,service mortgage-assistant-omni", cleanup)
        self.assertIn("secret mortgage-assistant-omni-api-key", cleanup)
        self.assertNotIn("delete namespace", cleanup)

    def test_deploy_script_ports_the_lab_5a_options(self) -> None:
        deploy = (MODULE_DIR / "scripts" / "deploy-omni.sh").read_text()
        for option in (
            "--approval-required-tools",
            "--lease-seconds",
            "--enable-reasoning",
            "--no-snapshot-history",
            "--image-uri",
            "--fault-injection-mode",
        ):
            self.assertIn(option, deploy)
        self.assertIn("'delay', 'error', 'abort', or 'crash'", deploy)
        self.assertIn("scripts/check_memory_ready.py", deploy)
        self.assertNotIn("Table.VectorIndexes", deploy)
        self.assertIn("READY_STREAK", deploy)
        self.assertIn("Could not detect an IPv4 address", deploy)

    def test_python_utilities_create_one_reusable_ssm_client(self) -> None:
        for path in (
            MODULE_DIR / "scripts" / "hydrate_memory.py",
            MODULE_DIR / "app" / "inspect_memory.py",
        ):
            self.assertEqual(path.read_text().count('session.client("ssm"'), 1)


if __name__ == "__main__":
    unittest.main()
