from pathlib import Path
import unittest


MODULE_DIR = Path(__file__).resolve().parents[1]


class ManifestAndDependencyTests(unittest.TestCase):
    def test_dependencies_are_declared_without_mcp_or_otel(self) -> None:
        project = (MODULE_DIR / "pyproject.toml").read_text()
        self.assertIn('"strands-agents>=1.56.0,<1.57"', project)
        self.assertIn('"strands-agents-tools==0.2.6"', project)
        self.assertIn('"strands-dynamodb-storage==0.1.2"', project)
        self.assertNotIn("mcp", project)
        self.assertNotIn("otel", project)

    def test_manifest_preserves_availability_security_and_memory_env(self) -> None:
        manifest = (MODULE_DIR / "k8s" / "service.template.yaml").read_text()
        for expected in (
            "replicas: 2",
            "maxUnavailable: 0",
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
            "type: LoadBalancer",
            "loadBalancerSourceRanges:",
            "name: MEMORY_SESSION_TTL_SECONDS",
            "name: MORTGAGE_API_KEY",
        ):
            self.assertIn(expected, manifest)
        for absent in (
            "FAULT_INJECTION",
            "APPROVAL",
            "LEASE_SECONDS",
            "ENABLE_REASONING",
            "SNAPSHOT_HISTORY",
            "CREDIT_SCORE",
            "OTEL_",
            "TELEMETRY",
            "langfuse",
        ):
            self.assertNotIn(absent, manifest)

    def test_container_is_locked_non_root_and_private_to_the_service_boundary(self) -> None:
        dockerfile = (MODULE_DIR / "Dockerfile").read_text()
        self.assertIn("uv sync --frozen --no-install-project", dockerfile)
        self.assertIn("USER 10001:10001", dockerfile)
        self.assertIn('CMD ["--host", "0.0.0.0"', dockerfile)
        self.assertNotIn("credit_score_mcp", dockerfile)
        self.assertNotIn("telemetry", dockerfile)

    def test_container_copies_complete_reduced_runtime_closure(self) -> None:
        dockerfile = (MODULE_DIR / "Dockerfile").read_text()
        runtime = {"memory", "mortgage_agent", "mortgage_api", "resilience"}
        for module in runtime:
            self.assertIn(f"app/{module}.py", dockerfile)
        for removed in (
            "approvals",
            "audit",
            "execution",
            "ledger",
            "service",
            "store",
        ):
            self.assertNotIn(f"app/{removed}.py", dockerfile)

        local = {path.stem for path in (MODULE_DIR / "app").glob("*.py")}
        for module in runtime:
            source = (MODULE_DIR / "app" / f"{module}.py").read_text()
            for other in local - runtime - {"inspect_memory", "invoke_eks"}:
                self.assertNotIn(f"import {other}", source)
                self.assertNotIn(f"from {other} import", source)

    def test_deploy_smoke_request_uses_reduced_contract(self) -> None:
        deploy = (MODULE_DIR / "scripts" / "deploy-memory.sh").read_text()
        self.assertIn('"prompt": sys.argv[1]', deploy)
        self.assertIn('"actor_id": sys.argv[2]', deploy)
        self.assertIn('"session_id": sys.argv[3]', deploy)
        self.assertIn("curl --fail-with-body", deploy)
        self.assertIn('response.get("duration_ms")', deploy)
        for absent in (
            "request_id\": sys.argv[4]",
            "/executions/",
            "chain_valid",
            "replay",
            "FAULT_INJECTION",
            "APPROVAL_REQUIRED_TOOLS",
            "LEASE_SECONDS",
            "ENABLE_REASONING",
            "SNAPSHOT_HISTORY",
        ):
            self.assertNotIn(absent, deploy)

    def test_checkpoint_has_no_cross_lab_imports(self) -> None:
        source = "\n".join(path.read_text() for path in (MODULE_DIR / "app").glob("*.py"))
        self.assertNotIn("05-observability", source)
        self.assertNotIn("06-mcp-credit-score", source)
        self.assertNotIn("sys.path", source)


if __name__ == "__main__":
    unittest.main()
