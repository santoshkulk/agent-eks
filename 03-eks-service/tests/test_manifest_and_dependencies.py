from pathlib import Path
import re
import unittest


MODULE_DIR = Path(__file__).resolve().parents[1]


class ManifestAndDependencyTests(unittest.TestCase):
    def test_dependencies_are_bounded(self) -> None:
        project = (MODULE_DIR / "pyproject.toml").read_text()
        self.assertIn('"strands-agents>=1.56.0,<1.57"', project)
        self.assertIn('"strands-agents-tools==0.2.6"', project)
        self.assertIn('"boto3>=1.43.94,<2"', project)

    def test_namespace_enforces_restricted_pods_and_readiness_gates(self) -> None:
        base = (MODULE_DIR / "k8s" / "base.yaml").read_text()
        self.assertIn("pod-security.kubernetes.io/enforce: restricted", base)
        self.assertIn("elbv2.k8s.aws/pod-readiness-gate-inject: enabled", base)
        self.assertIn("automountServiceAccountToken: false", base)

    def test_manifest_hardening_and_availability(self) -> None:
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
            "loadBalancerClass: service.k8s.aws/nlb",
            "loadBalancerSourceRanges:",
            "aws-load-balancer-scheme: internet-facing",
            "name: mortgage-assistant-api-key",
            "name: AWS_REGION\n              value: __AWS_REGION__",
            "name: AWS_DEFAULT_REGION\n              value: __AWS_REGION__",
            "name: KB_PARAMETER_NAME\n              value: __KB_PARAMETER_NAME__",
        ):
            self.assertIn(expected, manifest)

    def test_shutdown_budget_covers_prestop_and_graceful_drain(self) -> None:
        manifest = (MODULE_DIR / "k8s" / "service.template.yaml").read_text()
        dockerfile = (MODULE_DIR / "Dockerfile").read_text()
        grace = int(re.search(r"terminationGracePeriodSeconds: (\d+)", manifest)[1])
        pre_stop = int(re.search(r"preStop:\s+sleep:\s+seconds: (\d+)", manifest)[1])
        drain = int(re.search(r'"--timeout-graceful-shutdown", "(\d+)"', dockerfile)[1])
        self.assertGreaterEqual(grace, pre_stop + drain)

    def test_liveness_survives_a_full_pod(self) -> None:
        # Uvicorn answers 503 to every request, including probes, once the
        # concurrency limit is reached; liveness must tolerate that window.
        manifest = (MODULE_DIR / "k8s" / "service.template.yaml").read_text()
        dockerfile = (MODULE_DIR / "Dockerfile").read_text()
        limit = int(re.search(r'"--limit-concurrency", "(\d+)"', dockerfile)[1])
        self.assertGreaterEqual(limit, 32)
        liveness = manifest.split("livenessProbe:")[1].split("resources:")[0]
        period = int(re.search(r"periodSeconds: (\d+)", liveness)[1])
        failures = int(re.search(r"failureThreshold: (\d+)", liveness)[1])
        self.assertGreaterEqual(period * failures, 120)
        self.assertIn("startupProbe:", manifest)

    def test_template_placeholders_match_deploy_script(self) -> None:
        manifest = (MODULE_DIR / "k8s" / "service.template.yaml").read_text()
        script = (MODULE_DIR / "scripts" / "deploy-application.sh").read_text()
        placeholders = set(re.findall(r"__[A-Z0-9_]+__", manifest))
        rendered = set(re.findall(r's\|(__[A-Z0-9_]+__)\|', script))
        self.assertEqual(placeholders, rendered)

    def test_deploy_script_does_not_print_api_key(self) -> None:
        script = (MODULE_DIR / "scripts" / "deploy-application.sh").read_text()
        self.assertNotRegex(script, r"echo[^\n]*\$API_KEY")
        self.assertNotIn("API key: $API_KEY", script)
        self.assertIn("kubectl get secret mortgage-assistant-api-key", script)

    def test_container_copies_every_runtime_module(self) -> None:
        dockerfile = (MODULE_DIR / "Dockerfile").read_text()
        for module in ("mortgage_agent", "mortgage_api"):
            self.assertIn(f"app/{module}.py", dockerfile)
        self.assertIn("uv sync --frozen --no-install-project", dockerfile)
        self.assertIn("USER 10001:10001", dockerfile)


if __name__ == "__main__":
    unittest.main()
