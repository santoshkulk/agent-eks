import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))
os.environ.setdefault("MEMORY_TABLE_NAME", "test-memory-table")

from strands.hooks import BeforeToolCallEvent  # noqa: E402

import memory  # noqa: E402
from audit import (  # noqa: E402
    AuditHook,
    AuditTrail,
    build_explanation,
    load_records,
    redact,
    use_audit_trail,
    verify_chain,
)
from execution import (  # noqa: E402
    COMPLETED,
    FAILED,
    INTERRUPTED,
    RUNNING,
    ExecutionStore,
    InvalidExecutionState,
    PendingApproval,
    RequestMismatch,
    SessionBusy,
)
from ledger import idempotent  # noqa: E402
from store import InMemoryItemStore  # noqa: E402

ACTOR, SESSION = "actor-1", "session-1"


class ItemStoreTests(unittest.TestCase):
    def test_if_absent_and_expect_conditions(self) -> None:
        store = InMemoryItemStore()
        self.assertTrue(store.put("p", "a", {"v": 1}, if_absent=True))
        self.assertFalse(store.put("p", "a", {"v": 2}, if_absent=True))
        self.assertFalse(store.put("p", "a", {"v": 3}, expect={"v": 9}))
        self.assertTrue(store.put("p", "a", {"v": 3}, expect={"v": 1}))
        self.assertEqual(store.get("p", "a")["v"], 3)

    def test_query_filters_prefix_in_sort_order(self) -> None:
        store = InMemoryItemStore()
        for key in ("x/2", "x/1", "y/1"):
            store.put("p", key, {})
        self.assertEqual([i["sk"] for i in store.query("p", "x/")], ["x/1", "x/2"])


class AuditTrailTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryItemStore()

    def trail(self, attempt: int = 1) -> AuditTrail:
        return AuditTrail(self.store, ACTOR, SESSION, "req", attempt=attempt)

    def test_chain_is_valid_and_continues_across_attempts(self) -> None:
        first = self.trail()
        first.record("a", "supervisor", {"n": 1})
        first.record("b", "supervisor", {"n": 2})
        second = self.trail(attempt=2)
        second.record("c", "supervisor", {"n": 3})

        records = load_records(self.store, ACTOR, SESSION, "req")
        self.assertEqual([r["seq"] for r in records], [0, 1, 2])
        self.assertEqual([r["attempt"] for r in records], [1, 1, 2])
        self.assertTrue(verify_chain(records))

    def test_tampering_is_detected(self) -> None:
        trail = self.trail()
        for n in range(3):
            trail.record("step", "supervisor", {"n": n})
        records = load_records(self.store, ACTOR, SESSION, "req")

        records[1]["data"]["n"] = 99
        self.assertFalse(verify_chain(records))
        self.assertFalse(verify_chain([records[0], records[2]]))

    def test_redaction_and_truncation(self) -> None:
        cleaned = redact({"Authorization": "secret", "note": "x" * 5000, "nested": [{"token": "t"}]})
        self.assertEqual(cleaned["Authorization"], "[REDACTED]")
        self.assertEqual(cleaned["nested"][0]["token"], "[REDACTED]")
        self.assertIn("truncated", cleaned["note"])

    def test_non_critical_failure_is_dropped_but_critical_raises(self) -> None:
        trail = self.trail()
        with patch.object(self.store, "put", side_effect=RuntimeError("ddb down")):
            trail.record("noncritical", "supervisor", {})
            self.assertEqual(trail.dropped, 1)
            with self.assertRaises(Exception):
                trail.record("critical", "supervisor", {}, critical=True)

    def test_side_effect_tool_is_blocked_when_audit_is_unavailable(self) -> None:
        trail = self.trail()
        hook = AuditHook("new_application")
        event = BeforeToolCallEvent(
            agent=MagicMock(),
            selected_tool=None,
            tool_use={"name": "create_loan_application", "toolUseId": "t1", "input": {}},
            invocation_state={},
        )
        with use_audit_trail(trail), patch.object(
            self.store, "put", side_effect=RuntimeError("ddb down")
        ):
            hook._before_tool_call(event)
        self.assertIn("Audit trail unavailable", str(event.cancel_tool))

    def test_read_only_tool_is_not_blocked_when_audit_is_unavailable(self) -> None:
        trail = self.trail()
        hook = AuditHook("existing")
        event = BeforeToolCallEvent(
            agent=MagicMock(),
            selected_tool=None,
            tool_use={"name": "get_mortgage_details", "toolUseId": "t1", "input": {}},
            invocation_state={},
        )
        with use_audit_trail(trail), patch.object(
            self.store, "put", side_effect=RuntimeError("ddb down")
        ):
            hook._before_tool_call(event)
        self.assertFalse(event.cancel_tool)

    def test_tampered_records_do_not_break_the_explanation_or_the_chain_check(self) -> None:
        trail = self.trail()
        trail.record("tool_call", "supervisor", {"tool": "x", "toolUseId": "u", "status": "success", "result": []})
        trail.record("model_response", "supervisor", {})
        key = [i["sk"] for i in self.store.query(f"user/{ACTOR}", "audit/")][0]
        item = self.store.get(f"user/{ACTOR}", key)
        item["record"] = json.dumps({**json.loads(item["record"]), "data": {"tampered": True}})
        self.store.put(f"user/{ACTOR}", key, {k: v for k, v in item.items() if k not in ("pk", "sk")})
        records = load_records(self.store, ACTOR, SESSION, "req")
        self.assertFalse(verify_chain(records))
        self.assertEqual(build_explanation(records, {"x"})["malformed_records"], 1)

        item["record"] = "not json at all"
        self.store.put(f"user/{ACTOR}", key, {k: v for k, v in item.items() if k not in ("pk", "sk")})
        records = load_records(self.store, ACTOR, SESSION, "req")
        self.assertFalse(verify_chain(records))
        build_explanation(records, {"x"})

    def test_explanation_flags_a_missing_rationale(self) -> None:
        trail = self.trail()
        trail.record("decision", "supervisor", {"text": "", "tool_uses": [{"name": "x_specialist", "toolUseId": "u9"}]})
        trail.record("tool_call", "supervisor", {"tool": "x_specialist", "toolUseId": "u9", "status": "success", "result": []})
        explanation = build_explanation(load_records(self.store, ACTOR, SESSION, "req"), {"x_specialist"})
        self.assertFalse(explanation["route"][0]["rationale_recorded"])
        self.assertEqual(explanation["route"][0]["reason"], "")

    def test_explanation_links_route_reason_and_evidence(self) -> None:
        trail = self.trail()
        trail.record(
            "decision",
            "supervisor",
            {
                "text": "Routing rationale: account question",
                "tool_uses": [{"name": "existing_mortgage_specialist", "toolUseId": "u1"}],
            },
        )
        trail.record(
            "tool_call",
            "supervisor",
            {
                "tool": "existing_mortgage_specialist",
                "toolUseId": "u1",
                "status": "success",
                "result": [{"json": {"answer": "a", "evidence": ["principal 150000"]}}],
            },
        )
        trail.record("memory_read", "supervisor", {"results": [{"key": "memories/1", "score": 0.9}]})
        explanation = build_explanation(
            load_records(self.store, ACTOR, SESSION, "req"), {"existing_mortgage_specialist"}
        )
        self.assertEqual(explanation["route"][0]["reason"], "Routing rationale: account question")
        self.assertTrue(explanation["route"][0]["rationale_recorded"])
        self.assertEqual(explanation["evidence"], ["principal 150000"])
        self.assertEqual(explanation["memories_used"][0]["key"], "memories/1")


class ExecutionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryItemStore()
        self.now = 1000.0
        self.executions = ExecutionStore(
            self.store, lease_seconds=60, owner="pod-a", clock=lambda: self.now
        )

    def test_lifecycle_complete_then_replay(self) -> None:
        execution = self.executions.begin(ACTOR, SESSION, "r1", "hello")
        self.assertEqual((execution.status, execution.attempt, execution.mode), (RUNNING, 1, "new"))
        self.executions.complete(execution, "answer")

        replay = self.executions.begin(ACTOR, SESSION, "r1", "hello")
        self.assertEqual((replay.mode, replay.response), ("replay", "answer"))
        # Replay did not take the session lease.
        self.executions.begin(ACTOR, SESSION, "r2", "next")

    def test_session_lease_blocks_other_requests_until_released_or_expired(self) -> None:
        self.executions.begin(ACTOR, SESSION, "r1", "hello")
        with self.assertRaises(SessionBusy):
            self.executions.begin(ACTOR, SESSION, "r2", "other")
        self.now += 61
        self.executions.begin(ACTOR, SESSION, "r2", "other")

    def test_double_submit_of_a_running_request_is_rejected(self) -> None:
        self.executions.begin(ACTOR, SESSION, "r1", "hello")
        with self.assertRaises(SessionBusy):
            self.executions.begin(ACTOR, SESSION, "r1", "hello")

    def test_different_sessions_do_not_block_each_other(self) -> None:
        self.executions.begin(ACTOR, SESSION, "r1", "hello")
        self.executions.begin(ACTOR, "session-2", "r2", "hello")

    def test_failed_request_resumes_with_next_attempt(self) -> None:
        execution = self.executions.begin(ACTOR, SESSION, "r1", "hello")
        self.executions.fail(execution, "boom")

        resumed = self.executions.begin(ACTOR, SESSION, "r1", "hello")

        self.assertEqual((resumed.mode, resumed.attempt, resumed.status), ("resume", 2, RUNNING))
        self.assertEqual(self.executions.get(ACTOR, SESSION, "r1").status, RUNNING)

    def test_request_id_reused_for_different_prompt_is_rejected(self) -> None:
        execution = self.executions.begin(ACTOR, SESSION, "r1", "hello")
        self.executions.fail(execution, "boom")
        with self.assertRaises(RequestMismatch):
            self.executions.begin(ACTOR, SESSION, "r1", "something else")

    def test_interrupted_request_waits_for_approval(self) -> None:
        execution = self.executions.begin(ACTOR, SESSION, "r1", "apply")
        self.executions.interrupt(execution, [{"id": "i1", "name": "approve_x"}])

        pending = self.executions.begin(ACTOR, SESSION, "r1", "apply")
        self.assertEqual(pending.mode, "pending_approval")
        self.assertEqual(pending.status, INTERRUPTED)

        resumed = self.executions.begin_approval(ACTOR, SESSION, "r1")
        self.assertEqual((resumed.mode, resumed.attempt), ("approval", 2))
        self.executions.complete(resumed, "done")
        self.assertEqual(self.executions.get(ACTOR, SESSION, "r1").status, COMPLETED)

    def test_new_request_is_blocked_while_another_awaits_approval(self) -> None:
        paused = self.executions.begin(ACTOR, SESSION, "r1", "apply")
        self.executions.interrupt(paused, [{"id": "i1", "name": "approve_x"}])

        with self.assertRaises(PendingApproval):
            self.executions.begin(ACTOR, SESSION, "r2", "something else")
        # Other sessions are unaffected, and r1 itself can still be answered.
        self.executions.begin(ACTOR, "session-2", "r3", "hello")
        self.executions.begin_approval(ACTOR, SESSION, "r1")

    def test_interrupt_keeps_error_for_a_failed_approval_run(self) -> None:
        paused = self.executions.begin(ACTOR, SESSION, "r1", "apply")
        self.executions.interrupt(paused, [{"id": "i1", "name": "approve_x"}])
        resumed = self.executions.begin_approval(ACTOR, SESSION, "r1")

        self.executions.interrupt(resumed, resumed.interrupts, error="boom")

        stored = self.executions.get(ACTOR, SESSION, "r1")
        self.assertEqual((stored.status, stored.error), (INTERRUPTED, "boom"))
        self.assertEqual(stored.interrupts[0]["id"], "i1")

    def test_approval_requires_interrupted_state(self) -> None:
        execution = self.executions.begin(ACTOR, SESSION, "r1", "apply")
        self.executions.fail(execution, "boom")
        with self.assertRaises(InvalidExecutionState):
            self.executions.begin_approval(ACTOR, SESSION, "r1")
        self.assertEqual(self.executions.get(ACTOR, SESSION, "r1").status, FAILED)

    def test_lease_is_released_when_begin_fails(self) -> None:
        execution = self.executions.begin(ACTOR, SESSION, "r1", "hello")
        self.executions.fail(execution, "boom")
        with self.assertRaises(RequestMismatch):
            self.executions.begin(ACTOR, SESSION, "r1", "different")
        self.executions.begin(ACTOR, SESSION, "r3", "ok")


