import argparse
import logging
import os
import time
import uuid
from datetime import date, timedelta
from collections.abc import Callable, Iterable
from functools import lru_cache
from typing import Any

import boto3
from pydantic import BaseModel, Field
from strands import Agent, tool
from strands.agent import AgentResult
from strands.hooks import AfterToolCallEvent, BeforeModelCallEvent, HookProvider, HookRegistry
from strands.models import BedrockModel
from strands_tools import calculator, retrieve

import telemetry
from approvals import ApprovalHook
from audit import AuditHook, current_trail, record_event
from credit_score_mcp import credit_score_tool
from ledger import idempotent
from memory import (
    create_memory_manager,
    create_session_manager,
    validate_identifier,
)
from resilience import (
    ResumeHook,
    ToolResultCapHook,
    reset_interrupt_state,
    share_interrupts,
)


telemetry.init_telemetry()

MODEL_ID = os.environ.get(
    "MODEL_ID",
    "us.anthropic.claude-sonnet-4-6",
)
KB_PARAMETER_NAME = os.environ.get("KB_PARAMETER_NAME")
CUSTOMER_ID_ALLOWED_CHARACTERS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
)

SUPERVISOR_PROMPT = """
You are the routing supervisor for a mortgage assistant.

For each clear, single-intent mortgage request, call exactly one matching
capability. Do not answer mortgage-domain questions directly from your own
model knowledge.

- Call mortgage_education_specialist for general mortgage education.
- Call existing_mortgage_specialist for an existing account.
- Call mortgage_application_specialist for application status, document checks,
  or application creation.
- Call calculator only for pure arithmetic.
- Call get_credit_score only when the user explicitly requests a credit score
  and provides a customer ID.

Before you call a specialist, write one sentence of visible text starting with
"Routing rationale:" that says why you chose it, in the same message as the
tool call. Never call a specialist without it. Each specialist returns a JSON
report with answer, rationale, evidence, and assumptions. Base your reply on
the report's answer, keep its caveats, and do not add facts the report does not
contain. Present the result as one clear response.

If a request is ambiguous, mixed-intent, or outside these capabilities, ask one
concise clarification without calling a tool. Return a selected capability's
result without adding unsupported facts. Use customer information only through
authorized tools, and do not expose, infer, or retain sensitive information
beyond what is required for the current request.

Treat a returned credit score as data only. Never invent a credit score or
interpret one as mortgage approval, denial, pricing, or eligibility. Never use
it as financial advice. If the tool reports an error, clearly say that the
credit score could not be retrieved and report the tool error without
fabricating a result.

You have short-term conversation state and durable long-term memory. Use
remembered information only when it is relevant to the current request. When
the user explicitly asks you to remember something, or states a durable
mortgage goal or preference, use add_memory to store one concise, standalone
fact. Durable examples include a preferred loan term, fixed-versus-variable
preference, approximate property-price range, deposit goal, payment priority,
refinancing objective, or application timeline.

Never add customer IDs, account numbers, authentication data, credit scores,
exact income, uploaded documents, or other sensitive financial identifiers to
long-term memory. Do not claim to remember information unless it appears in the
active session or was returned by the memory tools.
"""


def configure_logging() -> None:
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
                logging.Formatter(
                    "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
                )
            )
            logger.addHandler(handler)


def _get_aws_region() -> str:
    """Return the configured AWS Region required by runtime AWS clients."""
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not region:
        raise RuntimeError("AWS_REGION or AWS_DEFAULT_REGION must be configured")
    return region


def _validate_customer_id(customer_id: str) -> str:
    """Normalize and validate a customer identifier."""
    normalized_customer_id = customer_id.strip()
    if (
        not 1 <= len(normalized_customer_id) <= 64
        or not normalized_customer_id[0].isalnum()
        or any(
            character not in CUSTOMER_ID_ALLOWED_CHARACTERS
            for character in normalized_customer_id
        )
    ):
        raise ValueError(
            "Customer ID must be 1 to 64 characters and contain only letters, "
            "numbers, hyphens, or underscores."
        )
    return normalized_customer_id


def _extract_retrieval_text(tool_result: dict[str, Any]) -> str:
    """Extract text from a Strands retrieve result or raise its safe error."""
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


