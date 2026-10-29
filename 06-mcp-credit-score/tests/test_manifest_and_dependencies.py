from pathlib import Path
import unittest

MODULE_DIR = Path(__file__).resolve().parents[1]


class ManifestAndDependencyTests(unittest.TestCase):
    def test_mcp_and_telemetry_dependencies_remain_declared(self) -> None:
        project = (MODULE_DIR / "pyproject.toml").read_text()
        self.assertIn('"mcp==2.1.1"', project)
        self.assertIn('"strands-agents[otel]>=1.56.0,<1.57"', project)

    def test_manifest_preserves_security_memory_mcp_and_telemetry(self) -> None:
        manifest = (MODULE_DIR / "k8s" / "service.template.yaml").read_text()
        for expected in (
            "replicas: 2",
            "maxUnavailable: 0",
            "serviceAccountName: mortgage-assistant",
            "automountServiceAccountToken: false",
            "runAsNonRoot: true",
            "type: RuntimeDefault",
            "allowPrivilegeEscalation: false",
            "readOnlyRootFilesystem: true",
            "drop:\n                - ALL",
            "sizeLimit: 256Mi",
            "kind: PodDisruptionBudget",
            "minAvailable: 1",
            "loadBalancerSourceRanges:",
            "name: MEMORY_SESSION_TTL_SECONDS",
            "name: CREDIT_SCORE_MCP_URL",
            "name: OTEL_EXPORTER_OTLP_ENDPOINT",
            "name: OTEL_EXPORTER_OTLP_HEADERS",
            "name: langfuse-otel-auth",
            "name: TELEMETRY_MASK_CONTENT",
        ):
            self.assertIn(expected, manifest)
        for absent in (
            "APPROVAL_REQUIRED_TOOLS",
            "LEASE_SECONDS",
            "ENABLE_REASONING",
            "SNAPSHOT_HISTORY",
            "FAULT_INJECTION",
        ):
            self.assertNotIn(absent, manifest)

    def test_container_copies_exact_six_module_runtime_closure(self) -> None:
        dockerfile = (MODULE_DIR / "Dockerfile").read_text()
        runtime = {
            "credit_score_mcp",
            "memory",
            "mortgage_agent",
            "mortgage_api",
            "resilience",
            "telemetry",
        }
        copy_lines = " ".join(
            line.strip().rstrip("\\")
            for line in dockerfile.splitlines()
            if line.startswith("COPY app/") or line.startswith("     app/")
        )
        copied = {
            token.removeprefix("app/").removesuffix(".py")
            for token in copy_lines.split()
            if token.startswith("app/")
        }
        self.assertEqual(copied, runtime)
        for removed in (
            "approvals",
            "audit",
            "execution",
            "inspect_audit",
            "ledger",
            "service",
            "store",
        ):
            self.assertNotIn(f"app/{removed}.py", dockerfile)

    def test_deploy_smoke_uses_client_and_reduced_contract(self) -> None:
        deploy = (MODULE_DIR / "scripts" / "deploy-mcp-integration.sh").read_text()
        for expected in (
            '"$MODULE_DIR/app/invoke_eks.py"',
            "json.load(sys.stdin)",
            'response.get("duration_ms")',
            'response.get("trace_id")',
            "--state-file",
            "--json",
            '"$MODULE_DIR/scripts/explore_credit_score_mcp.py" verify',
        ):
            self.assertIn(expected, deploy)
        for absent in (
            "Authorization: Bearer",
            "--api-key",
            'request_id": sys.argv[4]',
            "/executions/",
            "chain_valid",
            "replay",
            "approval-required",
            "lease-seconds",
            "enable-reasoning",
            "snapshot-history",
            "fault-injection",
            '"abort"',
            '"crash"',
        ):
            self.assertNotIn(absent, deploy.lower())

    def test_runtime_has_no_removed_operational_behavior(self) -> None:
        source = "\n".join(
            (MODULE_DIR / "app" / f"{name}.py").read_text()
            for name in (
                "credit_score_mcp",
                "memory",
                "mortgage_agent",
                "mortgage_api",
                "resilience",
                "telemetry",
            )
        )
        for removed in ("approvals", "audit", "execution", "ledger", "service", "store"):
            self.assertNotIn(f"from {removed} import", source)
            self.assertNotIn(f"import {removed}", source)
        for absent in (
            "record_fault_injection",
            "SNAPSHOT_HISTORY",
            "snapshot_trigger",
            "FAULT_INJECTION",
            "maybe_inject_fault",
            "share_interrupts",
        ):
            self.assertNotIn(absent, source)

    def test_checkpoint_has_no_cross_lab_imports(self) -> None:
        source = "\n".join(path.read_text() for path in (MODULE_DIR / "app").glob("*.py"))
        self.assertNotIn("04-memory", source)
        self.assertNotIn("05-observability", source)
        self.assertNotIn("sys.path", source)


if __name__ == "__main__":
    unittest.main()
