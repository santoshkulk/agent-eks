from pathlib import Path
import unittest

MODULE_DIR = Path(__file__).resolve().parents[1]


class ManifestAndDependencyTests(unittest.TestCase):
    def test_telemetry_dependencies_remain_declared(self) -> None:
        project = (MODULE_DIR / "pyproject.toml").read_text()
        self.assertNotIn("mcp", project)
        self.assertIn('name = "mortgage-workshop-lab-05cc"', project)
        self.assertIn('"strands-agents[otel]>=1.56.0,<1.57"', project)

    def test_pilot_has_no_kubernetes_resource_manifests(self) -> None:
        manifests = list((MODULE_DIR / "k8s").glob("*.yaml"))
        self.assertEqual(manifests, [])

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
            'json.load(sys.stdin)',
            'response.get("duration_ms")',
            'response.get("trace_id")',
            "--json",
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

    def test_deploy_is_an_image_only_overlay_with_failure_restore(self) -> None:
        deploy = (MODULE_DIR / "scripts" / "deploy-cost-attribution.sh").read_text()
        self.assertIn("PREVIOUS_IMAGE=", deploy)
        self.assertIn("cleanup_on_exit()", deploy)
        self.assertIn("trap cleanup_on_exit EXIT", deploy)
        self.assertIn('MUTATION_STARTED="true"', deploy)
        self.assertIn('DEPLOY_SUCCEEDED="true"', deploy)
        self.assertEqual(deploy.count('kubectl set image "deployment/$DEPLOYMENT"'), 2)
        for forbidden in (
            "kubectl apply",
            "kubectl create secret",
            "--from-literal",
            "secretsmanager get-secret-value",
            "SERVICE_MANIFEST",
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
        self.assertNotIn("snapshot_trigger", source)
        self.assertNotIn("FAULT_INJECTION", source)
        self.assertNotIn("maybe_inject_fault", source)

    def test_checkpoint_has_no_cross_lab_imports(self) -> None:
        source = "\n".join(path.read_text() for path in (MODULE_DIR / "app").glob("*.py"))
        self.assertNotIn("04-memory", source)
        self.assertNotIn("06-mcp-credit-score", source)
        self.assertNotIn("sys.path", source)


if __name__ == "__main__":
    unittest.main()
