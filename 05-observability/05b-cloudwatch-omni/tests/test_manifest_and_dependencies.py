from pathlib import Path
import unittest

MODULE_DIR = Path(__file__).resolve().parents[1]


class ManifestAndDependencyTests(unittest.TestCase):
    def test_telemetry_dependencies_remain_declared(self) -> None:
        project = (MODULE_DIR / "pyproject.toml").read_text()
        self.assertNotIn("mcp", project)
        self.assertIn('"strands-agents[otel]>=1.56.0,<1.57"', project)
        self.assertIn('"aws-opentelemetry-distro>=0.20.0"', project)
        self.assertIn('name = "mortgage-workshop-lab-05b"', project)

    def test_manifest_preserves_omni_security_memory_and_adot(self) -> None:
        manifest = (MODULE_DIR / "k8s" / "service.template.yaml").read_text()
        for expected in (
            "replicas: 1",
            "name: mortgage-assistant-omni",
            "app.kubernetes.io/name: mortgage-assistant-omni",
            "serviceAccountName: mortgage-assistant",
            "automountServiceAccountToken: false",
            "runAsNonRoot: true",
            "type: RuntimeDefault",
            "allowPrivilegeEscalation: false",
            "readOnlyRootFilesystem: true",
            "drop:\n                - ALL",
            "sizeLimit: 256Mi",
            "kind: Service",
            "type: LoadBalancer",
            "name: MEMORY_SESSION_TTL_SECONDS",
            "name: AGENT_OBSERVABILITY_ENABLED",
            "name: AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT",
            "name: OTEL_PYTHON_DISTRO",
            "value: aws_distro",
            "name: OTEL_PYTHON_CONFIGURATOR",
            "value: aws_configurator",
            "name: OTEL_RESOURCE_ATTRIBUTES",
            "service.name=mortgage-assistant-omni",
            "aws.log.group.names=__TRACE_LOG_GROUP__",
            "name: OTEL_EXPORTER_OTLP_TRACES_HEADERS",
            "x-aws-log-group=__TRACE_LOG_GROUP__,x-aws-log-stream=mortgage-assistant-omni",
            "name: OTEL_EXPORTER_OTLP_PROTOCOL",
            "value: http/protobuf",
            "name: OTEL_TRACES_EXPORTER",
            "name: OTEL_LOGS_EXPORTER",
            "name: OTEL_METRICS_EXPORTER",
            "name: mortgage-assistant-omni-api-key",
        ):
            self.assertIn(expected, manifest)
        for absent in (
            "APPROVAL_REQUIRED_TOOLS",
            "LEASE_SECONDS",
            "ENABLE_REASONING",
            "SNAPSHOT_HISTORY",
            "FAULT_INJECTION",
            "CREDIT_SCORE",
        ):
            self.assertNotIn(absent, manifest)

    def test_manifest_is_a_second_service_with_no_langfuse_wiring(self) -> None:
        manifest = (MODULE_DIR / "k8s" / "service.template.yaml").read_text()
        self.assertNotIn("name: OTEL_EXPORTER_OTLP_ENDPOINT", manifest)
        self.assertNotIn("name: OTEL_EXPORTER_OTLP_HEADERS", manifest)
        self.assertNotIn("TELEMETRY_MASK_CONTENT", manifest)
        self.assertNotIn("langfuse", manifest.lower())
        self.assertNotIn("PodDisruptionBudget", manifest)
        self.assertNotRegex(manifest, r"(?m)^  name: mortgage-assistant$")
        self.assertNotIn("name: mortgage-assistant-api-key", manifest)

    def test_container_runs_under_adot_auto_instrumentation(self) -> None:
        dockerfile = (MODULE_DIR / "Dockerfile").read_text()
        self.assertIn(
            'ENTRYPOINT ["opentelemetry-instrument", "uvicorn", "mortgage_api:app"]',
            dockerfile,
        )

    def test_container_copies_exact_reduced_runtime_closure(self) -> None:
        dockerfile = (MODULE_DIR / "Dockerfile").read_text()
        self.assertIn("uv sync --frozen --no-install-project", dockerfile)
        self.assertIn("USER 10001:10001", dockerfile)
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

    def test_deploy_smoke_uses_reduced_contract_and_trace_id(self) -> None:
        deploy = (MODULE_DIR / "scripts" / "deploy-omni.sh").read_text()
        for expected in (
            '"prompt": sys.argv[1]',
            '"actor_id": sys.argv[2]',
            '"session_id": sys.argv[3]',
            "curl --fail-with-body",
            'response.get("duration_ms")',
            'response.get("trace_id")',
            "TRACE_LOG_GROUP",
            "OMNI_DOMAIN_URL",
            "xray get-trace-segment-destination",
        ):
            self.assertIn(expected, deploy)
        for absent in (
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