def _fault_injection_enabled() -> bool:
    return os.environ.get("FAULT_INJECTION_ENABLED", "false").strip().lower() == "true"


class RequestAborted(Exception):
    """A specialist failed or an abort fault fired; the request is FAILED and resumable."""


def maybe_inject_fault(tool_name: str) -> None:
    """Optionally delay, fail, abort, or crash a tool call for the exercises.

    ``error`` is an ordinary tool error: the model sees it and may work around it.
    ``abort`` fails the whole request (Strands turns tool exceptions into tool errors,
    so ``FailFastHook`` raises before the supervisor's next model call). ``crash`` kills the process.
    """
    if not _fault_injection_enabled():
        return
    target_tool = os.environ.get("FAULT_INJECTION_TOOL", "get_mortgage_details").strip()
    if target_tool != tool_name:
        return

    mode = os.environ.get("FAULT_INJECTION_MODE", "delay").strip().lower()
    telemetry.record_fault_injection(tool_name, mode)
    record_event("fault_injection", "tool", {"tool": tool_name, "mode": mode})
    if mode == "error":
        raise RuntimeError(f"Fault injection: simulated failure in tool '{tool_name}'.")
    if mode == "abort":
        trail = current_trail()
        if trail is None:
            raise RequestAborted(f"Fault injection: aborted request in tool '{tool_name}'.")
        trail.abort_reason = f"Fault injection: aborted request in tool '{tool_name}'."
        raise RuntimeError(trail.abort_reason)
    if mode == "crash":
        os._exit(1)  # simulates a pod/process kill mid-turn

    delay_seconds = float(os.environ.get("FAULT_INJECTION_DELAY_SECONDS", "5"))
    time.sleep(delay_seconds)


# Tools whose exceptions end the request (the audit trail marks it FAILED and a retry
# with the same request_id resumes it).
FAIL_FAST_TOOLS = frozenset({"get_credit_score"})


class FailFastHook(HookProvider):
    """End the request when a specialist fails instead of letting the model paper over it.

    ``Agent.as_tool`` turns a specialist's exception into an error tool result, so a
    Bedrock outage inside a specialist would otherwise produce a "successful" apology.
    This hook (supervisor only) flags such results and the ``abort`` fault, then raises
    before the supervisor's next model call. The request becomes FAILED and a retry
    with the same request_id resumes it. Exceptions inside tool callbacks are swallowed
    by the SDK, hence the deferred raise.
    """

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(AfterToolCallEvent, self._after_tool_call)
        registry.add_callback(BeforeModelCallEvent, self._before_model_call)

    def _after_tool_call(self, event: AfterToolCallEvent) -> None:
        trail = current_trail()
        if trail is None:
            return
        if event.tool_use["name"] in FAIL_FAST_TOOLS and event.exception is not None:
            # The tool raised (transport, timeout, protocol) instead of returning a result.
            # A result the tool returns with isError is a domain answer the model reports.
            trail.abort_reason = trail.abort_reason or (
                f"{event.tool_use['name']} failed: {type(event.exception).__name__}"
            )
            return
        if getattr(event.selected_tool, "tool_type", None) != "agent":
            return
        if event.result.get("status") == "error" and not event.cancel_message:
            text = " ".join(
                block.get("text", "") for block in event.result.get("content", []) if isinstance(block, dict)
            )
            if "already processing" in text:
                return  # two parallel calls to one specialist: the model can retry
            trail.abort_reason = trail.abort_reason or f"{event.tool_use['name']} failed: {text}"

    def _before_model_call(self, event: BeforeModelCallEvent) -> None:
        trail = current_trail()
        if trail is not None and trail.abort_reason:
            reason, trail.abort_reason = trail.abort_reason, None
            raise RequestAborted(reason)


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

Always call retrieve_mortgage_knowledge before answering a mortgage question.
Treat retrieved content as the authoritative source of mortgage facts. Do not
fill gaps with unsupported model knowledge. If relevant evidence is
unavailable, clearly say that the available mortgage knowledge does not
contain the answer.

