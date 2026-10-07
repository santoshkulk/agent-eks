import hmac
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from functools import lru_cache
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Path, Query, Response
from pydantic import BaseModel, Field

import telemetry

telemetry.init_telemetry()

from credit_score_mcp import get_credit_score_mcp_url
from execution import INTERRUPTED, ExecutionConflict, ExecutionNotFound
from memory import MEMORY_EMBEDDING_MODEL_ID, MEMORY_TABLE_NAME, get_item_store
from mortgage_agent import (
    MODEL_ID,
    SPECIALIST_TOOL_NAMES,
    configure_logging,
    get_knowledge_base_id,
    reset_pending_interrupts,
    run_supervisor,
)
from service import Orchestrator, Outcome


configure_logging()
logger = logging.getLogger("mortgage_api")

app = FastAPI(
    title="Mortgage Assistant",
    description=(
        "HTTP API for the audited, explainable, resumable multi-agent mortgage "
        "assistant (agents as tools)."
    ),
    version="4.0.0",
)

API_KEY = os.environ.get("MORTGAGE_API_KEY", "").strip()
if not API_KEY:
    raise RuntimeError("MORTGAGE_API_KEY is required and must not be empty")

IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"

# Agent turns are slow and each holds a worker thread. Cap them below uvicorn's
# --limit-concurrency so health probes and read-only calls are never starved.
AGENT_SLOTS = threading.BoundedSemaphore(int(os.environ.get("AGENT_CONCURRENCY", "4")))


class InvokeRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    actor_id: str = Field(min_length=1, max_length=128, pattern=IDENTIFIER)
    session_id: str = Field(min_length=1, max_length=128, pattern=IDENTIFIER)
    request_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=IDENTIFIER,
        description="Idempotency key. Re-send the same value to resume or replay a request.",
    )


class ExecutionRef(BaseModel):
    actor_id: str = Field(min_length=1, max_length=128, pattern=IDENTIFIER)
    session_id: str = Field(min_length=1, max_length=128, pattern=IDENTIFIER)


class Decision(BaseModel):
    interrupt_id: str = Field(min_length=1, max_length=256)
    approved: bool
    comment: str = Field(default="", max_length=500)
    reviewer: str = Field(default="unknown", max_length=128)


class ApprovalRequest(ExecutionRef):
    decisions: list[Decision] = Field(min_length=1, max_length=10)


class InvokeResponse(BaseModel):
    request_id: str
    actor_id: str
    session_id: str
    status: str
    response: str | None = None
    interrupts: list[dict[str, Any]] = Field(default_factory=list)
    attempt: int
    duration_ms: int
    trace_id: str | None = None
    explanation: dict[str, Any] | None = None


def authorize(authorization: str | None) -> None:
    expected = f"Bearer {API_KEY}".encode("utf-8")
    # Compare bytes: compare_digest raises TypeError on non-ASCII str.
    if not authorization or not hmac.compare_digest(
        authorization.encode("utf-8"), expected
    ):
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")


@lru_cache(maxsize=1)
def get_orchestrator() -> Orchestrator:
    return Orchestrator(
        get_item_store(),
        run_supervisor,
        SPECIALIST_TOOL_NAMES,
        reset_agents=reset_pending_interrupts,
    )


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


def _conflict(error: Exception) -> HTTPException:
    return HTTPException(status_code=getattr(error, "status_code", 409), detail=str(error))


def _respond(
    outcome: Outcome,
    started: float,
    trace_id: str | None,
    response: Response,
) -> InvokeResponse:
    execution = outcome.execution
    if outcome.status == INTERRUPTED:
        response.status_code = 202
    explanation = None
    try:
        explanation = get_orchestrator().explanation(
            execution.actor_id, execution.session_id, execution.request_id
        )
    except Exception:
        logger.exception("Could not build explanation for %s", execution.request_id)
    return InvokeResponse(
        request_id=execution.request_id,
        actor_id=execution.actor_id,
        session_id=execution.session_id,
        status="awaiting_approval" if outcome.status == INTERRUPTED else execution.status.lower(),
        response=outcome.response,
        interrupts=outcome.interrupts,
        attempt=execution.attempt,
        duration_ms=round((time.monotonic() - started) * 1000),
        trace_id=trace_id or execution.trace_id,
        explanation=explanation,
    )


