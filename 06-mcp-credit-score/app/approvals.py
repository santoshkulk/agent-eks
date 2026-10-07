"""Human approval gates built on Strands interrupts.

``ApprovalHook`` raises an interrupt before a gated tool runs. The interrupt
propagates through ``Agent.as_tool`` to the supervisor, the API returns 202 with
the pending approvals, and the interrupt state is persisted in the agent snapshots,
so a reviewer can answer from any pod after a restart.
"""

import os
from typing import Any

from strands.hooks import BeforeToolCallEvent, HookProvider, HookRegistry

from audit import record_event

APPROVAL_REQUIRED_TOOLS = frozenset(
    name.strip()
    for name in os.environ.get("APPROVAL_REQUIRED_TOOLS", "create_loan_application").split(",")
    if name.strip()
)


def normalize_decision(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        return {
            "approved": bool(response.get("approved")),
            "comment": str(response.get("comment") or ""),
            "reviewer": str(response.get("reviewer") or "unknown"),
        }
    approved = str(response).strip().lower() in ("y", "yes", "true", "approve", "approved")
    return {"approved": approved, "comment": "", "reviewer": "unknown"}


class ApprovalHook(HookProvider):
    def __init__(
        self,
        agent_id: str,
        required_tools: frozenset[str] = APPROVAL_REQUIRED_TOOLS,
    ) -> None:
        self.agent_id = agent_id
        self.required_tools = required_tools

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        # Register before AuditHook so a paused call is audited once, when it runs.
        registry.add_callback(BeforeToolCallEvent, self._before_tool_call)

    def _before_tool_call(self, event: BeforeToolCallEvent) -> None:
        name = event.tool_use["name"]
        if name not in self.required_tools:
            return
        response = event.interrupt(
            f"approve_{name}",
            reason={
                "tool": name,
                "input": event.tool_use["input"],
                "agent": self.agent_id,
            },
        )
        decision = normalize_decision(response)
        record_event("approval", self.agent_id, {"tool": name, **decision})
        if not decision["approved"]:
            comment = f": {decision['comment']}" if decision["comment"] else ""
            event.cancel_tool = f"Denied by reviewer {decision['reviewer']}{comment}"
