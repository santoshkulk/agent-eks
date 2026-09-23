import hmac
import logging
import os
import time
import uuid

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

import telemetry

telemetry.init_telemetry()

from credit_score_mcp import get_credit_score_mcp_url
from mortgage_agent import (
    MODEL_ID,
    configure_logging,
    get_knowledge_base_id,
    run_prompt,
)
from memory import MEMORY_EMBEDDING_MODEL_ID, MEMORY_TABLE_NAME


configure_logging()
logger = logging.getLogger("mortgage_api")

app = FastAPI(
    title="Mortgage Assistant",
    description="HTTP API for the durable-memory, traced, MCP mortgage assistant.",
    version="3.0.0",
)

API_KEY = os.environ.get("MORTGAGE_API_KEY", "").strip()
if not API_KEY:
    raise RuntimeError("MORTGAGE_API_KEY is required and must not be empty")


class InvokeRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    actor_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    session_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )


class InvokeResponse(BaseModel):
    request_id: str
    actor_id: str
    session_id: str
    response: str
    duration_ms: int
    trace_id: str | None = None


def authorize(authorization: str | None) -> None:
    expected = f"Bearer {API_KEY}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
def readiness() -> dict[str, str]:
    get_credit_score_mcp_url()
    return {
        "status": "ready",
        "knowledge_base_id": get_knowledge_base_id(),
        "model_id": MODEL_ID,
        "memory_table": MEMORY_TABLE_NAME,
        "memory_embedding_model_id": MEMORY_EMBEDDING_MODEL_ID,
        "credit_score_mcp": "configured",
    }


@app.on_event("shutdown")
def on_shutdown() -> None:
    """Flush pending spans without affecting request processing."""
    telemetry.shutdown_telemetry()


@app.post("/invoke", response_model=InvokeResponse)
def invoke(
    request: InvokeRequest,
    authorization: str | None = Header(default=None),
) -> InvokeResponse:
    authorize(authorization)
    request_id = str(uuid.uuid4())
    started = time.monotonic()

    attributes = telemetry.trace_attributes(
        request.actor_id,
        request.session_id,
        request_id,
    )
    trace_id: str | None = None
    with telemetry.start_request_span("mortgage_assistant.invoke", attributes):
        try:
            response = run_prompt(
                request.prompt,
                actor_id=request.actor_id,
                session_id=request.session_id,
                request_id=request_id,
            )
        except Exception as error:
            logger.exception("Mortgage assistant request %s failed", request_id)
            raise HTTPException(
                status_code=500,
                detail=f"Mortgage assistant request failed; request_id={request_id}",
            ) from error

        trace_id = telemetry.current_trace_id()

    return InvokeResponse(
        request_id=request_id,
        actor_id=request.actor_id,
        session_id=request.session_id,
        response=response,
        duration_ms=round((time.monotonic() - started) * 1000),
        trace_id=trace_id,
    )
