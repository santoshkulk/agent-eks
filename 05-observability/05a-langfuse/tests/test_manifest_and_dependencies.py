from pathlib import Path
import unittest


MODULE_DIR = Path(__file__).resolve().parents[1]


class ManifestAndDependencyTests(unittest.TestCase):
    def test_telemetry_dependencies_are_declared(self) -> None:
        project = (MODULE_DIR / "pyproject.toml").read_text()
        self.assertNotIn("mcp", project)
        self.assertIn('"strands-agents[otel]>=1.56.0,<1.57"', project)

    def test_manifest_combines_telemetry_and_hardening(self) -> None:
        manifest = (MODULE_DIR / "k8s" / "service.template.yaml").read_text()
        for expected in (
            "replicas: 2",
            "serviceAccountName: mortgage-assistant",
            "automountServiceAccountToken: false",
            "runAsNonRoot: true",
            "seccompProfile:",
            "type: RuntimeDefault",
            "allowPrivilegeEscalation: false",
            "readOnlyRootFilesystem: true",
            "drop:\n                - ALL",
            "sizeLimit: 256Mi",
            "kind: PodDisruptionBudget",
            "minAvailable: 1",
            "kind: Service",
            "type: LoadBalancer",
            "name: OTEL_EXPORTER_OTLP_ENDPOINT",
            'value: "__OTEL_EXPORTER_OTLP_ENDPOINT__"',
            "name: OTEL_EXPORTER_OTLP_HEADERS",
            "name: langfuse-otel-auth",
            "name: TELEMETRY_MASK_CONTENT",
            "name: FAULT_INJECTION_ENABLED",
        ):
            self.assertIn(expected, manifest)

    def test_container_includes_telemetry_modules(self) -> None:
        dockerfile = (MODULE_DIR / "Dockerfile").read_text()
        self.assertIn("uv sync --frozen --no-install-project", dockerfile)
        self.assertIn("app/telemetry.py", dockerfile)
        self.assertIn("USER 10001:10001", dockerfile)
        self.assertNotIn("05-observability", dockerfile)

    def test_container_copies_every_runtime_module(self) -> None:
        dockerfile = (MODULE_DIR / "Dockerfile").read_text()
        # Everything the API imports transitively must be in the image.
        runtime = {
            "approvals", "audit", "execution", "ledger", "memory",
            "mortgage_agent", "mortgage_api", "resilience", "service", "store", "telemetry",
        }
        for module in runtime:
            self.assertIn(f"app/{module}.py", dockerfile)
        local = {path.stem for path in (MODULE_DIR / "app").glob("*.py")}
        for module in runtime:
            source = (MODULE_DIR / "app" / f"{module}.py").read_text()
            for other in local - runtime - {"inspect_memory", "invoke_eks", "inspect_audit"}:
                self.assertNotIn(f"import {other}", source)
                self.assertNotIn(f"from {other} import", source)

    def test_manifest_and_deploy_script_agree_on_resume_settings(self) -> None:
        manifest = (MODULE_DIR / "k8s" / "service.template.yaml").read_text()
        deploy = (MODULE_DIR / "scripts" / "deploy-observability.sh").read_text()
        for variable in (
            "APPROVAL_REQUIRED_TOOLS",
            "LEASE_SECONDS",
            "ENABLE_REASONING",
            "SNAPSHOT_HISTORY",
        ):
            self.assertIn(f"name: {variable}", manifest)
            self.assertIn(f"__{variable}__", manifest)
            self.assertIn(f's|__{variable}__|', deploy)

    def test_deploy_smoke_test_verifies_audit_chain_and_replay(self) -> None:
        deploy = (MODULE_DIR / "scripts" / "deploy-observability.sh").read_text()
        self.assertIn("chain_valid", deploy)
        self.assertIn("/executions/", deploy)
        self.assertIn("replaying the request_id", deploy)

    def test_no_new_iam_actions_are_needed(self) -> None:
        # Audit, execution, and ledger use only GetItem/PutItem/Query on the existing
        # table, which the lab 0 pod role already allows.
        source = (MODULE_DIR / "app" / "store.py").read_text()
        for call in ("get_item", "put_item", "query"):
            self.assertIn(f".{call}(", source)
        for forbidden in ("update_item", "delete_item", "scan(", "batch_write"):
            self.assertNotIn(forbidden, source)

    def test_checkpoint_has_no_cross_lab_imports(self) -> None:
        source = "\n".join(path.read_text() for path in (MODULE_DIR / "app").glob("*.py"))
        self.assertNotIn("04-memory", source)
        self.assertNotIn("05-observability", source)
        self.assertNotIn("sys.path", source)


if __name__ == "__main__":
    unittest.main()
