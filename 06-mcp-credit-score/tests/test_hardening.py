"""Regression tests for races and failure paths found in review."""

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("MEMORY_TABLE_NAME", "test-memory-table")
os.environ.setdefault("MORTGAGE_API_KEY", "test-api-key")
os.environ.setdefault(
    "CREDIT_SCORE_MCP_URL",
    "http://credit-score-mcp.credit-services.svc.cluster.local:8081/mcp",
)

from fastapi.testclient import TestClient  # noqa: E402
from strands.agent.state import AgentState  # noqa: E402
from strands.hooks import AfterToolCallEvent, BeforeInvocationEvent, BeforeToolCallEvent  # noqa: E402

import ledger  # noqa: E402
import mortgage_agent  # noqa: E402
import mortgage_api  # noqa: E402
from audit import (  # noqa: E402
    AuditHook,
    AuditTrail,
    build_explanation,
    load_records,
    use_audit_trail,
    verify_chain,
)
from execution import (  # noqa: E402
    CANCELLED,
    COMPLETED,
    INTERRUPTED,
    RUNNING,
    ExecutionStore,
    InvalidExecutionState,
    PendingApproval,
    SessionBusy,
)
from resilience import INFLIGHT_KEY, ResumeHook, reset_interrupt_state  # noqa: E402
from service import Orchestrator  # noqa: E402
from store import InMemoryItemStore  # noqa: E402

ACTOR, SESSION = "actor-1", "session-1"


class AuditRaceTests(unittest.TestCase):
    def test_lost_response_to_our_own_write_is_treated_as_success(self) -> None:
        class LossyStore(InMemoryItemStore):
            armed = True

            def put(self, pk, sk, attrs, **kwargs):
                written = super().put(pk, sk, attrs, **kwargs)
                if self.armed and sk.startswith("audit/") and written:
                    self.armed = False
                    return False  # botocore retried a PutItem whose first reply was lost
                return written

        store = LossyStore()
        trail = AuditTrail(store, ACTOR, SESSION, "r1")
        trail.record("a", "supervisor", {}, critical=True)
        trail.record("b", "supervisor", {}, critical=True)

        records = load_records(store, ACTOR, SESSION, "r1")
        self.assertEqual([r["seq"] for r in records], [0, 1])
        self.assertTrue(verify_chain(records))

    def test_second_writer_reloads_the_tail_instead_of_wedging(self) -> None:
        store = InMemoryItemStore()
        first = AuditTrail(store, ACTOR, SESSION, "r1")
        stale = AuditTrail(store, ACTOR, SESSION, "r1")  # took over; both start at seq 0
        first.record("a", "supervisor", {}, critical=True)

        stale.record("b", "supervisor", {}, critical=True)  # loses the race, reloads, retries

        records = load_records(store, ACTOR, SESSION, "r1")
        self.assertEqual([r["seq"] for r in records], [0, 1])
        self.assertTrue(verify_chain(records))

    def test_oversized_record_does_not_break_the_explanation(self) -> None:
        trail = AuditTrail(InMemoryItemStore(), ACTOR, SESSION, "r1")
        trail.record(
            "tool_call", "supervisor", {"tool": "x", **{f"blob{i}": "y" * 3900 for i in range(10)}}
        )
        records = load_records(trail.store, ACTOR, SESSION, "r1")
        self.assertTrue(records[0]["data"].get("truncated"))
        self.assertEqual(build_explanation(records, {"x"})["records"], 1)

    def test_side_effects_are_blocked_after_the_lease_is_lost(self) -> None:
        trail = AuditTrail(InMemoryItemStore(), ACTOR, SESSION, "r1")
        trail.lease_lost = True
        event = BeforeToolCallEvent(
            agent=MagicMock(),
            selected_tool=None,
            tool_use={"name": "create_loan_application", "toolUseId": "t", "input": {}},
            invocation_state={},
        )
        with use_audit_trail(trail):
            AuditHook("new_application")._before_tool_call(event)
        self.assertIn("lease was lost", str(event.cancel_tool))


class ExecutionHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryItemStore()
        self.now = 1000.0
        self.executions = ExecutionStore(
            self.store, lease_seconds=60, owner="pod-a", clock=lambda: self.now
        )

    def test_summary_hides_error_messages(self) -> None:
        execution = self.executions.begin(ACTOR, SESSION, "r1", "hi")
        self.executions.fail(execution, "ClientError: arn:aws:iam::123456789012:role/secret")
        stored = self.executions.get(ACTOR, SESSION, "r1")
        self.assertEqual(stored.summary()["error"], "ClientError")
        self.assertIn("arn:aws", stored.error)  # the full text stays in the record

    def test_crash_during_approval_leaves_a_recoverable_request(self) -> None:
        paused = self.executions.begin(ACTOR, SESSION, "r1", "apply")
        self.executions.interrupt(paused, [{"id": "i1", "name": "approve_x"}])
        self.executions.begin_approval(ACTOR, SESSION, "r1")  # pod dies here: RUNNING + interrupts
        self.now += 100

        pending = self.executions.begin(ACTOR, SESSION, "r1", "apply")  # client retries /invoke

        self.assertEqual((pending.mode, pending.status), ("pending_approval", INTERRUPTED))
        again = self.executions.begin_approval(ACTOR, SESSION, "r1")
        self.assertEqual(again.status, RUNNING)

    def test_pending_approval_check_releases_the_lease(self) -> None:
        paused = self.executions.begin(ACTOR, SESSION, "r1", "apply")
        self.executions.interrupt(paused, [{"id": "i1", "name": "approve_x"}])
        with self.assertRaises(PendingApproval):
            self.executions.begin(ACTOR, SESSION, "r2", "other")
        lock = self.store.get(f"user/{ACTOR}", f"lock/{SESSION}")
        self.assertEqual(lock["lease_expires_at"], 0)

    def test_abandon_unblocks_the_session(self) -> None:
        paused = self.executions.begin(ACTOR, SESSION, "r1", "apply")
        self.executions.interrupt(paused, [{"id": "i1", "name": "approve_x"}])

        cancelled = self.executions.abandon(ACTOR, SESSION, "r1")

        self.assertEqual((cancelled.status, cancelled.interrupts), (CANCELLED, []))
        self.executions.begin(ACTOR, SESSION, "r2", "next")
        with self.assertRaises(InvalidExecutionState):
            self.executions.abandon(ACTOR, SESSION, "r1")

    def test_release_never_frees_another_owners_lease(self) -> None:
        self.executions.begin(ACTOR, SESSION, "r1", "hi")
        other = ExecutionStore(self.store, lease_seconds=60, owner="pod-b", clock=lambda: self.now)
        other.release_session(ACTOR, SESSION, "r1")
        with self.assertRaises(SessionBusy):
            other.begin(ACTOR, SESSION, "r2", "x")

    def test_heartbeat_survives_a_transient_error_but_reports_a_refused_renewal(self) -> None:
        execution = self.executions.begin(ACTOR, SESSION, "r1", "hi")
        calls = {"n": 0}
        real_renew = self.executions.renew_lease

        def flaky(*args):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("throttled")
            return real_renew(*args)

        lost = []
        with patch.object(self.executions, "renew_lease", side_effect=flaky):
            heartbeat = self.executions.heartbeat(execution, on_lost=lambda: lost.append(1))
            heartbeat._interval = 0.05
            with heartbeat:
                time.sleep(0.4)
        self.assertFalse(heartbeat.lost)
        self.assertGreaterEqual(heartbeat.renewals, 1)

        with patch.object(self.executions, "renew_lease", return_value=False):
            heartbeat = self.executions.heartbeat(execution, on_lost=lambda: lost.append(1))
            heartbeat._interval = 0.05
            with heartbeat:
                time.sleep(0.3)
        self.assertTrue(heartbeat.lost)
        self.assertEqual(lost, [1])