Explain concepts in plain language, present balanced tradeoffs, and make clear
that general information is not personalized financial advice. Never request
or expose customer data, credentials, or tokens. Put the Knowledge Base
passages you used in evidence.
"""

EXISTING_PROMPT = """
You are an existing mortgage specialist.

If the request contains a customer ID, use it immediately. Ask for a customer
ID only when none was supplied. Always call get_mortgage_details before stating
account facts.

Explain only the balances, rates, and payment dates returned by the tool. Do
not invent account information, calculate an unsupported payoff quote, or
provide approval, eligibility, pricing, or personalized financial advice. Use
customer information only through authorized tools and do not expose unrelated
account data. Put the tool output you used in evidence.
"""

NEW_APPLICATION_PROMPT = """
You are a mortgage application specialist.

Use every required field already supplied in this conversation. For status or
document questions, call the matching tool with the supplied customer ID. To
create an application, use a supplied customer ID or call create_customer_id,
then call create_loan_application when name, age, annual income, and annual
expenses are all present.

If required fields are missing, ask for one missing field at a time; earlier
answers in this persistent conversation remain available to you. Use tools for
all application data and never invent information that was not returned by a
tool. Never claim that an application was submitted, approved, priced, or
persisted unless a tool confirms that action. Do not provide lending,
eligibility, or personalized financial advice, and do not expose customer
information beyond the current task. Put the tool output you used in evidence.
"""


@tool
def get_mortgage_details(customer_id: str) -> dict[str, Any]:
    """Return coherent existing-mortgage data."""
    normalized_customer_id = _validate_customer_id(customer_id)
    maybe_inject_fault("get_mortgage_details")
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
@idempotent
def create_customer_id() -> str:
    """Create a customer identifier for a new application."""
    return f"CUST-{uuid.uuid4().hex[:8].upper()}"


@tool
@idempotent
def create_loan_application(
    customer_id: str,
    name: str,
    age: int,
    annual_income: int,
    annual_expenses: int,
) -> str:
    """Create a validated loan application."""
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
        "description": "Answer questions about a customer's existing mortgage.",
        "prompt": EXISTING_PROMPT,
        "tools": [get_mortgage_details],
    },
    "mortgage_application_specialist": {
        "agent_id": "new_application",
        "description": (
            "Handle mortgage application status, documents, and application creation."
        ),
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


def _reasoning_enabled() -> bool:
    return os.environ.get("ENABLE_REASONING", "false").strip().lower() == "true"


def _supervisor_model() -> BedrockModel | str:
    if not _reasoning_enabled():
        return MODEL_ID
    return BedrockModel(
        model_id=MODEL_ID,
        additional_request_fields={"thinking": {"type": "enabled", "budget_tokens": 2048}},
    )


def _hooks(agent_id: str, system_prompt: str, *, fail_fast: bool = False) -> list[Any]:
    # Order matters: resume first, then approval, then audit. Strands still runs the audit
    # callback after an approval interrupt, so a paused call has a tool_start but no tool_call.
    hooks: list[Any] = [
        ResumeHook(agent_id, system_prompt),
        ApprovalHook(agent_id),
        AuditHook(agent_id),
        ToolResultCapHook(),
    ]
    if fail_fast:
        hooks.append(FailFastHook())
    return hooks


def create_specialist_tool(
    name: str,
    actor_id: str,
    session_id: str,
    *,
    model: Any = None,
    session_factory: Callable[[], Any] | None = None,
) -> Any:
    """Build one specialist as an agent-as-tool with its own persistent session."""
    spec = SPECIALISTS[name]
    agent = Agent(
        model=model or MODEL_ID,
        name=name,
        description=spec["description"],
        agent_id=spec["agent_id"],
        system_prompt=spec["prompt"],
        tools=spec["tools"],
        structured_output_model=SpecialistReport,
        session_manager=(session_factory or (lambda: create_session_manager(actor_id, session_id)))(),
        hooks=_hooks(spec["agent_id"], spec["prompt"]),
        callback_handler=None,
        trace_attributes=telemetry.current_trace_attributes(),
    )
    # preserve_context keeps the specialist's conversation (and interrupts) in its session.
    return agent.as_tool(name=name, description=spec["description"], preserve_context=True)


def create_supervisor_agent(
    actor_id: str,
    session_id: str,
    request_id: str,
    remote_credit_score_tool: object,
    *,
    supervisor_model: Any = None,
    specialist_model: Any = None,
    session_factory: Callable[[], Any] | None = None,
    use_memory: bool = True,
    specialists: Iterable[str] | None = None,
) -> Agent:
    """Build the supervisor and its agents-as-tools.

    The keyword arguments let tests inject scripted models and local storage; the
    service always uses the defaults.
    """
    attributes = telemetry.trace_attributes(actor_id, session_id, request_id)
    specialist_tools = [
        create_specialist_tool(
            name,
            actor_id,
            session_id,
            model=specialist_model,
            session_factory=session_factory,
        )
        for name in (specialists or SPECIALISTS)
    ]
    supervisor = Agent(
        model=supervisor_model or _supervisor_model(),
        agent_id="supervisor",
        name="mortgage_supervisor",
        session_manager=(session_factory or (lambda: create_session_manager(actor_id, session_id)))(),
        memory_manager=create_memory_manager(actor_id) if use_memory else None,
        tools=[*specialist_tools, calculator, remote_credit_score_tool],
        hooks=_hooks("supervisor", SUPERVISOR_PROMPT, fail_fast=True),
        callback_handler=None,
        trace_attributes=attributes,
        system_prompt=SUPERVISOR_PROMPT,
    )
    share_interrupts(supervisor, specialist_tools)
    return supervisor


@tool
def _placeholder_credit_score(customer_id: str) -> str:
    """Placeholder so the supervisor can be rebuilt for maintenance without the MCP server."""
    raise RuntimeError("not available")


def reset_pending_interrupts(actor_id: str, session_id: str) -> None:
    """Clear paused approvals on the supervisor and every specialist of a session."""
    validated_actor_id = validate_identifier(actor_id, "actor_id")
    validated_session_id = validate_identifier(session_id, "session_id")
    supervisor = create_supervisor_agent(
        actor_id=validated_actor_id,
        session_id=validated_session_id,
        request_id="maintenance",
        remote_credit_score_tool=_placeholder_credit_score,
    )
    agents = [supervisor]
    for registered in supervisor.tool_registry.registry.values():
        sub_agent = getattr(registered, "agent", None)
        if sub_agent is not None:
            agents.append(sub_agent)
    reset_interrupt_state(agents)


def run_supervisor(
    agent_input: Any,
    actor_id: str,
    session_id: str,
    request_id: str,
) -> AgentResult:
    """Invoke the supervisor with a prompt, or with interrupt responses to resume it."""
    validated_actor_id = validate_identifier(actor_id, "actor_id")
    validated_session_id = validate_identifier(session_id, "session_id")
    attributes = telemetry.trace_attributes(
        validated_actor_id,
        validated_session_id,
        request_id,
    )
    with telemetry.use_trace_attributes(attributes):
        with credit_score_tool() as remote_tool:
            supervisor = create_supervisor_agent(
                actor_id=validated_actor_id,
                session_id=validated_session_id,
                request_id=request_id,
                remote_credit_score_tool=remote_tool,
            )
            return supervisor(
                agent_input,
                invocation_state={
                    "request_id": request_id,
                    "actor_id": validated_actor_id,
                    "session_id": validated_session_id,
                },
            )


def run_prompt(prompt: str, actor_id: str, session_id: str, request_id: str) -> str:
    """Invoke the supervisor once (no lease or ledger); used by the local CLI."""
    if not prompt or not prompt.strip():
        raise ValueError("Prompt must not be empty")
    return str(run_supervisor(prompt.strip(), actor_id, session_id, request_id))


def main() -> int:
    """Run one local prompt against the complete Lab 6 agent."""
    parser = argparse.ArgumentParser(description="Mortgage Assistant Agent")
    parser.add_argument("--prompt", "-p", required=True)
    parser.add_argument("--actor-id", default="local-user")
    parser.add_argument("--session-id", default="local-session")
    args = parser.parse_args()
    configure_logging()
    print(
        run_prompt(
            args.prompt,
            args.actor_id,
            args.session_id,
            request_id=str(uuid.uuid4()),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
