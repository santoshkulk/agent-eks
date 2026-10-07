"""A deterministic Strands model for tests: a handler decides each response."""

import json
import uuid
from collections.abc import AsyncIterable, Callable
from typing import Any

from strands.models import Model

Block = tuple  # ("text", str) | ("tool", name, input_dict)
Handler = Callable[[list[dict[str, Any]], list[dict[str, Any]] | None, Any], list[Block]]


def last_tool_result(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the toolResult in the final user message, if the turn is mid tool loop."""
    if not messages or messages[-1]["role"] != "user":
        return None
    for block in messages[-1]["content"]:
        if "toolResult" in block:
            return block["toolResult"]
    return None


def forced_tool(tool_choice: Any) -> str | None:
    if isinstance(tool_choice, dict) and "tool" in tool_choice:
        return tool_choice["tool"]["name"]
    return None


class ScriptedModel(Model):
    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.calls = 0

    def update_config(self, **model_config: Any) -> None:
        pass

    def get_config(self) -> Any:
        return {}

    async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError
        yield  # pragma: no cover

    async def stream(  # type: ignore[override]
        self,
        messages,
        tool_specs=None,
        system_prompt=None,
        *,
        tool_choice=None,
        **kwargs,
    ) -> AsyncIterable[dict[str, Any]]:
        self.calls += 1
        blocks = self.handler(messages, tool_specs, tool_choice)
        has_tool = any(block[0] == "tool" for block in blocks)
        yield {"messageStart": {"role": "assistant"}}
        for index, block in enumerate(blocks):
            if block[0] == "text":
                yield {"contentBlockStart": {"contentBlockIndex": index, "start": {}}}
                yield {
                    "contentBlockDelta": {
                        "contentBlockIndex": index,
                        "delta": {"text": block[1]},
                    }
                }
            else:
                yield {
                    "contentBlockStart": {
                        "contentBlockIndex": index,
                        "start": {
                            "toolUse": {
                                "toolUseId": f"tool-{uuid.uuid4().hex[:12]}",
                                "name": block[1],
                            }
                        },
                    }
                }
                yield {
                    "contentBlockDelta": {
                        "contentBlockIndex": index,
                        "delta": {"toolUse": {"input": json.dumps(block[2])}},
                    }
                }
            yield {"contentBlockStop": {"contentBlockIndex": index}}
        yield {"messageStop": {"stopReason": "tool_use" if has_tool else "end_turn"}}
        yield {
            "metadata": {
                "usage": {"inputTokens": 3, "outputTokens": 2, "totalTokens": 5},
                "metrics": {"latencyMs": 1},
            }
        }
