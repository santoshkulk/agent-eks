import argparse
import logging
import os
import re
import sys
from collections.abc import Callable
from contextvars import ContextVar
from datetime import date, timedelta
from functools import lru_cache
from typing import Any
from uuid import uuid4

import boto3
from strands import Agent, tool
from strands_tools import calculator, retrieve


logger = logging.getLogger(__name__)
MODEL_ID = os.environ.get(
    "MODEL_ID",
    "us.anthropic.claude-sonnet-4-6",
)
KB_PARAMETER_NAME = os.environ.get("KB_PARAMETER_NAME")
CUSTOMER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SPECIALIST_TOOLS = {
    "answer_general_mortgage_questions": "mortgage_education_specialist",
    "answer_existing_mortgage_questions": "existing_mortgage_specialist",
    "answer_mortgage_application_questions": "mortgage_application_specialist",
}
_REQUEST_ID: ContextVar[str | None] = ContextVar("mortgage_request_id", default=None)


def configure_logging() -> None:
    """Configure metadata-only application traces and quiet framework logging."""
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        )
        logger.addHandler(handler)
    logger.propagate = False

    for logger_name in (
        "strands",
        "strands.agent",
        "strands.tools",
        "strands.models",
        "strands.bedrock",
    ):
        logging.getLogger(logger_name).setLevel(logging.WARNING)


def create_trace_callback(agent_name: str) -> Callable[..., None]:
    """Create a callback that logs request-correlated tool metadata only."""

    def trace_callback(**kwargs: Any) -> None:
        event = kwargs.get("event")
        if not isinstance(event, dict):
            return

        tool_use = event.get("contentBlockStart", {}).get("start", {}).get("toolUse")
        if not isinstance(tool_use, dict):
            return

        tool_name = tool_use.get("name")
        if not isinstance(tool_name, str):
            return

        request_id = _REQUEST_ID.get()
        specialist_name = SPECIALIST_TOOLS.get(tool_name)
        if specialist_name:
            logger.info(
                "[delegate] request_id=%s agent=%s specialist=%s tool=%s",
                request_id or "unscoped",
                agent_name,
                specialist_name,
                tool_name,
            )
        else:
            logger.info(
                "[tool] request_id=%s agent=%s tool=%s",
                request_id or "unscoped",
                agent_name,
                tool_name,
            )

    return trace_callback


def _get_aws_region() -> str:
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not region:
        raise RuntimeError("AWS_REGION or AWS_DEFAULT_REGION must be configured")
    return region


def _validate_customer_id(customer_id: str) -> str:
    normalized_customer_id = customer_id.strip()
    if not CUSTOMER_ID_PATTERN.fullmatch(normalized_customer_id):
        raise ValueError(
            "Customer ID must be 1 to 64 characters and contain only letters, "
            "numbers, hyphens, or underscores."
        )
    return normalized_customer_id


def _extract_retrieval_text(tool_result: dict[str, Any]) -> str:
    content = tool_result.get("content", [])
    text = "\n".join(
        item["text"]
        for item in content
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    ).strip()

    if tool_result.get("status") != "success":
        detail = text or "The retrieval tool returned an unknown error."
        raise RuntimeError(f"Mortgage knowledge retrieval failed: {detail}")

    return text or "No relevant results were found in the mortgage knowledge base."


@lru_cache(maxsize=1)
def get_knowledge_base_id() -> str:
    """Resolve the configured Knowledge Base ID from Parameter Store."""
    if not KB_PARAMETER_NAME:
        raise RuntimeError("KB_PARAMETER_NAME must be configured")

    try:
        parameter = boto3.client("ssm", region_name=_get_aws_region()).get_parameter(
            Name=KB_PARAMETER_NAME
        )
    except Exception as error:
        raise RuntimeError(
            f"Unable to retrieve the Knowledge Base ID from "
            f"{KB_PARAMETER_NAME}: {error}"
        ) from error

    knowledge_base_id = parameter["Parameter"]["Value"].strip()
    if not knowledge_base_id:
        raise RuntimeError(f"SSM parameter {KB_PARAMETER_NAME} is empty")

    return knowledge_base_id


