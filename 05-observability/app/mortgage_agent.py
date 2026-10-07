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
KB_PARAMETER_NAME = os.environ.get(
    "KB_PARAMETER_NAME",
    "/workshop/mortgage-assistant/bedrock/knowledge-base-id",
)

SUPERVISOR_PROMPT = """
You are the supervisor for a mortgage assistant.

Route general mortgage information questions to general_mortgage_specialist,
existing-account questions to existing_mortgage_specialist, and new application
questions to new_application_specialist. Use the calculator for calculations.

Before you call a specialist, write one sentence starting with "Routing
rationale:" that says why you chose it. Each specialist returns a JSON report
with answer, rationale, evidence, and assumptions. Base your reply on the
report's answer, keep its caveats, and do not add facts the report does not
contain. Present the result as one clear response.

You have short-term conversation state and durable long-term memory. Use
remembered information only when it is relevant to the current request. When
the user explicitly asks you to remember something, or states a durable
mortgage goal or preference, use add_memory to store one concise, standalone
fact. Durable examples include a preferred loan term, fixed-versus-variable
preference, approximate property-price range, deposit goal, payment priority,
refinancing objective, or application timeline.

Never add customer IDs, account numbers, authentication data,
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


@lru_cache(maxsize=1)
def get_knowledge_base_id() -> str:
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    try:
        parameter = boto3.client("ssm", region_name=region).get_parameter(
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

    os.environ["KNOWLEDGE_BASE_ID"] = knowledge_base_id
    return knowledge_base_id


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
        if trail is None or getattr(event.selected_tool, "tool_type", None) != "agent":
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
You are a mortgage information assistant.

Always use the retrieve tool before answering a mortgage question.
Answer only from information returned by the workshop Knowledge Base.
If the Knowledge Base does not contain the answer, say "I don't know."
Explain concepts in plain language, present balanced tradeoffs, and make
it clear that general information is not personalized financial advice.
Put the Knowledge Base passages you used in evidence.
"""

EXISTING_PROMPT = """
You are an existing-mortgage assistant.

Ask for a customer ID before using the mortgage-details tool. Explain
balances, rates, payment dates, and payoff information clearly. The
returned account data is mock workshop data. Do not invent account
information that was not returned by a tool. Put the tool output you used
in evidence.
"""

NEW_APPLICATION_PROMPT = """
You are a new mortgage application assistant.

Ask for a customer ID first and create one if necessary. Collect name,
age, annual income, and annual expenses one question at a time before
creating an application; earlier answers in this conversation are still
available to you. Use tools for all application data and never invent
information that was not returned by a tool. Put the tool output you used
in evidence.
"""


@tool
def get_mortgage_details(customer_id: str) -> dict:
    """Return mock existing-mortgage data for the workshop."""
    maybe_inject_fault("get_mortgage_details")
    today = date.today()
    return {
        "account_number": customer_id,
        "outstanding_principal": 150000.0,
        "interest_rate": 4.5,
        "maturity_date": "2030-06-30",
        "payments_remaining": 72,
        "last_payment_date": str(today - timedelta(days=30)),
        "next_payment_due": str(today + timedelta(days=1)),
        "next_payment_amount": 1250.0,
    }


@tool
def get_mortgage_app_doc_status(customer_id: str | None = None) -> list[dict]:
    """Return mock required-document status for a mortgage application."""
    return [
        {"type": "proof_of_income", "status": "COMPLETED"},
        {"type": "employment_information", "status": "MISSING"},
        {"type": "proof_of_assets", "status": "COMPLETED"},
        {"type": "credit_information", "status": "COMPLETED"},
    ]


@tool
def get_application_details(customer_id: str | None = None) -> dict:
    """Return mock details about a mortgage application."""
    return {
        "customer_id": customer_id or "123456",
        "application_id": "998776",
        "application_date": str(date.today() - timedelta(days=35)),
        "application_status": "IN_PROGRESS",
        "application_type": "NEW_MORTGAGE",
        "name": "Workshop Customer",
    }


@tool
@idempotent
def create_customer_id() -> str:
    """Create a mock customer ID."""
    return "123456"


@tool
@idempotent
def create_loan_application(
    customer_id: str,
    name: str,
    age: int,
    annual_income: int,
    annual_expense: int,
) -> str:
    """Create a mock loan application."""
    return (
        f"Loan application created for {name} (customer {customer_id}); "
        f"age={age}, annual_income={annual_income}, "
        f"annual_expense={annual_expense}."
    )


SPECIALISTS: dict[str, dict[str, Any]] = {
    "general_mortgage_specialist": {
        "agent_id": "general",
        "description": "Answer general mortgage questions using the workshop Knowledge Base.",
        "prompt": GENERAL_PROMPT,
        "tools": [retrieve],
    },
    "existing_mortgage_specialist": {
        "agent_id": "existing",
        "description": "Answer questions about a mock customer's existing mortgage.",
        "prompt": EXISTING_PROMPT,
        "tools": [get_mortgage_details],
    },
    "new_application_specialist": {
        "agent_id": "new_application",
        "description": "Handle new mortgage application questions and create applications.",
        "prompt": NEW_APPLICATION_PROMPT,
        "tools": [
            get_mortgage_app_doc_status,
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
    # Order matters: resume first, then approval (so a paused call is audited once).
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
    if name == "general_mortgage_specialist" and model is None:
        get_knowledge_base_id()
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
        tools=[*specialist_tools, calculator],
        hooks=_hooks("supervisor", SUPERVISOR_PROMPT, fail_fast=True),
        callback_handler=None,
        trace_attributes=attributes,
        system_prompt=SUPERVISOR_PROMPT,
    )
    share_interrupts(supervisor, specialist_tools)
    return supervisor


def reset_pending_interrupts(actor_id: str, session_id: str) -> None:
    """Clear paused approvals on the supervisor and every specialist of a session."""
    validated_actor_id = validate_identifier(actor_id, "actor_id")
    validated_session_id = validate_identifier(session_id, "session_id")
    supervisor = create_supervisor_agent(
        actor_id=validated_actor_id,
        session_id=validated_session_id,
        request_id="maintenance",
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
        supervisor = create_supervisor_agent(
            actor_id=validated_actor_id,
            session_id=validated_session_id,
            request_id=request_id,
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
    """Run one local prompt against the complete Lab 5 agent."""
    parser = argparse.ArgumentParser(description="Mortgage Assistant Agent")
    parser.add_argument("--prompt", "-p", required=True)
    parser.add_argument("--actor-id", default="local-workshop-user")
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
