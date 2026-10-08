import hmac
import logging
import os
import time
import uuid

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, field_validator

from mortgage_agent import (
    MODEL_ID,
    configure_logging,
    get_knowledge_base_id,
    run_prompt,
)


configure_logging()
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Mortgage Assistant",
    description="HTTP API for the Strands mortgage assistant.",
    version="1.0.0",
)

API_KEY = os.environ.get("MORTGAGE_API_KEY", "").strip()
if not API_KEY:
    raise RuntimeError("MORTGAGE_API_KEY is required and must not be empty")


class InvokeRequest(BaseModel):
    """Validated request for one stateless agent invocation."""

    prompt: str = Field(min_length=1, max_length=4000)

    @field_validator("prompt")
    @classmethod
    def validate_non_whitespace_prompt(cls, value: str) -> str:
        """Reject prompts that contain no non-whitespace characters."""
        if not value.strip():
            raise ValueError("Prompt must contain at least one non-whitespace character")
        return value


class InvokeResponse(BaseModel):
    """Response from one stateless agent invocation."""

    request_id: str
    response: str
    duration_ms: int


def authorize(authorization: str | None) -> None:
    """Require the configured bearer credential."""
    expected = f"Bearer {API_KEY}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")


@app.get("/health")
def health() -> dict[str, str]:
    """Report that the HTTP process is live."""
    return {"status": "ok"}


@app.get("/health/ready")
def readiness() -> dict[str, str]:
    """Report that required Knowledge Base configuration resolves."""
    return {
        "status": "ready",
        "knowledge_base_id": get_knowledge_base_id(),
        "model_id": MODEL_ID,
    }


@app.post("/invoke", response_model=InvokeResponse)
def invoke(
    request: InvokeRequest,
    authorization: str | None = Header(default=None),
) -> InvokeResponse:
    """Authorize and run one request-correlated, stateless prompt."""
    authorize(authorization)
    request_id = str(uuid.uuid4())
    started = time.monotonic()

    try:
        response = run_prompt(request.prompt, request_id=request_id)
    except Exception as error:
        logger.error(
            "Mortgage assistant request failed request_id=%s error_type=%s",
            request_id,
            type(error).__name__,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Mortgage assistant request failed; request_id={request_id}",
        ) from error

    return InvokeResponse(
        request_id=request_id,
        response=response,
        duration_ms=round((time.monotonic() - started) * 1000),
    )
