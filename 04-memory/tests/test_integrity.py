"""Audit anchor, tool-result cap, and the SDK private-API contract the labs rely on."""

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))
os.environ.setdefault("MEMORY_TABLE_NAME", "test-memory-table")

from strands import Agent  # noqa: E402
from strands.agent._agent_as_tool import _AgentAsTool  # noqa: E402
from strands.hooks import AfterToolCallEvent  # noqa: E402

from audit import AuditTrail, load_records, use_audit_trail  # noqa: E402
from resilience import ToolResultCapHook  # noqa: E402
from service import Orchestrator  # noqa: E402
from store import InMemoryItemStore  # noqa: E402

ACTOR, SESSION = "actor-1", "session-1"


def _finished_orchestrator():
    store = InMemoryItemStore()
    result = MagicMock(stop_reason="end_turn", interrupts=None)
    result.__str__.return_value = "the answer"
    return Orchestrator(store, lambda *args: result, set()), store


class AuditAnchorTests(unittest.TestCase):
    def test_anchor_matches_until_trailing_records_are_deleted(self) -> None:
        orchestrator, store = _finished_orchestrator()
        orchestrator.invoke(ACTOR, SESSION, "r1", "hello")

        trail = orchestrator.trail(ACTOR, SESSION, "r1")
        self.assertTrue(trail["chain_valid"])
        self.assertTrue(trail["anchor_valid"])

        # Remove the last record: the remaining chain still verifies, the anchor does not.
        records = store.query(f"user/{ACTOR}", f"audit/{SESSION}/r1/")
        del store._items[(f"user/{ACTOR}", records[-1]["sk"])]
        trail = orchestrator.trail(ACTOR, SESSION, "r1")
        self.assertTrue(trail["chain_valid"])
        self.assertFalse(trail["anchor_valid"])

    def test_anchor_covers_failed_and_cancelled_requests(self) -> None:
        store = InMemoryItemStore()

        def boom(*args):
            raise RuntimeError("down")

        orchestrator = Orchestrator(store, boom, set(), reset_agents=MagicMock())
        with self.assertRaises(RuntimeError):
            orchestrator.invoke(ACTOR, SESSION, "r1", "hello")
        self.assertTrue(orchestrator.trail(ACTOR, SESSION, "r1")["anchor_valid"])

        paused = orchestrator.executions.begin(ACTOR, SESSION, "r2", "apply")
        orchestrator.executions.interrupt(paused, [{"id": "i1", "name": "approve_x"}])
        orchestrator.cancel(ACTOR, SESSION, "r2")
        self.assertTrue(orchestrator.trail(ACTOR, SESSION, "r2")["anchor_valid"])

    def test_running_requests_have_no_anchor_yet(self) -> None:
        orchestrator, _ = _finished_orchestrator()
        orchestrator.executions.begin(ACTOR, SESSION, "r1", "hello")
        self.assertIsNone(orchestrator.trail(ACTOR, SESSION, "r1")["anchor_valid"])


class ToolResultCapTests(unittest.TestCase):
    def _event(self, text):
        return AfterToolCallEvent(
            agent=MagicMock(),
            selected_tool=None,
            tool_use={"name": "retrieve_mortgage_knowledge", "toolUseId": "t", "input": {}},
            invocation_state={},
            result={"toolUseId": "t", "status": "success", "content": [{"text": text}, {"json": {"k": 1}}]},
        )

    def test_oversized_text_is_truncated_and_audited(self) -> None:
        trail = AuditTrail(InMemoryItemStore(), ACTOR, SESSION, "r1")
        event = self._event("x" * 5000)
        with use_audit_trail(trail):
            ToolResultCapHook(limit=1000)._after_tool_call(event)
        text = event.result["content"][0]["text"]
        self.assertLess(len(text), 1100)
        self.assertIn("truncated 4000 characters", text)
        self.assertEqual(event.result["content"][1], {"json": {"k": 1}})
        types = [r["type"] for r in load_records(trail.store, ACTOR, SESSION, "r1")]
        self.assertEqual(types, ["tool_result_truncated"])

    def test_small_results_are_untouched(self) -> None:
        event = self._event("small")
        original = event.result
        ToolResultCapHook(limit=1000)._after_tool_call(event)
        self.assertIs(event.result, original)


class SdkPrivateApiContractTests(unittest.TestCase):
    """share_interrupts and reset_interrupt_state read SDK-private attributes.

    If a Strands upgrade renames them this fails loudly instead of the approval flow
    silently breaking. Re-run the approval exercise after changing the pinned version.
    """

    def test_agent_interrupt_state_shape(self) -> None:
        agent = Agent(model=MagicMock(), callback_handler=None)
        state = agent._interrupt_state
        for name in ("activated", "interrupts", "deactivate"):
            self.assertTrue(hasattr(state, name), name)

    def test_agent_as_tool_exposes_the_wrapped_agent(self) -> None:
        agent = Agent(model=MagicMock(), name="x", description="d", callback_handler=None)
        wrapped = agent.as_tool(name="x", description="d", preserve_context=True)
        self.assertIsInstance(wrapped, _AgentAsTool)
        self.assertIs(wrapped.agent, agent)

    def test_session_manager_hook_for_sync_agent(self) -> None:
        from strands.session import SnapshotSessionManager

        self.assertTrue(callable(getattr(SnapshotSessionManager, "sync_agent", None)))


if __name__ == "__main__":
    unittest.main()
