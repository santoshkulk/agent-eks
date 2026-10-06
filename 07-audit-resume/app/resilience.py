"""Crash-safe turns for agents with message-level snapshots.

Each agent records an ``inflight`` marker (``agent.state``) when a new turn starts
and clears it when the invocation ends. The marker is persisted with the snapshot,
so if a pod dies or the invocation raises, the next invocation finds it and rolls
the conversation back to the start of the unfinished turn (keeping the message
list valid: no dangling ``toolUse`` or consecutive user turns). Side effects the
unfinished turn already performed are protected by the tool ledger.

The same hook re-applies the code's system prompt, because a restored snapshot
otherwise overwrites it with the prompt from the day the session started.
"""

from typing import Any

from strands.hooks import (
    AfterInvocationEvent,
    BeforeInvocationEvent,
    HookProvider,
    HookRegistry,
)

from audit import current_trail, record_event

INFLIGHT_KEY = "inflight"


class ResumeHook(HookProvider):
    def __init__(self, agent_id: str, system_prompt: str) -> None:
        self.agent_id = agent_id
        self.system_prompt = system_prompt

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self._before_invocation)
        registry.add_callback(AfterInvocationEvent, self._after_invocation)

    def _before_invocation(self, event: BeforeInvocationEvent) -> None:
        agent = event.agent
        agent.system_prompt = self.system_prompt

        stale = agent.state.get(INFLIGHT_KEY)
        if stale:
            start = int(stale.get("start", len(agent.messages)))
            discarded = max(len(agent.messages) - start, 0)
            if 0 <= start <= len(agent.messages):
                del agent.messages[start:]
            agent.state.delete(INFLIGHT_KEY)
            record_event(
                "rollback",
                self.agent_id,
                {"discarded_messages": discarded, "from_request": stale.get("request_id")},
            )

        # An empty input list is an interrupt resume: the turn is already in progress.
        if event.messages:
            trail = current_trail()
            agent.state.set(
                INFLIGHT_KEY,
                {
                    "start": len(agent.messages),
                    "request_id": trail.request_id if trail else None,
                },
            )

    def _after_invocation(self, event: AfterInvocationEvent) -> None:
        # A missing result means the invocation raised: keep the marker for rollback.
        if event.result is not None:
            event.agent.state.delete(INFLIGHT_KEY)


def share_interrupts(parent: Any, agent_tools: list[Any]) -> None:
    """Re-link nested interrupts after a restore so a reviewer's answer reaches the specialist.

    ``Agent.as_tool`` resumes a paused specialist from the *same* ``Interrupt`` objects
    the parent registered when the interrupt was raised. That holds inside one process,
    but after a restart the parent and the specialist each restore their own copy from
    their own snapshot, so the parent's response never reaches the specialist and it
    asks again. Pointing the parent's entry at the specialist's object restores the
    in-process behavior. This reads SDK-private ``_interrupt_state``; it is a no-op
    when nothing is pending and is covered by ``test_agent_flows``.
    """
    parent_state = getattr(parent, "_interrupt_state", None)
    if parent_state is None or not parent_state.activated:
        return
    for agent_tool in agent_tools:
        child_state = getattr(getattr(agent_tool, "agent", None), "_interrupt_state", None)
        if child_state is None:
            continue
        for interrupt_id, interrupt in child_state.interrupts.items():
            if interrupt_id in parent_state.interrupts:
                parent_state.interrupts[interrupt_id] = interrupt
