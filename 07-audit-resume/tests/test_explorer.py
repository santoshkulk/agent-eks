import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch


MODULE_DIR = Path(__file__).resolve().parents[1]
MODULE_PATH = MODULE_DIR / "scripts" / "explore_credit_score_mcp.py"
SPEC = importlib.util.spec_from_file_location("explore_credit_score_mcp", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Unable to load {MODULE_PATH}")
explorer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(explorer)


class ExplorerTests(unittest.TestCase):
    def test_explorer_target_is_fixed_and_has_no_url_option(self) -> None:
        self.assertEqual(explorer.PROVIDER_NAMESPACE, "credit-services")
        self.assertEqual(explorer.PROVIDER_SERVICE, "credit-score-mcp")
        self.assertEqual(explorer.PROVIDER_REMOTE_PORT, 8081)
        self.assertEqual(explorer.PROVIDER_MCP_PATH, "/mcp")
        self.assertNotIn("--url", explorer.build_parser().format_help())

    @patch.object(explorer, "wait_for_port_forward")
    @patch.object(explorer.subprocess, "Popen")
    def test_port_forward_uses_fixed_target_and_terminates(
        self,
        popen,
        wait_for_port_forward,
    ) -> None:
        process = Mock()
        process.poll.return_value = None
        popen.return_value = process

        with explorer.port_forward(43123) as endpoint:
            self.assertEqual(endpoint, "http://127.0.0.1:43123/mcp")

        popen.assert_called_once_with(
            [
                "kubectl",
                "port-forward",
                "--namespace",
                "credit-services",
                "service/credit-score-mcp",
                "43123:8081",
                "--address",
                "127.0.0.1",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        wait_for_port_forward.assert_called_once_with(process, 43123)
        process.terminate.assert_called_once_with()
        process.kill.assert_not_called()

    @patch.object(explorer, "wait_for_port_forward")
    @patch.object(explorer.subprocess, "Popen")
    def test_port_forward_kills_process_after_terminate_timeout(
        self,
        popen,
        _,
    ) -> None:
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("kubectl", 5), 0]
        popen.return_value = process

        with explorer.port_forward(43124):
            pass

        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_count, 2)

    def test_exact_tool_contract_is_required(self) -> None:
        expected = Mock(name="expected")
        expected.name = "get_credit_score"
        self.assertIs(explorer.validate_tools([expected]), expected)

        unexpected = Mock(name="unexpected")
        unexpected.name = "other"
        with self.assertRaisesRegex(explorer.ExplorerError, "Expected exactly one"):
            explorer.validate_tools([expected, unexpected])


if __name__ == "__main__":
    unittest.main()
