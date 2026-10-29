"""Small hooks that keep supervisor snapshots current and bounded."""

import os
from typing import Any

from strands.hooks import (
    AfterToolCallEvent,
    BeforeInvocationEvent,
    HookProvider,
    HookRegistry,
)


MAX_TOOL_RESULT_CHARS = int(os.environ.get("MAX_TOOL_RESULT_CHARS", "16000"))


class CurrentPromptHook(HookProvider):
    """Reapply the deployed prompt after a session snapshot is restored.

    ``SnapshotSessionManager`` restores the prompt captured in the latest
    snapshot. Reapplying the current prompt lets existing sessions receive
    prompt updates after a rollout without changing their conversation.
    """

    def __init__(self, system_prompt: str) -> None:
        """Store the current prompt to apply before every invocation."""
        self.system_prompt = system_prompt

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        """Register the pre-invocation prompt refresh callback."""
        registry.add_callback(BeforeInvocationEvent, self._before_invocation)

    def _before_invocation(self, event: BeforeInvocationEvent) -> None:
        event.agent.system_prompt = self.system_prompt


class ToolResultCapHook(HookProvider):
    """Truncate oversized tool results before snapshot persistence."""

    def __init__(self, limit: int = MAX_TOOL_RESULT_CHARS) -> None:
        """Set the maximum number of characters retained per text block."""
        self.limit = limit

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        """Register the post-tool-call result cap callback."""
        registry.add_callback(AfterToolCallEvent, self._after_tool_call)

    def _after_tool_call(self, event: AfterToolCallEvent) -> None:
        content = event.result.get("content") or []
        capped = []
        changed = False
        for block in content:
            text = block.get("text") if isinstance(block, dict) else None
            if isinstance(text, str) and len(text) > self.limit:
                omitted = len(text) - self.limit
                block = {
                    **block,
                    "text": text[: self.limit]
                    + f"\n...[truncated {omitted} characters]",
                }
                changed = True
            capped.append(block)
        if changed:
            event.result = {**event.result, "content": capped}
