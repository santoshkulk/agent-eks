"""Exercise standalone and overlay deployment behavior with fake commands."""

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
        self.namespace_state = self.root / "namespace-created"
        self.namespace_json = self.root / "namespace.json"
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
                *"/langfuse/otlp-endpoint"*) printf 'http://langfuse-web.langfuse.svc:3000/api/public/otel/v1/traces' ;;
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
            if [[ "$*" == *"checkip.amazonaws.com"* ]]; then
              printf '203.0.113.10\n'
              exit 0
            fi
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

            if [[ "$args" == *" get namespace mortgage-assistant "* ]]; then
              if [[ "$args" == *" --ignore-not-found "* ]]; then
                if [[ "${NAMESPACE_PROBE_ERROR:-}" == "true" ]]; then
                  exit 1
                fi
                if [[ "${STACK_STATE:-overlay}" != "fresh" || -f "$NAMESPACE_STATE" ]]; then
                  printf 'namespace/mortgage-assistant\n'
                fi
                exit 0
              fi
              if [[ "$args" == *" --output json "* ]]; then
                if [[ ! -f "$NAMESPACE_STATE" ]]; then
                  exit 1
                fi
                if [[ -n "${OWNER_TOKEN_OVERRIDE:-}" ]]; then
                  printf '{"metadata":{"labels":{"workshop.aws/owner-token":"%s"}}}\n' "$OWNER_TOKEN_OVERRIDE"
                else
                  cat "$NAMESPACE_JSON"
                fi
                exit 0
              fi
              [[ -f "$NAMESPACE_STATE" || "${STACK_STATE:-overlay}" != "fresh" ]]
              exit
            fi

            if [[ "$args" == *" get deployment langfuse-web "* ]]; then
              if [[ "${MISSING_LANGFUSE_KEYS:-}" == "true" ]]; then
                printf '%s\n' '{"spec":{"template":{"spec":{"containers":[{"env":[]}]}}}}'
              else
                printf '%s\n' '{"spec":{"template":{"spec":{"containers":[{"env":[{"name":"LANGFUSE_INIT_PROJECT_PUBLIC_KEY","value":"pk-test"},{"name":"LANGFUSE_INIT_PROJECT_SECRET_KEY","value":"sk-test"}]}]}}}}'
              fi
              exit 0
            fi

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
              if [[ "$args" == *" kube-system "* ]]; then
                exit 0
              fi
              count=0
              if [[ -f "$SET_IMAGE_COUNT" ]]; then
                count="$(cat "$SET_IMAGE_COUNT")"
              fi
              if [[ "${FAIL_STAGE:-}" == "rollout" ]]; then
                if [[ "${STACK_STATE:-overlay}" == "fresh" || "$count" == "1" ]]; then
                  exit 1
                fi
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

            if [[ "$args" == *" create -f - "* ]]; then
              if [[ "${CREATE_CONFLICT:-}" == "true" ]]; then
                cat >/dev/null
                exit 1
              fi
              cat > "$NAMESPACE_JSON"
              touch "$NAMESPACE_STATE"
              exit 0
            fi
            if [[ "$args" == *" apply -f "*"/k8s/base.yaml "* ]]; then
              exit 0
            fi
            if [[ "$args" == *" apply -f "*"/service.yaml "* ]]; then
              if [[ "${FAIL_STAGE:-}" == "apply" ]]; then
                exit 1
              fi
              exit 0
            fi
            if [[ "$args" == *" create secret generic "* ]]; then
              cat >/dev/null
              printf '%s\n' 'apiVersion: v1' 'kind: Secret'
              exit 0
            fi
            if [[ "$args" == *" apply -f - "* ]]; then
              cat >/dev/null
              exit 0
            fi
            if [[ "$args" == *" delete namespace mortgage-assistant "* ]]; then
              rm -f "$NAMESPACE_STATE" "$NAMESPACE_JSON"
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
        *,
        stack_state: str = "overlay",
        overrides: dict[str, str] | None = None,
        cidr: str = "203.0.113.10/32",
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{self.bin_dir}:{environment['PATH']}",
                "COMMAND_LOG": str(self.command_log),
                "SET_IMAGE_COUNT": str(self.set_image_count),
                "NAMESPACE_STATE": str(self.namespace_state),
                "NAMESPACE_JSON": str(self.namespace_json),
                "STACK_STATE": stack_state,
            }
        )
        environment.update(overrides or {})
        if fail_stage is not None:
            environment["FAIL_STAGE"] = fail_stage
        command = [
            "bash",
            str(DEPLOY_SCRIPT),
            "--image-uri",
            "example.test/repository:lab05cc",
        ]
        if stack_state == "fresh":
            command.extend(["--service-access-cidr", cidr])
        return subprocess.run(
            command,
            cwd=MODULE_DIR,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    def _log_lines(self) -> list[str]:
        if not self.command_log.exists():
            return []
        return self.command_log.read_text().splitlines()

    def _image_updates(self) -> list[str]:
        return [line for line in self._log_lines() if line.startswith("set image ")]

    def _namespace_deletes(self) -> list[str]:
        return [
            line
            for line in self._log_lines()
            if line.startswith("delete namespace mortgage-assistant ")
        ]

    def _reset_state(self) -> None:
        self.command_log.unlink(missing_ok=True)
        self.set_image_count.unlink(missing_ok=True)
        self.namespace_state.unlink(missing_ok=True)
        self.namespace_json.unlink(missing_ok=True)

    def test_overlay_failures_restore_captured_image(self) -> None:
        for fail_stage in ("rollout", "readiness", "smoke"):
            with self.subTest(fail_stage=fail_stage):
                self._reset_state()
                result = self._run_deploy(fail_stage)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                updates = self._image_updates()
                self.assertEqual(len(updates), 2, result.stdout + result.stderr)
                self.assertIn("mortgage-assistant=example.test/repository:lab05cc", updates[0])
                self.assertIn(
                    "mortgage-assistant=example.test/repository:lab05-agent-previous",
                    updates[1],
                )

    def test_overlay_prerequisites_fail_before_image_mutation(self) -> None:
        cases = (
            {"MISSING_PDB": "true"},
            {"SERVICE_ACCOUNT_OVERRIDE": "unexpected-service-account"},
            {"PREVIOUS_IMAGE_OVERRIDE": "example.test/repository:lab06-agent-later"},
            {"PREVIOUS_IMAGE_OVERRIDE": "foreign.test/repository:lab05-agent-lookalike"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                self._reset_state()
                result = self._run_deploy(None, overrides=overrides)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self._image_updates(), [])

    def test_overlay_success_changes_only_the_image(self) -> None:
        result = self._run_deploy(None)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self._image_updates()), 1)
        log = "\n".join(self._log_lines())
        self.assertNotIn("apply -f", log)
        self.assertNotIn("create secret", log)

    def test_fresh_success_creates_complete_stack_without_image_patch(self) -> None:
        result = self._run_deploy(None, stack_state="fresh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        log = "\n".join(self._log_lines())
        self.assertIn("apply -f", log)
        self.assertIn("k8s/base.yaml", log)
        self.assertIn("service.yaml", log)
        self.assertIn("create secret generic mortgage-assistant-api-key", log)
        self.assertIn("create secret generic langfuse-otel-auth", log)
        self.assertEqual(self._image_updates(), [])
        self.assertEqual(self._namespace_deletes(), [])

    def test_fresh_post_creation_failures_remove_only_created_namespace(self) -> None:
        for fail_stage in ("apply", "rollout", "readiness", "smoke"):
            with self.subTest(fail_stage=fail_stage):
                self._reset_state()
                result = self._run_deploy(fail_stage, stack_state="fresh")
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(len(self._namespace_deletes()), 1)
                self.assertEqual(self._image_updates(), [])

    def test_fresh_preflight_failures_do_not_create_or_delete_namespace(self) -> None:
        cases = (
            ({"MISSING_LANGFUSE_KEYS": "true"}, "203.0.113.10/32"),
            ({}, "0.0.0.0/0"),
            ({}, "not-a-cidr"),
        )
        for overrides, cidr in cases:
            with self.subTest(overrides=overrides, cidr=cidr):
                self._reset_state()
                result = self._run_deploy(
                    None,
                    stack_state="fresh",
                    overrides=overrides,
                    cidr=cidr,
                )
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self._namespace_deletes(), [])
                self.assertFalse(self.namespace_state.exists())

    def test_namespace_probe_error_fails_before_any_mutation(self) -> None:
        result = self._run_deploy(
            None,
            stack_state="fresh",
            overrides={"NAMESPACE_PROBE_ERROR": "true"},
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        log = "\n".join(self._log_lines())
        self.assertNotIn("apply -f", log)
        self.assertNotIn("create -f -", log)
        self.assertNotIn("create secret", log)
        self.assertEqual(self._namespace_deletes(), [])

    def test_namespace_create_conflict_never_deletes_competing_namespace(self) -> None:
        result = self._run_deploy(
            None,
            stack_state="fresh",
            overrides={"CREATE_CONFLICT": "true"},
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._namespace_deletes(), [])
        self.assertFalse(self.namespace_state.exists())

    def test_owner_token_mismatch_blocks_failure_cleanup(self) -> None:
        result = self._run_deploy(
            "apply",
            stack_state="fresh",
            overrides={"OWNER_TOKEN_OVERRIDE": "another-invocation"},
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._namespace_deletes(), [])
        self.assertTrue(self.namespace_state.exists())
        self.assertIn("refusing automatic deletion", result.stderr)


if __name__ == "__main__":
    unittest.main()
