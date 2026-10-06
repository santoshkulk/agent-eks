"""Request orchestration: lease, audit trail, resume, and approval handling.

The HTTP layer calls ``Orchestrator.invoke`` and ``Orchestrator.decide``. Both take
the session lease, bind an ``AuditTrail`` for the request, run the supervisor
(``run_agent``), and record the outcome on the execution record.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from approvals import normalize_decision
from audit import (
    AuditTrail,
    build_explanation,
    load_records,
    use_audit_trail,
    verify_chain,
)
from execution import (
    COMPLETED,
    FAILED,
    INTERRUPTED,
    Execution,
    ExecutionNotFound,
    ExecutionStore,
    InvalidExecutionState,
)
from store import ItemStore

logger = logging.getLogger("service")

# run_agent(agent_input, actor_id, session_id, request_id) -> AgentResult-like object
RunAgent = Callable[[Any, str, str, str], Any]


@dataclass
class Outcome:
    execution: Execution
    response: str | None = None
    interrupts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def status(self) -> str:
        return self.execution.status


class Orchestrator:
    def __init__(
        self,
        store: ItemStore,
        run_agent: RunAgent,
        specialist_tools: frozenset[str] | set[str],
        executions: ExecutionStore | None = None,
    ) -> None:
        self.store = store
        self.run_agent = run_agent
        self.specialist_tools = set(specialist_tools)
        self.executions = executions or ExecutionStore(store)

    # -- invoke / resume ------------------------------------------------------
    def invoke(
        self,
        actor_id: str,
        session_id: str,
        request_id: str,
        prompt: str,
        trace_id: str | None = None,
    ) -> Outcome:
        execution = self.executions.begin(
            actor_id, session_id, request_id, prompt, trace_id=trace_id
        )
        if execution.mode == "replay":
            return Outcome(execution, response=execution.response)
        if execution.mode == "pending_approval":
            return Outcome(execution, interrupts=execution.interrupts)
        return self._run(execution, prompt.strip(), trace_id)

    def decide(
        self,
        actor_id: str,
        session_id: str,
        request_id: str,
        decisions: list[dict[str, Any]],
        trace_id: str | None = None,
    ) -> Outcome:
        """Apply reviewer decisions to an INTERRUPTED request and continue it."""
        current = self.executions.get(actor_id, session_id, request_id)
        if current is None:
            raise ExecutionNotFound(f"no execution {request_id}")
        pending = {item["id"] for item in current.interrupts}
        answered = {item["interrupt_id"] for item in decisions}
        if current.status != INTERRUPTED:
            raise InvalidExecutionState(
                f"execution {request_id} is {current.status}, not awaiting approval"
            )
        if answered != pending:
            raise InvalidExecutionState(
                f"decisions must answer exactly the pending approvals: {sorted(pending)}"
            )
        responses = [
            {
                "interruptResponse": {
                    "interruptId": item["interrupt_id"],
                    "response": normalize_decision(item),
                }
            }
            for item in decisions
        ]
        execution = self.executions.begin_approval(actor_id, session_id, request_id)
        return self._run(execution, responses, trace_id, decisions=decisions)

    def _run(
        self,
        execution: Execution,
        agent_input: Any,
        trace_id: str | None,
        decisions: list[dict[str, Any]] | None = None,
    ) -> Outcome:
        try:
            trail = AuditTrail(
                self.store,
                execution.actor_id,
                execution.session_id,
                execution.request_id,
                attempt=execution.attempt,
                trace_id=trace_id or execution.trace_id,
            )
        except Exception as error:
            self.executions.fail(execution, f"audit unavailable: {error}")
            raise

        with use_audit_trail(trail):
            trail.record(
                "execution_start",
                "api",
                {
                    "mode": execution.mode,
                    "attempt": execution.attempt,
                    "prompt": execution.prompt,
                    "decisions": decisions,
                },
            )
            try:
                result = self.run_agent(
                    agent_input,
                    execution.actor_id,
                    execution.session_id,
                    execution.request_id,
                )
            except BaseException as error:
                # BaseException too: aborted requests must be recorded and resumable.
                trail.record(
                    "execution_failed",
                    "api",
                    {"error_type": type(error).__name__, "error": str(error)},
                )
                message = f"{type(error).__name__}: {error}"
                if execution.mode == "approval":
                    # The agents are still paused: let the reviewer resubmit the decisions.
                    self.executions.interrupt(execution, execution.interrupts, error=message)
                else:
                    self.executions.fail(execution, message)
                raise

            if getattr(result, "stop_reason", None) == "interrupt":
                interrupts = [
                    {"id": item.id, "name": item.name, "reason": item.reason}
                    for item in (result.interrupts or [])
                ]
                trail.record("execution_interrupted", "api", {"interrupts": interrupts})
                self.executions.interrupt(execution, interrupts)
                return Outcome(execution, interrupts=interrupts)

            response = str(result)
            trail.record("execution_completed", "api", {"response": response})
            self.executions.complete(execution, response)
            return Outcome(execution, response=response)

    # -- read side --------------------------------------------------------------
    def trail(self, actor_id: str, session_id: str, request_id: str) -> dict[str, Any]:
        execution = self.executions.get(actor_id, session_id, request_id)
        if execution is None:
            raise ExecutionNotFound(f"no execution {request_id}")
        records = load_records(self.store, actor_id, session_id, request_id)
        return {
            "execution": execution.summary(),
            "chain_valid": verify_chain(records),
            "explanation": build_explanation(records, self.specialist_tools),
            "records": records,
        }

    def explanation(self, actor_id: str, session_id: str, request_id: str) -> dict[str, Any]:
        records = load_records(self.store, actor_id, session_id, request_id)
        return build_explanation(records, self.specialist_tools)

    def session_executions(self, actor_id: str, session_id: str) -> list[dict[str, Any]]:
        return [item.summary() for item in self.executions.list_session(actor_id, session_id)]


__all__ = ["Orchestrator", "Outcome", "COMPLETED", "FAILED", "INTERRUPTED"]
