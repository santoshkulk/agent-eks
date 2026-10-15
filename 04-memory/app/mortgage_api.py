"""Authenticated FastAPI adapter for the Lab 4 mortgage supervisor."""

import hmac
import logging
import os
import threading
import time
import uuid

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from memory import MEMORY_EMBEDDING_MODEL_ID, MEMORY_TABLE_NAME
from mortgage_agent import (
    MODEL_ID,
    configure_logging,
    get_knowledge_base_id,
    run_supervisor,
)


configure_logging()
logger = logging.getLogger("mortgage_api")

app = FastAPI(
    title="Mortgage Assistant",
    description="HTTP API for the memory-enabled multi-agent mortgage assistant.",
    version="4.0.0",
)

API_KEY = os.environ.get("MORTGAGE_API_KEY", "").strip()
if not API_KEY:
    raise RuntimeError("MORTGAGE_API_KEY is required and must not be empty")

IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"

# Agent turns are slow and each holds a worker thread. Cap them below Uvicorn's
# connection limit so health probes are not starved.
AGENT_SLOTS = threading.BoundedSemaphore(int(os.environ.get("AGENT_CONCURRENCY", "4")))


class InvokeRequest(BaseModel):
    """Validated request body for one supervisor invocation."""

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=4000)
    actor_id: str = Field(min_length=1, max_length=128, pattern=IDENTIFIER)
    session_id: str = Field(min_length=1, max_length=128, pattern=IDENTIFIER)


class InvokeResponse(BaseModel):
    """Stable public response for a completed supervisor invocation."""

    request_id: str
    actor_id: str
    session_id: str
    response: str
    duration_ms: int


def authorize(authorization: str | None) -> None:
    """Require the configured bearer credential using constant-time comparison."""
    expected = f"Bearer {API_KEY}".encode("utf-8")
    if not authorization or not hmac.compare_digest(
        authorization.encode("utf-8"), expected
    ):
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")


@app.get("/health")
def health() -> dict[str, str]:
    """Return process liveness without requiring authentication."""
    return {"status": "ok"}


@app.get("/health/ready")
def readiness() -> dict[str, str]:
    """Validate required configuration and return readiness details."""
    return {
        "status": "ready",
        "knowledge_base_id": get_knowledge_base_id(),
        "model_id": MODEL_ID,
        "memory_table": MEMORY_TABLE_NAME,
        "memory_embedding_model_id": MEMORY_EMBEDDING_MODEL_ID,
    }


@app.post("/invoke", response_model=InvokeResponse)
def invoke(
    request: InvokeRequest,
    authorization: str | None = Header(default=None),
) -> InvokeResponse:
    """Authenticate, bound concurrency, and invoke a fresh supervisor instance."""
    authorize(authorization)
    request_id = str(uuid.uuid4())
    started = time.monotonic()
    if not AGENT_SLOTS.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail="All agent slots are busy; retry shortly",
            headers={"Retry-After": "5"},
        )
    try:
        result = run_supervisor(
            request.prompt,
            request.actor_id,
            request.session_id,
            request_id,
        )
    except Exception as error:
        logger.exception("Mortgage assistant request %s failed", request_id)
        raise HTTPException(
            status_code=500,
            detail=f"Mortgage assistant request failed; request_id={request_id}",
        ) from error
    finally:
        AGENT_SLOTS.release()

    return InvokeResponse(
        request_id=request_id,
        actor_id=request.actor_id,
        session_id=request.session_id,
        response=str(result),
        duration_ms=round((time.monotonic() - started) * 1000),
    )
