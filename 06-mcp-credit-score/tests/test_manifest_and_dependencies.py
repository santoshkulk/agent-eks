from pathlib import Path
import unittest


MODULE_DIR = Path(__file__).resolve().parents[1]


class ManifestAndDependencyTests(unittest.TestCase):
    def test_mcp_and_telemetry_dependencies_are_declared(self) -> None:
        project = (MODULE_DIR / "pyproject.toml").read_text()
        self.assertIn('"mcp==2.1.1"', project)
        self.assertIn('"strands-agents[otel]>=1.55.1,<2"', project)

    def test_manifest_combines_mcp_telemetry_and_hardening(self) -> None:
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
            "name: CREDIT_SCORE_MCP_URL",
            "value: __CREDIT_SCORE_MCP_URL__",
            "name: OTEL_EXPORTER_OTLP_ENDPOINT",
            "value: __OTEL_EXPORTER_OTLP_ENDPOINT__",
            "name: OTEL_EXPORTER_OTLP_HEADERS",
            "name: langfuse-otel-auth",
            "name: TELEMETRY_MASK_CONTENT",
            "name: FAULT_INJECTION_ENABLED",
        ):
            self.assertIn(expected, manifest)

    def test_container_includes_mcp_and_telemetry_modules(self) -> None:
        dockerfile = (MODULE_DIR / "Dockerfile").read_text()
        self.assertIn("uv sync --frozen --no-install-project", dockerfile)
        self.assertIn("app/credit_score_mcp.py", dockerfile)
        self.assertIn("app/telemetry.py", dockerfile)
        self.assertIn("USER 10001:10001", dockerfile)
        self.assertNotIn("05-observability", dockerfile)

    def test_checkpoint_has_no_cross_lab_imports(self) -> None:
        source = "\n".join(path.read_text() for path in (MODULE_DIR / "app").glob("*.py"))
        self.assertNotIn("04-memory", source)
        self.assertNotIn("05-observability", source)
        self.assertNotIn("sys.path", source)


if __name__ == "__main__":
    unittest.main()
