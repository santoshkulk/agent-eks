from __future__ import annotations

import argparse
import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
import json
import logging
import socket
import subprocess
import sys
import time
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import PaginatedRequestParams


logger = logging.getLogger(__name__)

PROVIDER_NAMESPACE = "credit-services"
PROVIDER_SERVICE = "credit-score-mcp"
PROVIDER_REMOTE_PORT = 8081
PROVIDER_MCP_PATH = "/mcp"
EXPECTED_TOOL_NAME = "get_credit_score"
READ_TIMEOUT_SECONDS = 30.0
PORT_FORWARD_STARTUP_SECONDS = 15.0
PORT_FORWARD_SHUTDOWN_SECONDS = 5.0


class ExplorerError(RuntimeError):
    """Represent a user-readable MCP explorer failure."""


def find_free_loopback_port() -> int:
    """Reserve and return an available loopback TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def wait_for_port_forward(
    process: subprocess.Popen[str],
    local_port: int,
    timeout_seconds: float = PORT_FORWARD_STARTUP_SECONDS,
) -> None:
    """Wait until the kubectl port-forward accepts loopback connections."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            detail = process.stderr.read().strip() if process.stderr else ""
            suffix = f": {detail}" if detail else ""
            raise ExplorerError(
                f"kubectl port-forward exited with code {return_code}{suffix}"
            )
        try:
            with socket.create_connection(
                ("127.0.0.1", local_port),
                timeout=0.2,
            ):
                return
        except OSError:
            time.sleep(0.1)
    raise ExplorerError("Timed out waiting for the credit-score MCP port-forward")


@contextmanager
def port_forward(local_port: int) -> Iterator[str]:
    """Forward the fixed credit-score MCP Service to a loopback endpoint."""
    command = [
        "kubectl",
        "port-forward",
        "--namespace",
        PROVIDER_NAMESPACE,
        f"service/{PROVIDER_SERVICE}",
        f"{local_port}:{PROVIDER_REMOTE_PORT}",
        "--address",
        "127.0.0.1",
    ]
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as error:
        raise ExplorerError("kubectl is not installed or is not on PATH") from error

    try:
        wait_for_port_forward(process, local_port)
        yield f"http://127.0.0.1:{local_port}{PROVIDER_MCP_PATH}"
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=PORT_FORWARD_SHUTDOWN_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=PORT_FORWARD_SHUTDOWN_SECONDS)


def json_safe(value: Any) -> Any:
    """Convert MCP and Pydantic values into JSON-safe structures."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def validate_tools(tools: list[Any]) -> Any:
    """Require exactly one MCP server tool with the workshop contract name."""
    names = [tool.name for tool in tools]
    if names != [EXPECTED_TOOL_NAME]:
        found = ", ".join(names) if names else "none"
        raise ExplorerError(
            "Expected exactly one MCP tool named "
            f"{EXPECTED_TOOL_NAME}; found: {found}"
        )
    return tools[0]


async def list_all_tools(session: ClientSession) -> list[Any]:
    """Read every MCP tool page before validating the MCP server contract."""
    result = await session.list_tools()
    tools = list(result.tools)
    cursor = result.next_cursor
    while cursor:
        result = await session.list_tools(
            params=PaginatedRequestParams(cursor=cursor)
        )
        tools.extend(result.tools)
        cursor = result.next_cursor
    return tools


async def execute_operation(
    endpoint: str,
    command: str,
    customer_id: str | None,
) -> dict[str, Any]:
    """Initialize a real MCP session and execute one explorer operation."""
    async with streamable_http_client(endpoint) as streams:
        read_stream, write_stream = streams
        async with ClientSession(
            read_stream,
            write_stream,
            read_timeout_seconds=READ_TIMEOUT_SECONDS,
        ) as session:
            initialization = await session.initialize()
            tools = await list_all_tools(session)
            tool = validate_tools(tools)

            if command == "info":
                return {
                    "endpoint": "fixed kubectl port-forward",
                    "provider": {
                        "namespace": PROVIDER_NAMESPACE,
                        "service": PROVIDER_SERVICE,
                        "port": PROVIDER_REMOTE_PORT,
                    },
                    "initialization": json_safe(initialization),
                }
            if command == "list-tools":
                return {"tools": json_safe(tools)}
            if command == "inspect-tool":
                return {"tool": json_safe(tool)}
            if command == "verify":
                verification_id = "lab06-explorer-verify"
                result = await session.call_tool(
                    EXPECTED_TOOL_NAME,
                    arguments={"customer_id": verification_id},
                    read_timeout_seconds=READ_TIMEOUT_SECONDS,
                )
                if result.is_error:
                    raise ExplorerError(
                        "The credit-score MCP verification call returned an error"
                    )
                structured = result.structured_content
                if not isinstance(structured, dict):
                    raise ExplorerError(
                        "The credit-score MCP verification call returned no "
                        "structured result"
                    )
                if (
                    structured.get("customer_id") != verification_id
                    or structured.get("credit_score") != 80
                    or structured.get("source") != "synthetic-workshop"
                ):
                    raise ExplorerError(
                        "The credit-score MCP verification result did not match "
                        "the workshop contract"
                    )
                return {
                    "status": "ok",
                    "protocol_initialized": True,
                    "tool": tool.name,
                    "result": json_safe(structured),
                }
            if command == "call-credit-score":
                if customer_id is None or not customer_id.strip():
                    raise ExplorerError("--customer-id must not be empty")
                result = await session.call_tool(
                    EXPECTED_TOOL_NAME,
                    arguments={"customer_id": customer_id.strip()},
                    read_timeout_seconds=READ_TIMEOUT_SECONDS,
                )
                if result.is_error:
                    raise ExplorerError(
                        "The credit-score MCP tool returned an error: "
                        f"{json.dumps(json_safe(result), sort_keys=True)}"
                    )
                return {
                    "tool": EXPECTED_TOOL_NAME,
                    "customer_id": customer_id.strip(),
                    "result": json_safe(result),
                }
            raise ExplorerError(f"Unsupported explorer command: {command}")


def build_parser() -> argparse.ArgumentParser:
    """Build the fixed-target participant command-line parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Explore the fixed credit-score MCP server in credit-services "
            "through a temporary loopback port-forward."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("info", help="Show MCP server initialization details.")
    subparsers.add_parser("list-tools", help="List the MCP server's tools.")
    subparsers.add_parser(
        "inspect-tool",
        help="Show the get_credit_score tool schema.",
    )
    call_parser = subparsers.add_parser(
        "call-credit-score",
        help="Call get_credit_score with a synthetic customer ID.",
    )
    call_parser.add_argument("--customer-id", required=True)
    subparsers.add_parser(
        "verify",
        help="Verify MCP initialization and the exact tool contract.",
    )
    return parser


def main() -> int:
    """Run the explorer and return a user-friendly process status."""
    args = build_parser().parse_args()
    try:
        local_port = find_free_loopback_port()
        with port_forward(local_port) as endpoint:
            result = asyncio.run(
                execute_operation(
                    endpoint=endpoint,
                    command=args.command,
                    customer_id=getattr(args, "customer_id", None),
                )
            )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (ExplorerError, OSError, TimeoutError) as error:
        logger.debug("Credit-score MCP exploration failed", exc_info=True)
        print(f"Credit-score MCP explorer failed: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        logger.error("Unexpected credit-score MCP explorer failure", exc_info=True)
        print(
            f"Credit-score MCP explorer failed unexpectedly: {error}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