class LedgerHardeningTests(unittest.TestCase):
    def test_parallel_identical_calls_run_the_effect_once(self) -> None:
        store = InMemoryItemStore()
        runs = []

        def slow_effect():
            runs.append(1)
            time.sleep(0.3)
            return "created"

        results = []

        def call():
            with use_audit_trail(AuditTrail(store, ACTOR, SESSION, "r1")):
                results.append(ledger.run_once("create", {"n": 1}, slow_effect))

        threads = [threading.Thread(target=call) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(runs), 1)
        self.assertEqual(results, ["created", "created"])

    def test_same_request_id_in_another_session_is_independent(self) -> None:
        store = InMemoryItemStore()
        runs = []

        def effect():
            runs.append(1)
            return "ok"

        with use_audit_trail(AuditTrail(store, ACTOR, "session-a", "req")):
            ledger.run_once("create", {"n": 1}, effect)
        with use_audit_trail(AuditTrail(store, ACTOR, "session-b", "req")):
            ledger.run_once("create", {"n": 1}, effect)
        self.assertEqual(len(runs), 2)

    def test_failed_ledger_write_does_not_lose_the_result(self) -> None:
        store = InMemoryItemStore()
        with use_audit_trail(AuditTrail(store, ACTOR, SESSION, "r1")):
            with patch.object(ledger.Ledger, "mark_done", side_effect=RuntimeError("ddb")), patch(
                "ledger.time.sleep"
            ):
                self.assertEqual(ledger.run_once("create", {}, lambda: "made"), "made")


class RollbackAndFailFastTests(unittest.TestCase):
    def _agent(self, messages, removed=0):
        return SimpleNamespace(
            messages=list(messages),
            state=AgentState(),
            system_prompt=None,
            conversation_manager=SimpleNamespace(removed_message_count=removed),
        )

    def test_rollback_shifts_the_cut_point_by_what_was_trimmed(self) -> None:
        hook = ResumeHook("supervisor", "prompt")
        agent = self._agent([{"role": "user"}] * 4)
        hook._before_invocation(
            BeforeInvocationEvent(agent=agent, messages=[{"role": "user", "content": []}])
        )
        self.assertEqual(agent.state.get(INFLIGHT_KEY)["start"], 4)

        # The turn added 4 messages, then the sliding window dropped 3 old ones.
        agent.messages = [{"role": "old"}] + [{"role": "turn"}] * 4
        agent.conversation_manager.removed_message_count = 3
        hook._before_invocation(
            BeforeInvocationEvent(agent=agent, messages=[{"role": "user", "content": []}])
        )

        self.assertEqual(agent.messages, [{"role": "old"}])

    def test_rollback_of_a_fully_trimmed_history_removes_only_the_turn(self) -> None:
        hook = ResumeHook("supervisor", "prompt")
        agent = self._agent([{"role": "user"}] * 2)
        hook._before_invocation(
            BeforeInvocationEvent(agent=agent, messages=[{"role": "user", "content": []}])
        )
        agent.messages = [{"role": "turn"}] * 3
        agent.conversation_manager.removed_message_count = 5
        hook._before_invocation(
            BeforeInvocationEvent(agent=agent, messages=[{"role": "user", "content": []}])
        )
        self.assertEqual(agent.messages, [])

    def test_parallel_call_to_one_specialist_does_not_fail_the_request(self) -> None:
        trail = AuditTrail(InMemoryItemStore(), ACTOR, SESSION, "r1")
        event = AfterToolCallEvent(
            agent=MagicMock(),
            selected_tool=SimpleNamespace(tool_type="agent"),
            tool_use={"name": "existing_mortgage_specialist", "toolUseId": "t", "input": {}},
            invocation_state={},
            result={
                "toolUseId": "t",
                "status": "error",
                "content": [{"text": "Agent 'existing_mortgage_specialist' is already processing a request"}],
            },
        )
        with use_audit_trail(trail):
            mortgage_agent.FailFastHook()._after_tool_call(event)
        self.assertIsNone(trail.abort_reason)

    def test_reset_interrupt_state_clears_and_persists(self) -> None:
        state = SimpleNamespace(activated=True, deactivate=MagicMock())
        session_manager = MagicMock()
        agent = SimpleNamespace(_interrupt_state=state, _session_manager=session_manager)
        idle = SimpleNamespace(
            _interrupt_state=SimpleNamespace(activated=False, deactivate=MagicMock()),
            _session_manager=MagicMock(),
        )

        reset_interrupt_state([agent, idle])

        state.deactivate.assert_called_once()
        session_manager.sync_agent.assert_called_once_with(agent)
        idle._interrupt_state.deactivate.assert_not_called()


