import asyncio
import importlib.util
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request


MODULE_DIR = Path(__file__).resolve().parents[1]
EXPLORER_PATH = MODULE_DIR / "scripts" / "explore_credit_score_mcp.py"
FIXTURE_PATH = MODULE_DIR / "tests" / "fixtures" / "credit_score_mcp_server.py"
SPEC = importlib.util.spec_from_file_location("contract_explorer", EXPLORER_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Unable to load {EXPLORER_PATH}")
explorer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(explorer)


def available_port() -> int:
    """Reserve and return an available loopback TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class MCPContractTests(unittest.TestCase):
    """Exercise the participant client against a local Streamable HTTP server."""

    def test_real_streamable_http_discovery_and_call(self) -> None:
        port = available_port()
        environment = os.environ.copy()
        environment["FIXTURE_PORT"] = str(port)
        process = subprocess.Popen(
            [sys.executable, str(FIXTURE_PATH)],
            cwd=MODULE_DIR,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

        try:
            readiness_url = f"http://127.0.0.1:{port}/health/ready"
            deadline = time.monotonic() + 15
            while True:
                if process.poll() is not None:
                    detail = process.stderr.read() if process.stderr else ""
                    self.fail(f"MCP fixture exited before readiness: {detail}")
                try:
                    with urllib.request.urlopen(readiness_url, timeout=1) as response:
                        if response.status == 200:
                            break
                except (urllib.error.URLError, TimeoutError):
                    pass
                if time.monotonic() >= deadline:
                    self.fail("MCP fixture did not become ready within 15 seconds")
                time.sleep(0.1)

            endpoint = f"http://127.0.0.1:{port}/mcp"
            verification = asyncio.run(
                explorer.execute_operation(endpoint, "verify", None)
            )
            result = asyncio.run(
                explorer.execute_operation(
                    endpoint,
                    "call-credit-score",
                    "workshop-contract-test",
                )
            )

            self.assertEqual(verification["tool"], "get_credit_score")
            structured = result["result"]["structured_content"]
            self.assertEqual(structured["credit_score"], 80)
            self.assertEqual(structured["customer_id"], "workshop-contract-test")
            self.assertEqual(structured["source"], "synthetic-workshop")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if process.stderr is not None:
                process.stderr.close()


if __name__ == "__main__":
    unittest.main()
