"""Strands supervisor with traced specialists, supervisor-only memory, and MCP."""

import argparse
import logging
import os
import re
import uuid
from collections.abc import Callable, Iterable
from datetime import date, timedelta
from functools import lru_cache
from typing import Any

import boto3
from pydantic import BaseModel, Field
from strands import Agent, tool
from strands.agent import AgentResult
from strands_tools import calculator
from strands_tools.retrieve import retrieve

import telemetry
from credit_score_mcp import credit_score_tool
from memory import create_memory_manager, create_session_manager, validate_identifier
from resilience import CurrentPromptHook, ToolResultCapHook


telemetry.init_telemetry()

MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-6")
KB_PARAMETER_NAME = os.environ.get("KB_PARAMETER_NAME")
CUSTOMER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

SUPERVISOR_PROMPT = """
You are the routing supervisor for a mortgage assistant.

For each clear, single-intent mortgage request, call exactly one matching
capability. Do not answer mortgage-domain questions directly from your own
model knowledge.

- Call mortgage_education_specialist for general mortgage education.
- Call existing_mortgage_specialist for an existing mortgage account.
- Call mortgage_application_specialist for application status, document
  checks, or application creation.
- Call get_credit_score only when the user explicitly requests a credit score
  and provides a customer ID.
- Call calculator only for pure arithmetic.

Before calling a specialist, write one sentence of visible text starting with
"Routing rationale:" that explains why you chose it, in the same message as the
tool call. Never call a specialist without this rationale. If a request is
ambiguous, mixed-intent, outside these capabilities, or asks for a credit score
without a customer ID, ask one concise clarifying question without calling a
tool.

Each specialist returns a JSON report with answer, rationale, evidence, and
assumptions. Base your response on the report's answer, preserve its caveats,
and do not add facts the report does not contain. Never claim that a customer ID
or application was created unless the specialist reports the result of the
corresponding creation tool.

Treat a returned credit score as synthetic data only. Never interpret a score
as approval, denial, pricing, eligibility, compliance evidence, personalized
financial advice, or any lending decision. Never invent a score. If
get_credit_score returns an error, report the tool error without fabricating a
result.

You have short-term conversation state and durable long-term memory. Use
remembered information only when it is relevant to the current request. When
the user explicitly asks you to remember something, or states a durable
mortgage goal or preference, use add_memory to store one concise, standalone
fact. Durable examples include a preferred loan term, fixed-versus-variable
preference, approximate property-price range, deposit goal, payment priority,
refinancing objective, or application timeline.

Never add customer IDs, credit scores, account numbers, authentication data,
exact income, uploaded documents, or other sensitive financial identifiers to
long-term memory. Do not claim to remember information unless it appears in the
active session or was returned by the memory tools. Use customer information
only through authorized tools, and do not expose, infer, or retain sensitive
information beyond what the current request requires.
"""