class LeaseHeartbeatTests(unittest.TestCase):
    def test_renew_extends_only_the_owners_lease(self) -> None:
        store = InMemoryItemStore()
        now = {"t": 1000.0}
        mine = ExecutionStore(store, lease_seconds=60, owner="a", clock=lambda: now["t"])
        other = ExecutionStore(store, lease_seconds=60, owner="b", clock=lambda: now["t"])
        execution = mine.begin(ACTOR, SESSION, "r1", "hello")

        now["t"] += 50
        self.assertTrue(mine.renew_lease(ACTOR, SESSION, "r1"))
        now["t"] += 50  # past the original expiry, inside the renewed one
        with self.assertRaises(SessionBusy):
            other.begin(ACTOR, SESSION, "r2", "x")
        self.assertFalse(other.renew_lease(ACTOR, SESSION, "r1"))
        mine.complete(execution, "done")
        self.assertFalse(mine.renew_lease(ACTOR, SESSION, "r1"))

    def test_heartbeat_keeps_a_long_request_from_being_taken_over(self) -> None:
        import time as real_time

        store = InMemoryItemStore()
        # Whole-second timestamps: a 2 s lease leaves at least 1 s of margin per renewal.
        mine = ExecutionStore(store, lease_seconds=2, owner="a")
        other = ExecutionStore(store, lease_seconds=2, owner="b")
        execution = mine.begin(ACTOR, SESSION, "r1", "hello")

        with mine.heartbeat(execution) as heartbeat:
            real_time.sleep(3.0)  # longer than the lease
            with self.assertRaises(SessionBusy):
                other.begin(ACTOR, SESSION, "r2", "x")
            self.assertGreaterEqual(heartbeat.renewals, 2)
        self.assertFalse(heartbeat.lost)

    def test_heartbeat_reports_a_lost_lease(self) -> None:
        import time as real_time

        store = InMemoryItemStore()
        mine = ExecutionStore(store, lease_seconds=1, owner="a")
        execution = mine.begin(ACTOR, SESSION, "r1", "hello")
        mine.release_session(ACTOR, SESSION, "r1")
        ExecutionStore(store, lease_seconds=60, owner="b").begin(ACTOR, SESSION, "r2", "x")

        with mine.heartbeat(execution) as heartbeat:
            real_time.sleep(0.6)
        self.assertTrue(heartbeat.lost)