def _execute(
    action: Callable[[str | None], Outcome],
    actor_id: str,
    session_id: str,
    request_id: str,
    response: Response,
) -> InvokeResponse:
    started = time.monotonic()
    if not AGENT_SLOTS.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail="All agent slots are busy; retry shortly with the same request_id",
            headers={"Retry-After": "5"},
        )
    try:
        return _execute_with_slot(action, actor_id, session_id, request_id, response, started)
    finally:
        AGENT_SLOTS.release()


def _execute_with_slot(
    action: Callable[[str | None], Outcome],
    actor_id: str,
    session_id: str,
    request_id: str,
    response: Response,
    started: float,
) -> InvokeResponse:
    attributes = telemetry.trace_attributes(actor_id, session_id, request_id)
    with telemetry.start_request_span("mortgage_assistant.invoke", attributes):
        trace_id = telemetry.current_trace_id()
        try:
            outcome = action(trace_id)
        except (ExecutionConflict, ExecutionNotFound) as error:
            raise _conflict(error) from error
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as error:
            logger.exception("Mortgage assistant request %s failed", request_id)
            raise HTTPException(
                status_code=500,
                detail=(
                    "Mortgage assistant request failed; "
                    f"request_id={request_id}; retry with the same request_id to resume"
                ),
            ) from error
    return _respond(outcome, started, trace_id, response)


@app.post("/invoke", response_model=InvokeResponse)
def invoke(
    request: InvokeRequest,
    response: Response,
    authorization: str | None = Header(default=None),
) -> InvokeResponse:
    authorize(authorization)
    request_id = request.request_id or str(uuid.uuid4())
    return _execute(
        lambda trace_id: get_orchestrator().invoke(
            request.actor_id, request.session_id, request_id, request.prompt, trace_id
        ),
        request.actor_id,
        request.session_id,
        request_id,
        response,
    )


@app.post("/executions/{request_id}/resume", response_model=InvokeResponse)
def resume(
    request: ExecutionRef,
    response: Response,
    request_id: str = Path(max_length=128, pattern=IDENTIFIER),
    authorization: str | None = Header(default=None),
) -> InvokeResponse:
    authorize(authorization)
    existing = get_orchestrator().executions.get(
        request.actor_id, request.session_id, request_id
    )
    if existing is None:
        raise HTTPException(status_code=404, detail=f"no execution {request_id}")
    return _execute(
        lambda trace_id: get_orchestrator().invoke(
            request.actor_id, request.session_id, request_id, existing.prompt, trace_id
        ),
        request.actor_id,
        request.session_id,
        request_id,
        response,
    )


@app.post("/executions/{request_id}/approvals", response_model=InvokeResponse)
def approvals(
    request: ApprovalRequest,
    response: Response,
    request_id: str = Path(max_length=128, pattern=IDENTIFIER),
    authorization: str | None = Header(default=None),
) -> InvokeResponse:
    authorize(authorization)
    return _execute(
        lambda trace_id: get_orchestrator().decide(
            request.actor_id,
            request.session_id,
            request_id,
            [decision.model_dump() for decision in request.decisions],
            trace_id,
        ),
        request.actor_id,
        request.session_id,
        request_id,
        response,
    )


@app.post("/executions/{request_id}/cancel")
def cancel_execution(
    request: ExecutionRef,
    request_id: str = Path(max_length=128, pattern=IDENTIFIER),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Abandon a stuck or unwanted paused request so the session accepts new prompts."""
    authorize(authorization)
    try:
        execution = get_orchestrator().cancel(
            request.actor_id, request.session_id, request_id
        )
    except (ExecutionConflict, ExecutionNotFound) as error:
        raise _conflict(error) from error
    return execution.summary()


@app.get("/executions/{request_id}")
def execution_trail(
    request_id: str = Path(max_length=128, pattern=IDENTIFIER),
    actor_id: str = Query(min_length=1, max_length=128, pattern=IDENTIFIER),
    session_id: str = Query(min_length=1, max_length=128, pattern=IDENTIFIER),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    authorize(authorization)
    try:
        return get_orchestrator().trail(actor_id, session_id, request_id)
    except ExecutionNotFound as error:
        raise _conflict(error) from error


@app.get("/sessions/{session_id}/executions")
def session_executions(
    session_id: str = Path(max_length=128, pattern=IDENTIFIER),
    actor_id: str = Query(min_length=1, max_length=128, pattern=IDENTIFIER),
    authorization: str | None = Header(default=None),
) -> list[dict[str, Any]]:
    authorize(authorization)
    return get_orchestrator().session_executions(actor_id, session_id)