def configure_logging() -> None:
    """Configure concise module-aware logging for Strands components."""
    for logger_name in (
        "strands",
        "strands.agent",
        "strands.tools",
        "strands.models",
        "strands.bedrock",
    ):
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.INFO)
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(
                logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
            )
            logger.addHandler(handler)


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
    """Resolve and cache the configured Knowledge Base ID from Parameter Store."""
    if not KB_PARAMETER_NAME:
        raise RuntimeError("KB_PARAMETER_NAME must be configured")
    try:
        parameter = boto3.client("ssm", region_name=_get_aws_region()).get_parameter(
            Name=KB_PARAMETER_NAME
        )
    except Exception as error:
        raise RuntimeError(
            f"Unable to retrieve the Knowledge Base ID from {KB_PARAMETER_NAME}: {error}"
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
            "toolUseId": f"mortgage-knowledge-{uuid.uuid4().hex}",
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


class SpecialistReport(BaseModel):
    """Structured result every specialist returns to the supervisor."""

    answer: str = Field(description="The answer for the user, in plain language.")
    rationale: str = Field(description="One or two sentences on how the answer was reached.")
    evidence: list[str] = Field(
        default_factory=list,
        description="Knowledge Base passages or tool outputs the answer relies on.",
    )
    assumptions: list[str] = Field(
        default_factory=list,
        description="Anything assumed or missing that the user should know.",
    )


GENERAL_PROMPT = """
You are a mortgage education specialist.

Call retrieve_mortgage_knowledge exactly once for each mortgage question. Do
not retry or reformulate the retrieval. Treat a successful retrieval as
available even when it returns zero passages or passages that do not directly
answer the question. Use retrieved passages as the authoritative source of
mortgage facts and never fill gaps with model knowledge.

If no returned passage directly supports the requested answer, set `answer` to
exactly "The configured mortgage Knowledge Base does not contain enough
information to answer that question." and leave `evidence` empty. Do not
describe the Knowledge Base as unavailable, use outside knowledge, or recommend
external websites, agencies, or professionals. Say retrieval failed only when
the tool explicitly reports an error; in that case set `answer` to exactly "The
mortgage Knowledge Base retrieval failed, so I cannot provide a grounded
answer." and leave `evidence` empty.

When evidence directly supports an answer, explain it in plain language,
present balanced tradeoffs, and make clear that general information is not
personalized financial advice. Put only the Knowledge Base passages you used in
`evidence`. Never request or expose customer data, credentials, or tokens.
"""

EXISTING_PROMPT = """
You are an existing mortgage specialist.

If the request or conversation contains a customer ID, use it immediately. Ask
for a customer ID only when none was supplied. Always call get_mortgage_details
before stating account facts.

Explain only the balances, rates, and payment dates returned by the tool. Do
not invent account information, calculate an unsupported payoff quote, or
provide approval, eligibility, pricing, or personalized financial advice. Put
the tool output you used in evidence. Use customer information only through
authorized tools and do not expose unrelated account data.
"""

NEW_APPLICATION_PROMPT = """
You are a mortgage application specialist.

Use every required field already supplied in the current request or earlier in
this conversation; never ask for a supplied field again. For application status
or document questions, call the matching tool with the supplied customer ID,
and ask for the ID only when it is missing.

To create an application, use a supplied customer ID or call create_customer_id
when one is needed. Collect any missing name, age, annual income, and annual
expenses one question at a time. Call create_loan_application only after every
required field is available and the user has asked to create the application.

Never say that a customer ID or application was created before the corresponding
tool executes successfully. Never claim that an application was approved,
priced, or persisted beyond what the creation tool reports. Use tools for all
application data, do not invent information, and put the tool outputs you used
in evidence. Do not provide lending, eligibility, or personalized financial
advice, and do not expose customer information beyond the current task.
"""


@tool
def get_mortgage_details(customer_id: str) -> dict[str, Any]:
    """Return mortgage details for the specified customer."""
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
def get_mortgage_application_document_statuses(customer_id: str) -> dict[str, Any]:
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
def create_customer_id() -> str:
    """Create a synthetic customer identifier for a new application."""
    return f"CUST-{uuid.uuid4().hex[:8].upper()}"


@tool
def create_loan_application(
    customer_id: str,
    name: str,
    age: int,
    annual_income: int,
    annual_expenses: int,
) -> str:
    """Create a synthetic mortgage application after validating its inputs."""
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
        f"Loan application created for {normalized_name} "
        f"(customer {normalized_customer_id}); age={age}, "
        f"annual_income={annual_income}, annual_expenses={annual_expenses}."
    )


SPECIALISTS: dict[str, dict[str, Any]] = {
    "mortgage_education_specialist": {
        "agent_id": "general",
        "description": (
            "Answer general mortgage education questions using the configured "
            "mortgage Knowledge Base."
        ),
        "prompt": GENERAL_PROMPT,
        "tools": [retrieve_mortgage_knowledge],
    },
    "existing_mortgage_specialist": {
        "agent_id": "existing",
        "description": "Answer questions about an existing mortgage account.",
        "prompt": EXISTING_PROMPT,
        "tools": [get_mortgage_details],
    },
    "mortgage_application_specialist": {
        "agent_id": "new_application",
        "description": "Handle mortgage application status, document checks, and creation.",
        "prompt": NEW_APPLICATION_PROMPT,
        "tools": [
            get_mortgage_application_document_statuses,
            get_application_details,
            create_customer_id,
            create_loan_application,
        ],
    },
}
SPECIALIST_TOOL_NAMES = frozenset(SPECIALISTS)


