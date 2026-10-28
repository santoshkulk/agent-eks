from pathlib import Path
import unittest

MODULE_DIR = Path(__file__).resolve().parents[1]


class ManifestAndDependencyTests(unittest.TestCase):
    def test_telemetry_dependencies_remain_declared(self) -> None:
        project = (MODULE_DIR / "pyproject.toml").read_text()
        self.assertNotIn("mcp", project)
        self.assertIn('name = "mortgage-workshop-lab-05cc"', project)
        self.assertIn('"strands-agents[otel]>=1.56.0,<1.57"', project)

    def test_manifests_preserve_security_availability_and_telemetry(self) -> None:
        base = (MODULE_DIR / "k8s" / "base.yaml").read_text()
        manifest = (MODULE_DIR / "k8s" / "service.template.yaml").read_text()
        self.assertIn("automountServiceAccountToken: false", base)
        deploy = (MODULE_DIR / "scripts" / "deploy-cost-attribution.sh").read_text()
        for expected in (
            '"workshop.aws/owner-token": owner_token',
            '"pod-security.kubernetes.io/enforce": "restricted"',
            '"elbv2.k8s.aws/pod-readiness-gate-inject": "enabled"',
        ):
            self.assertIn(expected, deploy)
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
            "name: OTEL_EXPORTER_OTLP_ENDPOINT",
            "name: OTEL_EXPORTER_OTLP_HEADERS",
            "name: langfuse-otel-auth",
            "name: TELEMETRY_MASK_CONTENT",
        ):
            self.assertIn(expected, manifest)

    def test_container_copies_exact_reduced_runtime_closure(self) -> None:
        dockerfile = (MODULE_DIR / "Dockerfile").read_text()
        runtime = {"memory", "mortgage_agent", "mortgage_api", "resilience", "telemetry"}
        for module in runtime:
            self.assertIn(f"app/{module}.py", dockerfile)
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

        local = {path.stem for path in (MODULE_DIR / "app").glob("*.py")}
        for module in runtime:
            source = (MODULE_DIR / "app" / f"{module}.py").read_text()
            for other in local - runtime - {"inspect_memory", "invoke_eks"}:
                self.assertNotIn(f"import {other}", source)
                self.assertNotIn(f"from {other} import", source)

    def test_deploy_smoke_uses_client_and_reduced_contract(self) -> None:
        deploy = (MODULE_DIR / "scripts" / "deploy-cost-attribution.sh").read_text()
        for expected in (
            '"$MODULE_DIR/app/invoke_eks.py"',
            "json.load(sys.stdin)",
            'response.get("duration_ms")',
            'response.get("trace_id")',
            "--json",
        ):
            self.assertIn(expected, deploy)
        for absent in (
            "Authorization: Bearer",
            "--api-key",
            "/executions/",
            "chain_valid",
            "replay",
            "fault-injection",
        ):
            self.assertNotIn(absent, deploy.lower())

    def test_deploy_has_isolated_fresh_and_overlay_rollback_paths(self) -> None:
        deploy = (MODULE_DIR / "scripts" / "deploy-cost-attribution.sh").read_text()
        for expected in (
            'DEPLOY_MODE="fresh"',
            'DEPLOY_MODE="overlay"',
            'FRESH_NAMESPACE_CREATED="true"',
            "trap cleanup_on_exit EXIT",
            'workshop.aws/owner-token',
            'kubectl apply -f "$MODULE_DIR/k8s/base.yaml"',
            '--from-file=api-key=/dev/stdin',
            '--from-file=otlp-headers=/dev/stdin',
            'kubectl apply -f "$SERVICE_MANIFEST"',
            'kubectl set image "deployment/$DEPLOYMENT"',
        ):
            self.assertIn(expected, deploy)
        self.assertEqual(deploy.count('kubectl set image "deployment/$DEPLOYMENT"'), 2)
        for forbidden in (
            "secretsmanager get-secret-value",
            "--from-literal",
            ".data.api-key",
        ):
            self.assertNotIn(forbidden, deploy)

    def test_runtime_has_no_deleted_imports_or_fault_event(self) -> None:
        source = "\n".join(
            (MODULE_DIR / "app" / f"{name}.py").read_text()
            for name in ("memory", "mortgage_agent", "mortgage_api", "resilience", "telemetry")
        )
        for removed in ("approvals", "audit", "execution", "ledger", "service", "store"):
            self.assertNotIn(f"from {removed} import", source)
            self.assertNotIn(f"import {removed}", source)
        self.assertNotIn("record_fault_injection", source)
        self.assertNotIn('add_event("fault_injection"', source)
        self.assertNotIn("SNAPSHOT_HISTORY", source)
        self.assertNotIn("FAULT_INJECTION", source)

    def test_checkpoint_has_no_cross_lab_imports(self) -> None:
        source = "\n".join(path.read_text() for path in (MODULE_DIR / "app").glob("*.py"))
        self.assertNotIn("04-memory", source)
        self.assertNotIn("06-mcp-credit-score", source)
        self.assertNotIn("sys.path", source)


if __name__ == "__main__":
    unittest.main()