@tool
def retrieve_mortgage_knowledge(query: str) -> str:
    """Retrieve information from the configured mortgage knowledge base."""
    normalized_query = query.strip()
    if not normalized_query:
        raise ValueError("Retrieval query must not be empty")

    tool_result = retrieve(
        {
            "toolUseId": f"mortgage-knowledge-{uuid4().hex}",
            "input": {
                "text": normalized_query,
                "knowledgeBaseId": get_knowledge_base_id(),
                "region": _get_aws_region(),
                "numberOfResults": 5,
                "score": 0.4,
            },
        }
    )
    return _extract_retrieval_text(tool_result)


@tool
def answer_general_mortgage_questions(query: str) -> str:
    """Delegate general mortgage education questions to the grounded specialist."""
    agent = Agent(
        name="mortgage_education_specialist",
        description=(
            "Answers general mortgage education questions using the configured "
            "mortgage knowledge base."
        ),
        model=MODEL_ID,
        tools=[retrieve_mortgage_knowledge],
        system_prompt="""
        You are a mortgage education specialist.

        Always call retrieve_mortgage_knowledge before answering a mortgage
        question. Treat retrieved content as the authoritative source of
        mortgage facts. Do not fill gaps with unsupported model knowledge. If
        relevant evidence is unavailable, clearly say that the available
        mortgage knowledge does not contain the answer.

        Explain concepts in plain language, present balanced tradeoffs, and make
        clear that general information is not personalized financial advice.
        Never request or expose customer data, credentials, or tokens.
        """,
        callback_handler=create_trace_callback("mortgage_education_specialist"),
    )
    return str(agent(query))


@tool
def get_mortgage_details(customer_id: str) -> dict[str, Any]:
    """Return coherent mortgage details for the specified customer."""
    normalized_customer_id = _validate_customer_id(customer_id)
    today = date.today()
    return {
        "customer_id": normalized_customer_id,
        "account_number": f"MORTGAGE-{normalized_customer_id}",
        "outstanding_principal": 150000.0,
        "interest_rate": 4.5,
        "last_payment_date": str(today - timedelta(days=30)),
        "next_payment_due": str(today + timedelta(days=1)),
        "next_payment_amount": 1250.0,
    }


@tool
def answer_existing_mortgage_questions(query: str) -> str:
    """Delegate existing-mortgage questions to the account specialist."""
    agent = Agent(
        name="existing_mortgage_specialist",
        description="Answers questions about existing mortgage accounts.",
        model=MODEL_ID,
        tools=[get_mortgage_details],
        system_prompt="""
        You are an existing mortgage specialist.

        If the request contains a customer ID, use it immediately. Ask for a
        customer ID only when none was supplied. Always call
        get_mortgage_details before stating account facts.

        Explain only the balances, rates, and payment dates returned by the
        tool. Do not invent account information, calculate an unsupported payoff
        quote, or provide approval, eligibility, pricing, or personalized
        financial advice. Use customer information only through authorized
        tools and do not expose unrelated account data.
        """,
        callback_handler=create_trace_callback("existing_mortgage_specialist"),
    )
    return str(agent(query))


@tool
def get_mortgage_application_document_statuses(
    customer_id: str,
) -> dict[str, Any]:
    """Return document statuses for a mortgage application."""
    normalized_customer_id = _validate_customer_id(customer_id)
    return {
        "customer_id": normalized_customer_id,
        "documents": [
            {"type": "proof_of_income", "status": "COMPLETED"},
            {"type": "employment_information", "status": "MISSING"},
            {"type": "proof_of_assets", "status": "COMPLETED"},
            {"type": "credit_information", "status": "COMPLETED"},
        ],
    }


@tool
def get_application_details(customer_id: str) -> dict[str, str]:
    """Return mortgage application details for the specified customer."""
    normalized_customer_id = _validate_customer_id(customer_id)
    return {
        "customer_id": normalized_customer_id,
        "application_id": "998776",
        "application_date": str(date.today() - timedelta(days=35)),
        "application_status": "IN_PROGRESS",
        "application_type": "NEW_MORTGAGE",
    }


@tool
def generate_customer_id() -> str:
    """Generate a customer identifier for a new application."""
    return f"CUST-{uuid4().hex[:8].upper()}"