def create_specialist_tool(name: str, *, model: Any = None) -> Any:
    """Build a stateless traced specialist and expose it through ``Agent.as_tool``."""
    spec = SPECIALISTS[name]
    if name == "mortgage_education_specialist" and model is None:
        get_knowledge_base_id()
    agent = Agent(
        model=model if model is not None else MODEL_ID,
        name=name,
        description=spec["description"],
        agent_id=spec["agent_id"],
        system_prompt=spec["prompt"],
        tools=spec["tools"],
        structured_output_model=SpecialistReport,
        callback_handler=None,
        trace_attributes=telemetry.current_trace_attributes(),
    )
    return agent.as_tool(name=name, description=spec["description"], preserve_context=True)


def create_supervisor_agent(
    actor_id: str,
    session_id: str,
    request_id: str,
    remote_credit_score_tool: Any,
    *,
    supervisor_model: Any = None,
    specialist_model: Any = None,
    session_factory: Callable[[], Any] | None = None,
    use_memory: bool = True,
    specialists: Iterable[str] | None = None,
) -> Agent:
    """Build one traced supervisor with MCP, memory, and stateless specialists."""
    attributes = telemetry.current_trace_attributes() or telemetry.trace_attributes(
        actor_id, session_id, request_id
    )
    with telemetry.use_trace_attributes(attributes):
        specialist_tools = [
            create_specialist_tool(name, model=specialist_model)
            for name in (specialists or SPECIALISTS)
        ]
        session_manager = (
            session_factory()
            if session_factory is not None
            else create_session_manager(actor_id, session_id)
        )
        return Agent(
            model=supervisor_model if supervisor_model is not None else MODEL_ID,
            agent_id="supervisor",
            name="mortgage_supervisor",
            session_manager=session_manager,
            memory_manager=create_memory_manager(actor_id) if use_memory else None,
            tools=[*specialist_tools, calculator, remote_credit_score_tool],
            hooks=[CurrentPromptHook(SUPERVISOR_PROMPT), ToolResultCapHook()],
            callback_handler=None,
            trace_attributes=attributes,
            system_prompt=SUPERVISOR_PROMPT,
        )


def run_supervisor(
    agent_input: str,
    actor_id: str,
    session_id: str,
    request_id: str,
) -> AgentResult:
    """Discover MCP, then invoke a fresh supervisor while its context remains open."""
    validated_actor_id = validate_identifier(actor_id, "actor_id")
    validated_session_id = validate_identifier(session_id, "session_id")
    attributes = telemetry.trace_attributes(
        validated_actor_id, validated_session_id, request_id
    )
    with telemetry.use_trace_attributes(attributes):
        with credit_score_tool() as remote_credit_score_tool:
            supervisor = create_supervisor_agent(
                validated_actor_id,
                validated_session_id,
                request_id,
                remote_credit_score_tool=remote_credit_score_tool,
            )
            return supervisor(
                agent_input,
                invocation_state={
                    "request_id": request_id,
                    "actor_id": validated_actor_id,
                    "session_id": validated_session_id,
                },
            )


def run_prompt(prompt: str, actor_id: str, session_id: str) -> str:
    """Validate and run one local prompt against the complete Lab 6 agent."""
    if not prompt or not prompt.strip():
        raise ValueError("Prompt must not be empty")
    return str(
        run_supervisor(
            prompt.strip(), actor_id, session_id, request_id=str(uuid.uuid4())
        )
    )


def main() -> int:
    """Run one local prompt against the complete Lab 6 agent."""
    parser = argparse.ArgumentParser(description="Mortgage Assistant Agent")
    parser.add_argument("--prompt", "-p", required=True)
    parser.add_argument("--actor-id", default="local-user")
    parser.add_argument("--session-id", default="local-session")
    args = parser.parse_args()
    configure_logging()
    print(run_prompt(args.prompt, args.actor_id, args.session_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
