"""Local Streamable HTTP fixture for the Lab 06 consumer contract tests."""

import logging
import os

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


logger = logging.getLogger(__name__)


class CreditScoreResult(BaseModel):
    """Represent a deterministic structured fixture result."""

    customer_id: str
    credit_score: int
    score_scale: str
    source: str


server = MCPServer(
    name="credit-score-mcp",
    version="test",
    instructions="Return synthetic fixture data only.",
)


@server.tool(name="get_credit_score", structured_output=True)
def get_credit_score(customer_id: str) -> CreditScoreResult:
    """Return a deterministic synthetic score for the supplied identifier."""
    return CreditScoreResult(
        customer_id=customer_id,
        credit_score=80,
        score_scale="0-100",
        source="synthetic-workshop",
    )


@server.custom_route("/health/ready", methods=["GET"], include_in_schema=False)
async def readiness(_: Request) -> Response:
    """Return fixture readiness."""
    return JSONResponse({"status": "ready"})


def main() -> None:
    """Run the loopback-only MCP fixture."""
    server.run(
        transport="streamable-http",
        host="127.0.0.1",
        port=int(os.environ["FIXTURE_PORT"]),
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
    )


if __name__ == "__main__":
    main()
