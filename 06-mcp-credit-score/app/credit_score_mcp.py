from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
import logging
import os
from urllib.parse import urlparse

from mcp.client.streamable_http import streamable_http_client
from strands.tools.mcp import MCPClient
from strands.tools.mcp.mcp_agent_tool import MCPAgentTool


logger = logging.getLogger(__name__)

EXPECTED_TOOL_NAME = "get_credit_score"
EXPECTED_PROVIDER_URL = (
    "http://credit-score-mcp.credit-services.svc.cluster.local:8081/mcp"
)
MCP_STARTUP_TIMEOUT_SECONDS = 30


class CreditScoreMCPError(RuntimeError):
    """Represent a safe, user-readable credit-score MCP configuration error."""


def get_credit_score_mcp_url() -> str:
    """Return and validate the required credit-score MCP endpoint URL."""
    endpoint = os.environ.get("CREDIT_SCORE_MCP_URL", "").strip()
    if not endpoint:
        raise CreditScoreMCPError("CREDIT_SCORE_MCP_URL is required")

    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CreditScoreMCPError(
            "CREDIT_SCORE_MCP_URL must be an absolute HTTP or HTTPS URL"
        )
    if parsed.username or parsed.password:
        raise CreditScoreMCPError(
            "CREDIT_SCORE_MCP_URL must not contain embedded credentials"
        )
    if endpoint != EXPECTED_PROVIDER_URL:
        raise CreditScoreMCPError(
            "CREDIT_SCORE_MCP_URL must identify the fixed workshop provider "
            f"at {EXPECTED_PROVIDER_URL}"
        )
    return endpoint


def _transport_factory(endpoint: str):
    """Create the verified Streamable HTTP transport context manager."""
    return streamable_http_client(endpoint)


def _discover_all_tools(client: MCPClient) -> list[MCPAgentTool]:
    """Read every provider tool page before enforcing the exact contract."""
    page = client.list_tools_sync()
    tools = list(page)
    pagination_token = getattr(page, "pagination_token", None)
    while pagination_token:
        page = client.list_tools_sync(pagination_token=pagination_token)
        tools.extend(page)
        pagination_token = getattr(page, "pagination_token", None)
    return tools


def _select_credit_score_tool(tools: list[MCPAgentTool]) -> MCPAgentTool:
    """Require the provider to expose exactly the expected remote tool."""
    tool_names = [tool.tool_name for tool in tools]
    if tool_names != [EXPECTED_TOOL_NAME]:
        found = ", ".join(tool_names) if tool_names else "none"
        raise CreditScoreMCPError(
            "Credit-score MCP provider must expose exactly one tool named "
            f"{EXPECTED_TOOL_NAME}; found: {found}"
        )
    return tools[0]


@contextmanager
def credit_score_tool() -> Iterator[MCPAgentTool]:
    """Keep the MCP connection open while its discovered tool is in use.

    The pinned MCP transport supplies bounded defaults of 30 seconds for
    connect, write, and pool operations and 300 seconds for stream reads.
    Strands additionally bounds connection startup to 30 seconds.
    """
    endpoint = get_credit_score_mcp_url()
    logger.info("Connecting to the configured credit-score MCP provider")
    client = MCPClient(
        lambda: _transport_factory(endpoint),
        startup_timeout=MCP_STARTUP_TIMEOUT_SECONDS,
    )
    stack = ExitStack()
    try:
        stack.enter_context(client)
        tool = _select_credit_score_tool(_discover_all_tools(client))
    except CreditScoreMCPError:
        stack.close()
        raise
    except Exception as error:
        stack.close()
        logger.error("Credit-score MCP initialization failed", exc_info=True)
        raise CreditScoreMCPError(
            "The credit-score MCP provider is unavailable or has an invalid contract"
        ) from error

    try:
        yield tool
    finally:
        stack.close()
