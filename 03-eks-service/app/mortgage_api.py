import hmac
import logging
import os
import time
import uuid

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from mortgage_agent import (
    MODEL_ID,
    configure_logging,
    get_knowledge_base_id,
    run_prompt,
)


configure_logging()
logger = logging.getLogger("mortgage_api")

app = FastAPI(
    title="Mortgage Assistant",
    description="HTTP API for the Strands mortgage assistant.",
    version="1.0.0",
)

API_KEY = os.environ.get("MORTGAGE_API_KEY", "")


class InvokeRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)


class InvokeResponse(BaseModel):
    request_id: str
    response: str
    duration_ms: int


def authorize(authorization: str | None) -> None:
    if not API_KEY:
        return

    expected = f"Bearer {API_KEY}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
def readiness() -> dict[str, str]:
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
    authorize(authorization)
    request_id = str(uuid.uuid4())
    started = time.monotonic()

    try:
        response = run_prompt(request.prompt)
    except Exception as error:
        logger.exception("Mortgage assistant request %s failed", request_id)
        raise HTTPException(
            status_code=500,
            detail=f"Mortgage assistant request failed; request_id={request_id}",
        ) from error

    return InvokeResponse(
        request_id=request_id,
        response=response,
        duration_ms=round((time.monotonic() - started) * 1000),
    )
