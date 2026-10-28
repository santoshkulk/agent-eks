"""Execute the deployment script against fake commands to verify image rollback."""

import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

MODULE_DIR = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = MODULE_DIR / "scripts" / "deploy-cost-attribution.sh"


class DeployRollbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.command_log = self.root / "kubectl.log"
        self.set_image_count = self.root / "set-image-count"
        self._write_fake_commands()

    def _write_executable(self, name: str, source: str) -> None:
        path = self.bin_dir / name
        path.write_text(textwrap.dedent(source).lstrip())
        path.chmod(0o755)

    def _write_fake_commands(self) -> None:
        self._write_executable(
            "aws",
            r"""
            #!/usr/bin/env bash
            set -euo pipefail
            args=" $* "
            if [[ "$args" == *" ssm get-parameter "* ]]; then
              case "$args" in
                *"/eks/cluster-name"*) printf 'test-cluster' ;;
                *"/ecr/repository-uri"*) printf 'example.test/repository' ;;
                *"/memory/table-name"*) printf 'memory-table' ;;
                *"/memory/vector-index-name"*) printf 'vector-index' ;;
                *"/bedrock/knowledge-base-id"*) printf 'KB123' ;;
                *"/langfuse/url"*) printf 'http://langfuse.test' ;;
                *) exit 2 ;;
              esac
            elif [[ "$args" == *" eks update-kubeconfig "* ]]; then
              exit 0
            else
              echo "Unexpected aws call: $*" >&2
              exit 2
            fi
            """,
        )
        self._write_executable(
            "uv",
            r"""
            #!/usr/bin/env bash
            set -euo pipefail
            args=" $* "
            if [[ "$args" == *"check_memory_ready.py"* ]]; then
              printf 'ACTIVE ACTIVE\n'
            elif [[ "$args" == *"app/invoke_eks.py"* ]]; then
              if [[ "${FAIL_STAGE:-}" == "smoke" ]]; then
                echo "simulated smoke failure" >&2
                exit 1
              fi
              printf '%s\n' '{"request_id":"request-1","actor_id":"deployment-smoke-test","session_id":"session-1","response":"ok","duration_ms":1,"trace_id":"0123456789abcdef0123456789abcdef"}'
            else
              echo "Unexpected uv call: $*" >&2
              exit 2
            fi
            """,
        )
        self._write_executable(
            "curl",
            r"""
            #!/usr/bin/env bash
            if [[ "${FAIL_STAGE:-}" == "readiness" ]]; then
              exit 1
            fi
            exit 0
            """,
        )
        self._write_executable("sleep", "#!/usr/bin/env bash\nexit 0\n")
        self._write_executable(
            "kubectl",
            r"""
            #!/usr/bin/env bash
            set -euo pipefail
            printf '%s\n' "$*" >> "$COMMAND_LOG"
            args=" $* "

            if [[ "${MISSING_PDB:-}" == "true" &&
              "$args" == *" get poddisruptionbudget/mortgage-assistant "* ]]; then
              exit 1
            fi

            if [[ "$args" == *" set image "* ]]; then
              count=0
              if [[ -f "$SET_IMAGE_COUNT" ]]; then
                count="$(cat "$SET_IMAGE_COUNT")"
              fi
              count=$((count + 1))
              printf '%s' "$count" > "$SET_IMAGE_COUNT"
              exit 0
            fi

            if [[ "$args" == *" rollout status "* ]]; then
              count=0
              if [[ -f "$SET_IMAGE_COUNT" ]]; then
                count="$(cat "$SET_IMAGE_COUNT")"
              fi
              if [[ "${FAIL_STAGE:-}" == "rollout" && "$count" == "1" ]]; then
                exit 1
              fi
              exit 0
            fi

            if [[ "$args" == *" get deployment mortgage-assistant "* && "$args" == *" jsonpath="* ]]; then
              case "$args" in
                *".image}"*) printf '%s' "${PREVIOUS_IMAGE_OVERRIDE:-example.test/repository:lab05-agent-previous}" ;;
                *".spec.replicas}"*) printf '2' ;;
                *"serviceAccountName"*) printf '%s' "${SERVICE_ACCOUNT_OVERRIDE:-mortgage-assistant}" ;;
                *"MORTGAGE_API_KEY"*) printf 'mortgage-assistant-api-key' ;;
                *"OTEL_EXPORTER_OTLP_HEADERS"*) printf 'langfuse-otel-auth' ;;
                *) echo "Unexpected deployment jsonpath: $*" >&2; exit 2 ;;
              esac
              exit 0
            fi

            if [[ "$args" == *" get poddisruptionbudget mortgage-assistant "* && "$args" == *" jsonpath="* ]]; then
              printf '1'
              exit 0
            fi

            if [[ "$args" == *" get service mortgage-assistant "* && "$args" == *" jsonpath="* ]]; then
              case "$args" in
                *"loadBalancerSourceRanges"*) printf '203.0.113.10/32' ;;
                *"status.loadBalancer"*) printf 'api.example.test' ;;
                *) echo "Unexpected service jsonpath: $*" >&2; exit 2 ;;
              esac
              exit 0
            fi

            case "$1" in
              get|describe|logs) exit 0 ;;
              *) echo "Unexpected kubectl call: $*" >&2; exit 2 ;;
            esac
            """,
        )

    def _run_deploy(
        self,
        fail_stage: str | None,
        overrides: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{self.bin_dir}:{environment['PATH']}",
                "COMMAND_LOG": str(self.command_log),
                "SET_IMAGE_COUNT": str(self.set_image_count),
            }
        )
        environment.update(overrides or {})
        if fail_stage is not None:
            environment["FAIL_STAGE"] = fail_stage
        return subprocess.run(
            [
                "bash",
                str(DEPLOY_SCRIPT),
                "--image-uri",
                "example.test/repository:lab05cc",
            ],
            cwd=MODULE_DIR,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    def _image_updates(self) -> list[str]:
        return [
            line
            for line in self.command_log.read_text().splitlines()
            if line.startswith("set image ")
        ]

    def test_each_post_mutation_failure_restores_captured_image(self) -> None:
        for fail_stage in ("rollout", "readiness", "smoke"):
            with self.subTest(fail_stage=fail_stage):
                self.command_log.unlink(missing_ok=True)
                self.set_image_count.unlink(missing_ok=True)
                result = self._run_deploy(fail_stage)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                updates = self._image_updates()
                self.assertEqual(len(updates), 2, result.stdout + result.stderr)
                self.assertIn(
                    "mortgage-assistant=example.test/repository:lab05cc",
                    updates[0],
                )
                self.assertIn(
                    "mortgage-assistant=example.test/repository:lab05-agent-previous",
                    updates[1],
                )

    def test_incompatible_prerequisites_fail_before_image_mutation(self) -> None:
        cases = (
            {"MISSING_PDB": "true"},
            {"SERVICE_ACCOUNT_OVERRIDE": "unexpected-service-account"},
            {"PREVIOUS_IMAGE_OVERRIDE": "example.test/repository:lab06-agent-later"},
            {"PREVIOUS_IMAGE_OVERRIDE": "foreign.test/repository:lab05-agent-lookalike"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                self.command_log.unlink(missing_ok=True)
                self.set_image_count.unlink(missing_ok=True)
                result = self._run_deploy(None, overrides)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self._image_updates(), [])

    def test_success_changes_only_the_image_without_rollback(self) -> None:
        result = self._run_deploy(None)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        updates = self._image_updates()
        self.assertEqual(len(updates), 1)
        self.assertIn(
            "mortgage-assistant=example.test/repository:lab05cc",
            updates[0],
        )
        log = self.command_log.read_text()
        self.assertNotIn("apply ", log)
        self.assertNotIn("create secret", log)


if __name__ == "__main__":
    unittest.main()