@tool
def prepare_mortgage_application(
    customer_id: str,
    name: str,
    age: int,
    annual_income: int,
    annual_expenses: int,
) -> str:
    """Prepare a non-submitted mortgage application for review."""
    normalized_customer_id = _validate_customer_id(customer_id)
    normalized_name = name.strip()
    if not normalized_name or len(normalized_name) > 100:
        raise ValueError("Name must contain between 1 and 100 characters.")
    if not 18 <= age <= 100:
        raise ValueError("Age must be between 18 and 100.")
    if not 0 < annual_income <= 10_000_000:
        raise ValueError("Annual income must be between 1 and 10,000,000.")
    if not 0 <= annual_expenses <= annual_income:
        raise ValueError(
            "Annual expenses must be non-negative and no greater than annual income."
        )

    return (
        f"Mortgage application prepared for {normalized_name} "
        f"(customer {normalized_customer_id}); age={age}, "
        f"annual_income={annual_income}, annual_expenses={annual_expenses}. "
        "The application is ready for review but has not been submitted or persisted."
    )


@tool
def answer_mortgage_application_questions(query: str) -> str:
    """Delegate application status, document, and preparation questions."""
    agent = Agent(
        name="mortgage_application_specialist",
        description=(
            "Handles mortgage application status, document checks, and "
            "application preparation."
        ),
        model=MODEL_ID,
        tools=[
            get_mortgage_application_document_statuses,
            get_application_details,
            generate_customer_id,
            prepare_mortgage_application,
        ],
        system_prompt="""
        You are a mortgage application specialist.

        Use every required field already supplied in the request. For status or
        document questions, call the matching tool with the supplied customer
        ID. To prepare an application, use a supplied customer ID or call
        generate_customer_id, then call prepare_mortgage_application when name,
        age, annual income, and annual expenses are all present.

        If required fields are missing, list all missing fields in one response;
        do not collect them one question at a time because this command does not
        retain conversation state. Never claim that an application was
        submitted, approved, priced, or persisted unless a tool confirms that
        action. Do not provide lending, eligibility, or personalized financial
        advice, and do not expose customer information beyond the current task.
        """,
        callback_handler=create_trace_callback("mortgage_application_specialist"),
    )
    return str(agent(query))


def create_supervisor_agent() -> Agent:
    """Create the top-level agent that routes one-shot mortgage requests."""
    return Agent(
        name="mortgage_supervisor",
        description="Routes mortgage requests to one specialist or tool.",
        model=MODEL_ID,
        tools=[
            answer_general_mortgage_questions,
            answer_existing_mortgage_questions,
            answer_mortgage_application_questions,
            calculator,
        ],
        system_prompt="""
        You are the routing supervisor for a mortgage assistant.

        For each clear, single-intent mortgage request, call exactly one matching
        capability. Do not answer mortgage-domain questions directly from your
        own model knowledge.

        - Call answer_general_mortgage_questions for general mortgage education.
        - Call answer_existing_mortgage_questions for an existing account.
        - Call answer_mortgage_application_questions for application status,
          document checks, or application preparation.
        - Call calculator only for pure arithmetic.

        If a request is ambiguous, mixed-intent, or outside these capabilities,
        ask one concise clarification without calling a tool. Return a selected
        tool's result without adding unsupported facts. Use customer information
        only through authorized tools, and do not expose, infer, or retain
        sensitive information beyond what is required for the current request.
        """,
        callback_handler=create_trace_callback("mortgage_supervisor"),
    )


def run_prompt(prompt: str, request_id: str | None = None) -> str:
    """Run one stateless prompt with request-correlated trace context."""
    if not prompt or not prompt.strip():
        raise ValueError("Prompt must not be empty")

    trace_request_id = request_id or str(uuid4())
    token = _REQUEST_ID.set(trace_request_id)
    try:
        return str(create_supervisor_agent()(prompt.strip()))
    finally:
        _REQUEST_ID.reset(token)


def main() -> int:
    """Run one mortgage assistant prompt from the command line."""
    parser = argparse.ArgumentParser(description="Mortgage Assistant Agent")
    parser.add_argument(
        "--prompt",
        "-p",
        required=True,
        help="Prompt to send to the mortgage assistant.",
    )
    args = parser.parse_args()
    configure_logging()
    print(run_prompt(args.prompt), file=sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