class OrchestratorHardeningTests(unittest.TestCase):
    def test_duplicate_decisions_and_cancel(self) -> None:
        store = InMemoryItemStore()
        reset = MagicMock()
        orchestrator = Orchestrator(
            store, MagicMock(), {"existing_mortgage_specialist"}, reset_agents=reset
        )
        paused = orchestrator.executions.begin(ACTOR, SESSION, "r1", "apply")
        orchestrator.executions.interrupt(paused, [{"id": "i1", "name": "approve_x"}])

        with self.assertRaises(InvalidExecutionState):
            orchestrator.decide(
                ACTOR,
                SESSION,
                "r1",
                [
                    {"interrupt_id": "i1", "approved": True},
                    {"interrupt_id": "i1", "approved": False},
                ],
            )

        cancelled = orchestrator.cancel(ACTOR, SESSION, "r1")
        self.assertEqual(cancelled.status, CANCELLED)
        reset.assert_called_once_with(ACTOR, SESSION)
        types = [r["type"] for r in load_records(store, ACTOR, SESSION, "r1")]
        self.assertEqual(types, ["execution_cancelled"])

    def test_completion_write_is_retried(self) -> None:
        store = InMemoryItemStore()
        result = SimpleNamespace(stop_reason="end_turn", interrupts=None, __str__=lambda self: "done")
        result = MagicMock(stop_reason="end_turn", interrupts=None)
        result.__str__.return_value = "the answer"
        orchestrator = Orchestrator(store, lambda *args: result, set())
        real_complete = orchestrator.executions.complete
        failures = {"n": 0}

        def flaky_complete(execution, response):
            failures["n"] += 1
            if failures["n"] < 3:
                raise RuntimeError("throttled")
            return real_complete(execution, response)

        with patch.object(orchestrator.executions, "complete", side_effect=flaky_complete), patch(
            "service.time.sleep"
        ):
            outcome = orchestrator.invoke(ACTOR, SESSION, "r1", "hello")

        self.assertEqual(outcome.response, "the answer")
        self.assertEqual(orchestrator.executions.get(ACTOR, SESSION, "r1").status, COMPLETED)


class ApiHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(mortgage_api.app)
        self.headers = {"Authorization": "Bearer test-api-key"}
        self.body = {"prompt": "hi", "actor_id": "actor-1", "session_id": "session-1"}

    def test_non_ascii_authorization_is_401_not_500(self) -> None:
        response = self.client.post(
            "/invoke", json=self.body, headers={"Authorization": "Bearer ключ".encode("utf-8")}
        )
        self.assertEqual(response.status_code, 401)

    def test_agent_slots_are_capped_with_429(self) -> None:
        slots = threading.BoundedSemaphore(1)
        slots.acquire()
        with patch.object(mortgage_api, "AGENT_SLOTS", slots):
            response = self.client.post("/invoke", json=self.body, headers=self.headers)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["retry-after"], "5")

    def test_path_identifiers_are_validated(self) -> None:
        long_id = "x" * 200
        params = {"actor_id": "actor-1", "session_id": "session-1"}
        self.assertEqual(
            self.client.get(f"/executions/{long_id}", params=params, headers=self.headers).status_code,
            422,
        )
        self.assertEqual(
            self.client.get(
                f"/sessions/{long_id}/executions", params={"actor_id": "actor-1"}, headers=self.headers
            ).status_code,
            422,
        )

    def test_cancel_endpoint(self) -> None:
        orchestrator = MagicMock()
        orchestrator.cancel.return_value = SimpleNamespace(summary=lambda: {"status": CANCELLED})
        with patch("mortgage_api.get_orchestrator", return_value=orchestrator):
            unauthorized = self.client.post(
                "/executions/r1/cancel", json={"actor_id": "actor-1", "session_id": "session-1"}
            )
            ok = self.client.post(
                "/executions/r1/cancel",
                json={"actor_id": "actor-1", "session_id": "session-1"},
                headers=self.headers,
            )
        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(ok.json()["status"], CANCELLED)
        orchestrator.cancel.assert_called_once_with("actor-1", "session-1", "r1")


if __name__ == "__main__":
    unittest.main()