class LedgerTests(unittest.TestCase):
    def test_side_effect_runs_once_per_request_and_input(self) -> None:
        store = InMemoryItemStore()
        calls = []

        @idempotent
        def create(name: str) -> str:
            calls.append(name)
            return f"created {name}"

        with use_audit_trail(AuditTrail(store, ACTOR, SESSION, "r1")):
            self.assertEqual(create("sam"), "created sam")
            self.assertEqual(create("sam"), "created sam")
            create("alex")
        self.assertEqual(calls, ["sam", "alex"])

        # A retry (new trail object, same request) replays; a new request runs again.
        with use_audit_trail(AuditTrail(store, ACTOR, SESSION, "r1", attempt=2)):
            self.assertEqual(create("sam"), "created sam")
        self.assertEqual(calls, ["sam", "alex"])
        with use_audit_trail(AuditTrail(store, ACTOR, SESSION, "r2")):
            create("sam")
        self.assertEqual(calls, ["sam", "alex", "sam"])

    def test_pending_marker_is_audited_as_possibly_executed(self) -> None:
        store = InMemoryItemStore()
        trail = AuditTrail(store, ACTOR, SESSION, "r1")

        @idempotent
        def create(name: str) -> str:
            raise RuntimeError("pod died")

        with use_audit_trail(trail):
            with self.assertRaises(RuntimeError):
                create("sam")
            with self.assertRaises(RuntimeError):
                create("sam")
        types = [r["type"] for r in load_records(store, ACTOR, SESSION, "r1")]
        self.assertIn("ledger_pending", types)

    def test_without_a_trail_the_function_just_runs(self) -> None:
        @idempotent
        def create(name: str) -> str:
            return name

        self.assertEqual(create("x"), "x")


class MemoryProvenanceTests(unittest.TestCase):
    def test_add_stamps_provenance_and_is_idempotent_per_request(self) -> None:
        writes = []

        class Storage:
            async def write(self, key, data, **kwargs):
                writes.append((key, data, kwargs))

        store = InMemoryItemStore()
        memory_store = memory.DynamoDBMemoryStore(Storage(), "user/actor-1")  # type: ignore[arg-type]
        with patch.object(memory, "embed_text", return_value=[0.0] * 1024):
            with use_audit_trail(AuditTrail(store, ACTOR, SESSION, "r1")):
                asyncio.run(memory_store.add("prefers a 30 year fixed loan"))
                asyncio.run(memory_store.add("prefers a 30 year fixed loan"))
        self.assertEqual(writes[0][0], writes[1][0])
        metadata = writes[0][2]["metadata"]
        self.assertEqual(metadata["request_id"], "r1")
        self.assertEqual(metadata["actor_id"], ACTOR)
        records = load_records(store, ACTOR, SESSION, "r1")
        self.assertEqual([r["type"] for r in records], ["memory_write", "memory_write"])

    def test_search_records_what_was_retrieved(self) -> None:
        result = SimpleNamespace(
            key="memories/abc", score=0.87, data=b"prefers fixed", metadata={"request_id": "old"}
        )

        class Storage:
            async def search(self, query):
                return [result]

        store = InMemoryItemStore()
        memory_store = memory.DynamoDBMemoryStore(Storage(), "user/actor-1")  # type: ignore[arg-type]
        with patch.object(memory, "embed_text", return_value=[0.0] * 1024):
            with use_audit_trail(AuditTrail(store, ACTOR, SESSION, "r1")):
                entries = asyncio.run(memory_store.search("loan preference"))
        self.assertEqual(entries[0].content, "prefers fixed")
        record = load_records(store, ACTOR, SESSION, "r1")[0]
        self.assertEqual(record["type"], "memory_read")
        self.assertEqual(record["data"]["results"][0]["key"], "memories/abc")
        self.assertEqual(json.loads(json.dumps(record["data"]))["query"], "loan preference")


if __name__ == "__main__":
    unittest.main()
