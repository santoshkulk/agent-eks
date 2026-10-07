"""End-to-end flows with scripted models: audit, explainability, resume, approval.

These run the real supervisor, agents-as-tools, hooks, session snapshots (local
files), orchestrator, and in-memory item store. Only the model is scripted.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("MEMORY_TABLE_NAME", "test-memory-table")

from strands.session import SnapshotSessionManager  # noqa: E402
from strands.storage.local_file_storage import LocalFileStorage  # noqa: E402

import mortgage_agent  # noqa: E402
from audit import load_records, verify_chain  # noqa: E402
from execution import (  # noqa: E402
    COMPLETED,
    FAILED,
    INTERRUPTED,
    ExecutionStore,
    PendingApproval,
)
from fixtures.scripted_model import (  # noqa: E402
    ScriptedModel,
    forced_tool,
    last_tool_result,
)
from service import Orchestrator  # noqa: E402
from store import InMemoryItemStore  # noqa: E402

ACTOR = "actor-1"
SESSION = "session-1"


def user_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message["role"] == "user":
            texts = [b["text"] for b in message["content"] if "text" in b]
            if texts:
                return texts[0]
    return ""


def supervisor_handler(messages, tool_specs, tool_choice):
    if last_tool_result(messages) is not None:
        return [("text", "Here is your answer based on the specialist report.")]
    prompt = user_text(messages)
    if "balance" in prompt:
        return [
            ("text", "Routing rationale: the user asks about an existing account."),
            ("tool", "existing_mortgage_specialist", {"input": prompt}),
        ]
    if "apply" in prompt:
        return [
            ("text", "Routing rationale: the user wants a new application."),
            ("tool", "new_application_specialist", {"input": prompt}),
        ]
    return [("text", "Hello")]


def specialist_handler(messages, tool_specs, tool_choice):
    if forced_tool(tool_choice) == "SpecialistReport":
        return [
            (
                "tool",
                "SpecialistReport",
                {
                    "answer": "ok",
                    "rationale": "used the account tool",
                    "evidence": ["get_mortgage_details returned principal 150000"],
                },
            )
        ]
    if last_tool_result(messages) is not None:
        return [("text", "done")]
    names = {spec["name"] for spec in tool_specs or []}
    if "get_mortgage_details" in names:
        return [("tool", "get_mortgage_details", {"customer_id": "C1"})]
    if "create_loan_application" in names:
        return [
            (
                "tool",
                "create_loan_application",
                {
                    "customer_id": "C1",
                    "name": "Sam",
                    "age": 30,
                    "annual_income": 90000,
                    "annual_expense": 40000,
                },
            )
        ]
    return [("text", "nothing to do")]


class Harness:
    def __init__(self, base_dir: str) -> None:
        self.base_dir = base_dir
        self.store = InMemoryItemStore()
        self.supervisor_model_calls = 0
        self.orchestrator = Orchestrator(
            self.store,
            self.run_agent,
            mortgage_agent.SPECIALIST_TOOL_NAMES,
            ExecutionStore(self.store, lease_seconds=60),
        )

    def session_factory(self, session_id: str):
        def make():
            return SnapshotSessionManager(
                session_id,
                storage=LocalFileStorage(self.base_dir),
                save_latest_on="message",
                snapshot_trigger=lambda *, agent_data, **kwargs: True,
            )

        return make

    def run_agent(self, agent_input, actor_id, session_id, request_id):
        supervisor = mortgage_agent.create_supervisor_agent(
            actor_id,
            session_id,
            request_id,
            supervisor_model=ScriptedModel(supervisor_handler),
            specialist_model=ScriptedModel(specialist_handler),
            session_factory=self.session_factory(session_id),
            use_memory=False,
            specialists=["existing_mortgage_specialist", "new_application_specialist"],
        )
        return supervisor(agent_input)

    def restored_messages(self, session_id: str = SESSION) -> dict[str, list[dict]]:
        """Messages each agent restores from its snapshot (what a new pod would see)."""
        supervisor = mortgage_agent.create_supervisor_agent(
            ACTOR,
            session_id,
            "inspect",
            supervisor_model=ScriptedModel(supervisor_handler),
            specialist_model=ScriptedModel(specialist_handler),
            session_factory=self.session_factory(session_id),
            use_memory=False,
            specialists=["existing_mortgage_specialist", "new_application_specialist"],
        )
        found = {"supervisor": list(supervisor.messages)}
        for registered in supervisor.tool_registry.registry.values():
            sub = getattr(registered, "agent", None)
            if sub is not None:
                found[sub.agent_id] = list(sub.messages)
        return found

    def records(self, request_id: str):
        return load_records(self.store, ACTOR, SESSION, request_id)

    def types(self, request_id: str) -> list[tuple[str, str]]:
        return [(r["agent_id"], r["type"]) for r in self.records(request_id)]


class ReasoningConfigTests(unittest.TestCase):
    def test_reasoning_is_off_by_default_and_configures_thinking_when_enabled(self) -> None:
        with patch.dict(os.environ, {"ENABLE_REASONING": "false"}):
            self.assertEqual(mortgage_agent._supervisor_model(), mortgage_agent.MODEL_ID)
        with patch.dict(os.environ, {"ENABLE_REASONING": "true"}):
            model = mortgage_agent._supervisor_model()
        fields = model.get_config()["additional_request_fields"]
        self.assertEqual(fields["thinking"]["type"], "enabled")
        self.assertGreater(fields["thinking"]["budget_tokens"], 1000)


class AgentFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.h = Harness(self.tmp.name)

    def test_audit_trail_and_explanation_for_agent_as_tool_route(self) -> None:
        outcome = self.h.orchestrator.invoke(ACTOR, SESSION, "req-1", "What is my balance?")

        self.assertEqual(outcome.status, COMPLETED)
        records = self.h.records("req-1")
        self.assertTrue(verify_chain(records))
        types = self.h.types("req-1")
        # The specialist's own tool call is audited under its agent_id.
        self.assertIn(("existing", "tool_call"), types)
        self.assertIn(("supervisor", "tool_call"), types)
        trail = self.h.orchestrator.trail(ACTOR, SESSION, "req-1")
        self.assertTrue(trail["chain_valid"])
        route = trail["explanation"]["route"]
        self.assertEqual(route[0]["agent"], "existing_mortgage_specialist")
        self.assertIn("existing account", route[0]["reason"])
        self.assertTrue(trail["explanation"]["evidence"])

    def test_completed_request_replays_without_running_agents(self) -> None:
        self.h.orchestrator.invoke(ACTOR, SESSION, "req-2", "What is my balance?")
        before = len(self.h.records("req-2"))

        replay = self.h.orchestrator.invoke(ACTOR, SESSION, "req-2", "What is my balance?")

        self.assertEqual(replay.execution.mode, "replay")
        self.assertEqual(len(self.h.records("req-2")), before)

    def test_failed_request_resumes_with_rollback_on_same_request_id(self) -> None:
        fault = {
            "FAULT_INJECTION_ENABLED": "true",
            "FAULT_INJECTION_MODE": "abort",
            "FAULT_INJECTION_TOOL": "get_mortgage_details",
        }
        with patch.dict(os.environ, fault):
            with self.assertRaises(BaseException):
                self.h.orchestrator.invoke(ACTOR, SESSION, "req-3", "What is my balance?")
        failed = self.h.orchestrator.executions.get(ACTOR, SESSION, "req-3")
        assert failed is not None
        self.assertEqual(failed.status, FAILED)

        outcome = self.h.orchestrator.invoke(ACTOR, SESSION, "req-3", "What is my balance?")

        self.assertEqual(outcome.status, COMPLETED)
        self.assertEqual(outcome.execution.attempt, 2)
        types = self.h.types("req-3")
        self.assertIn(("supervisor", "rollback"), types)
        for agent_id, messages in self.h.restored_messages().items():
            for previous, current in zip(messages, messages[1:]):
                if previous["role"] != current["role"]:
                    continue
                # The SDK's Bedrock model separates a tool-result-only user turn from the
                # next user turn at request time (structured output ends a turn that way).
                tool_result_only = previous["role"] == "user" and all(
                    "toolResult" in block for block in previous["content"]
                )
                self.assertTrue(
                    tool_result_only,
                    f"{agent_id} has consecutive {previous['role']} messages",
                )
        supervisor_messages = self.h.restored_messages()["supervisor"]
        user_prompts = [
            m for m in supervisor_messages
            if m["role"] == "user" and any("text" in b for b in m["content"])
        ]
        self.assertEqual(len(user_prompts), 1, "the failed attempt's prompt must not be duplicated")
        self.assertIn("execution_failed", [t for _, t in types])
        self.assertTrue(verify_chain(self.h.records("req-3")))

    def test_gated_tool_pauses_for_approval_then_runs_once(self) -> None:
        paused = self.h.orchestrator.invoke(ACTOR, SESSION, "req-4", "Please apply now")

        self.assertEqual(paused.status, INTERRUPTED)
        self.assertEqual(paused.interrupts[0]["name"], "approve_create_loan_application")
        ledger_keys = [i["sk"] for i in self.h.store.query(f"user/{ACTOR}", "ledger/")]
        self.assertEqual(ledger_keys, [])

        interrupt_id = paused.interrupts[0]["id"]
        done = self.h.orchestrator.decide(
            ACTOR,
            SESSION,
            "req-4",
            [{"interrupt_id": interrupt_id, "approved": True, "reviewer": "pat"}],
        )

        self.assertEqual(done.status, COMPLETED)
        approvals = [r for r in self.h.records("req-4") if r["type"] == "approval"]
        self.assertEqual(approvals[0]["data"]["reviewer"], "pat")
        ledger_keys = [i["sk"] for i in self.h.store.query(f"user/{ACTOR}", "ledger/")]
        self.assertEqual(len(ledger_keys), 1)
        self.assertTrue(verify_chain(self.h.records("req-4")))

    def test_failed_approval_run_can_be_resubmitted(self) -> None:
        paused = self.h.orchestrator.invoke(ACTOR, SESSION, "req-6", "Please apply now")
        decision = [{"interrupt_id": paused.interrupts[0]["id"], "approved": True}]
        real_run = self.h.orchestrator.run_agent

        def failing_run(*args):
            raise RuntimeError("down")

        self.h.orchestrator.run_agent = failing_run
        with self.assertRaises(RuntimeError):
            self.h.orchestrator.decide(ACTOR, SESSION, "req-6", decision)
        stored = self.h.orchestrator.executions.get(ACTOR, SESSION, "req-6")
        self.assertEqual(stored.status, INTERRUPTED)

        self.h.orchestrator.run_agent = real_run
        done = self.h.orchestrator.decide(ACTOR, SESSION, "req-6", decision)
        self.assertEqual(done.status, COMPLETED)

    def test_lease_is_kept_alive_while_the_agent_runs(self) -> None:
        seen = {}
        real_run = self.h.orchestrator.run_agent

        def slow_run(*args):
            import time

            time.sleep(0.4)
            lock = self.h.store.get(f"user/{ACTOR}", f"lock/{SESSION}")
            seen["expires"] = lock["lease_expires_at"]
            return real_run(*args)

        self.h.orchestrator.executions.lease_seconds = 1
        self.h.orchestrator.run_agent = slow_run
        self.h.orchestrator.invoke(ACTOR, SESSION, "req-9", "What is my balance?")
        self.assertIn("expires", seen)

    def test_new_request_waits_while_another_is_paused(self) -> None:
        self.h.orchestrator.invoke(ACTOR, SESSION, "req-7", "Please apply now")
        with self.assertRaises(PendingApproval):
            self.h.orchestrator.invoke(ACTOR, SESSION, "req-8", "What is my balance?")

    def test_denied_approval_blocks_the_tool(self) -> None:
        paused = self.h.orchestrator.invoke(ACTOR, SESSION, "req-5", "Please apply now")
        interrupt_id = paused.interrupts[0]["id"]

        done = self.h.orchestrator.decide(
            ACTOR,
            SESSION,
            "req-5",
            [{"interrupt_id": interrupt_id, "approved": False, "comment": "no"}],
        )

        self.assertEqual(done.status, COMPLETED)
        ledger_keys = [i["sk"] for i in self.h.store.query(f"user/{ACTOR}", "ledger/")]
        self.assertEqual(ledger_keys, [])
        calls = [r for r in self.h.records("req-5") if r["type"] == "tool_call"]
        cancelled = [c for c in calls if c["data"]["tool"] == "create_loan_application"]
        self.assertTrue(cancelled)
        self.assertIn("Denied", json.dumps(cancelled[0]["data"]["result"]))


if __name__ == "__main__":
    unittest.main()
