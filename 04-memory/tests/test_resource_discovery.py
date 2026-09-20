import importlib.util
from pathlib import Path
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
            MODULE_DIR / "scripts" / "deploy-memory.sh",
            MODULE_DIR / "scripts" / "cleanup-memory.sh",
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

    def test_python_utilities_create_one_reusable_ssm_client(self) -> None:
        utility_paths = [
            MODULE_DIR / "scripts" / "hydrate_memory.py",
            MODULE_DIR / "app" / "inspect_memory.py",
        ]
        for path in utility_paths:
            self.assertEqual(path.read_text().count('session.client("ssm"'), 1)

    def test_shell_scripts_preserve_lab_name_and_canonical_paths(self) -> None:
        deploy = (MODULE_DIR / "scripts" / "deploy-memory.sh").read_text()
        cleanup = (MODULE_DIR / "scripts" / "cleanup-memory.sh").read_text()
        self.assertIn("Lab 04", deploy)
        self.assertIn("Lab 04", cleanup)
        self.assertIn(
            "/workshop/mortgage-assistant/eks/cluster-name",
            deploy,
        )
        self.assertIn(
            "/workshop/mortgage-assistant/ecr/repository-uri",
            deploy,
        )
        self.assertIn(
            "/workshop/mortgage-assistant/bedrock/knowledge-base-id",
            deploy,
        )
        self.assertIn(
            "/workshop/mortgage-assistant/eks/cluster-name",
            cleanup,
        )


if __name__ == "__main__":
    unittest.main()
