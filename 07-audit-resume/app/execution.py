"""Execution records, per-session lease, and idempotent request handling.

One ``exec/<session>/<request_id>`` item tracks each request's lifecycle:
RUNNING -> COMPLETED | FAILED | INTERRUPTED. A separate ``lock/<session>`` item is a
lease so two pods cannot run turns against the same session at once. Both use
conditional writes (see ``store.ItemStore``) and need no extra IAM permissions.

A client retries a failed request by re-sending the same ``request_id``; the
record decides whether that is a replay (COMPLETED), a resume (FAILED or an
expired lease), a conflict (live lease), or a pending approval (INTERRUPTED).
"""

import hashlib
import json
import logging
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from store import Attrs, ItemStore

logger = logging.getLogger("execution")

LEASE_SECONDS = int(os.environ.get("LEASE_SECONDS", "180"))

RUNNING = "RUNNING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
INTERRUPTED = "INTERRUPTED"


class ExecutionConflict(Exception):
    status_code = 409


class SessionBusy(ExecutionConflict):
    """Another request holds the session lease."""


class PendingApproval(ExecutionConflict):
    """Another request in this session is paused for approval and must be answered first."""


class RequestMismatch(ExecutionConflict):
    """The request_id was already used for a different prompt."""


class InvalidExecutionState(ExecutionConflict):
    """The execution is not in a state that allows the requested action."""


class ExecutionNotFound(Exception):
    status_code = 404


def prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.strip().encode("utf-8")).hexdigest()


@dataclass
class Execution:
    actor_id: str
    session_id: str
    request_id: str
    attempt: int
    status: str
    prompt: str
    prompt_hash: str
    mode: str = "new"  # new | resume | approval | replay | pending_approval
    response: str | None = None
    error: str | None = None
    interrupts: list[dict[str, Any]] = field(default_factory=list)
    version: int = 0
    trace_id: str | None = None

    def to_attrs(self, owner: str, lease_expires_at: int) -> Attrs:
        return {
            "status": self.status,
            "attempt": self.attempt,
            "prompt": self.prompt,
            "prompt_hash": self.prompt_hash,
            "response": self.response or "",
            "error": self.error or "",
            "interrupts": json.dumps(self.interrupts),
            "owner": owner,
            "lease_expires_at": lease_expires_at,
            "version": self.version,
            "updated_at": int(time.time()),
            "trace_id": self.trace_id or "",
        }

    @classmethod
    def from_attrs(cls, actor_id: str, session_id: str, request_id: str, item: Attrs) -> "Execution":
        return cls(
            actor_id=actor_id,
            session_id=session_id,
            request_id=request_id,
            attempt=int(item["attempt"]),
            status=str(item["status"]),
            prompt=str(item.get("prompt", "")),
            prompt_hash=str(item.get("prompt_hash", "")),
            response=str(item.get("response") or "") or None,
            error=str(item.get("error") or "") or None,
            interrupts=json.loads(str(item.get("interrupts") or "[]")),
            version=int(item.get("version", 0)),
            trace_id=str(item.get("trace_id") or "") or None,
        )

    def summary(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "status": self.status,
            "attempt": self.attempt,
            "error": self.error,
            "interrupts": self.interrupts,
            "trace_id": self.trace_id,
        }


class ExecutionStore:
    def __init__(
        self,
        store: ItemStore,
        *,
        lease_seconds: int = LEASE_SECONDS,
        owner: str | None = None,
        clock: Any = time.time,
    ) -> None:
        self.store = store
        self.lease_seconds = lease_seconds
        # Per-process owner: a restarted container keeps its pod name but must not
        # inherit the lease its crashed predecessor held.
        host = os.environ.get("HOSTNAME") or socket.gethostname()
        self.owner = owner or f"{host}:{uuid.uuid4().hex[:8]}"
        self._clock = clock

    # -- keys ---------------------------------------------------------------
    @staticmethod
    def _pk(actor_id: str) -> str:
        return f"user/{actor_id}"

    @staticmethod
    def _exec_sk(session_id: str, request_id: str) -> str:
        return f"exec/{session_id}/{request_id}"

    @staticmethod
    def _lock_sk(session_id: str) -> str:
        return f"lock/{session_id}"

    def _now(self) -> int:
        return int(self._clock())

    # -- reads --------------------------------------------------------------
    def get(self, actor_id: str, session_id: str, request_id: str) -> Execution | None:
        item = self.store.get(self._pk(actor_id), self._exec_sk(session_id, request_id))
        if item is None:
            return None
        return Execution.from_attrs(actor_id, session_id, request_id, item)

    def list_session(self, actor_id: str, session_id: str) -> list[Execution]:
        prefix = f"exec/{session_id}/"
        return [
            Execution.from_attrs(actor_id, session_id, str(item["sk"])[len(prefix):], item)
            for item in self.store.query(self._pk(actor_id), prefix)
        ]

    # -- lease --------------------------------------------------------------
    def _acquire_session(self, actor_id: str, session_id: str, request_id: str) -> None:
        pk, sk = self._pk(actor_id), self._lock_sk(session_id)
        now = self._now()
        attrs: Attrs = {
            "owner": self.owner,
            "request_id": request_id,
            "lease_expires_at": now + self.lease_seconds,
            "version": 1,
        }
        current = self.store.get(pk, sk)
        if current is None:
            acquired = self.store.put(pk, sk, attrs, if_absent=True)
        else:
            if int(current["lease_expires_at"]) > now:
                # Also covers a double-submit of the same request while it is running.
                raise SessionBusy(
                    f"session {session_id} is busy with request {current['request_id']}"
                )
            attrs["version"] = int(current["version"]) + 1
            acquired = self.store.put(pk, sk, attrs, expect={"version": current["version"]})
        if not acquired:
            raise SessionBusy(f"session {session_id} is busy")

    def release_session(self, actor_id: str, session_id: str, request_id: str) -> None:
        pk, sk = self._pk(actor_id), self._lock_sk(session_id)
        current = self.store.get(pk, sk)
        if current is None or current["request_id"] != request_id:
            return
        self.store.put(
            pk,
            sk,
            {**current, "lease_expires_at": 0, "version": int(current["version"]) + 1},
            expect={"version": current["version"]},
        )

    def renew_lease(self, actor_id: str, session_id: str, request_id: str) -> bool:
        """Extend the session lease for a running request. False when it was lost."""
        pk, sk = self._pk(actor_id), self._lock_sk(session_id)
        current = self.store.get(pk, sk)
        if current is None or current["request_id"] != request_id:
            return False
        if current["owner"] != self.owner or int(current["lease_expires_at"]) == 0:
            return False  # not ours, or already released
        return self.store.put(
            pk,
            sk,
            {
                **current,
                "lease_expires_at": self._now() + self.lease_seconds,
                "version": int(current["version"]) + 1,
            },
            expect={"version": current["version"]},
        )

    def heartbeat(self, execution: "Execution") -> "LeaseHeartbeat":
        return LeaseHeartbeat(self, execution)

    # -- lifecycle ----------------------------------------------------------
    def begin(
        self,
        actor_id: str,
        session_id: str,
        request_id: str,
        prompt: str,
        *,
        trace_id: str | None = None,
    ) -> Execution:
        """Start, resume, or replay a request. Holds the session lease unless replaying."""
        digest = prompt_hash(prompt)
        existing = self.get(actor_id, session_id, request_id)
        if existing is not None and existing.prompt_hash != digest:
            raise RequestMismatch(f"request_id {request_id} was used with a different prompt")
        if existing is not None and existing.status == COMPLETED:
            existing.mode = "replay"
            return existing
        if existing is not None and existing.status == INTERRUPTED:
            existing.mode = "pending_approval"
            return existing

        for other in self.list_session(actor_id, session_id):
            # The paused agents are in an interrupt state and cannot take a new prompt.
            if other.status == INTERRUPTED and other.request_id != request_id:
                raise PendingApproval(
                    f"request {other.request_id} is awaiting approval; answer it first"
                )

        self._acquire_session(actor_id, session_id, request_id)
        try:
            if existing is None:
                execution = Execution(
                    actor_id, session_id, request_id, 1, RUNNING, prompt.strip(), digest,
                    trace_id=trace_id,
                )
                created = self.store.put(
                    self._pk(actor_id),
                    self._exec_sk(session_id, request_id),
                    execution.to_attrs(self.owner, self._now() + self.lease_seconds),
                    if_absent=True,
                )
                if not created:
                    raise SessionBusy(f"request {request_id} is already running")
                return execution

            execution = existing
            execution.attempt += 1
            execution.status = RUNNING
            execution.error = None
            execution.mode = "resume"
            execution.trace_id = trace_id or execution.trace_id
            self._save(execution, expect_version=existing.version)
            return execution
        except Exception:
            self.release_session(actor_id, session_id, request_id)
            raise

    def begin_approval(self, actor_id: str, session_id: str, request_id: str) -> Execution:
        """Take the lease to continue an INTERRUPTED request after reviewer decisions."""
        existing = self.get(actor_id, session_id, request_id)
        if existing is None:
            raise ExecutionNotFound(f"no execution {request_id}")
        if existing.status != INTERRUPTED:
            raise InvalidExecutionState(
                f"execution {request_id} is {existing.status}, not awaiting approval"
            )
        self._acquire_session(actor_id, session_id, request_id)
        try:
            existing.attempt += 1
            existing.status = RUNNING
            existing.mode = "approval"
            self._save(existing, expect_version=existing.version)
            return existing
        except Exception:
            self.release_session(actor_id, session_id, request_id)
            raise

    def _save(self, execution: Execution, *, expect_version: int) -> None:
        execution.version = expect_version + 1
        saved = self.store.put(
            self._pk(execution.actor_id),
            self._exec_sk(execution.session_id, execution.request_id),
            execution.to_attrs(self.owner, self._now() + self.lease_seconds),
            expect={"version": expect_version},
        )
        if not saved:
            raise SessionBusy(f"request {execution.request_id} was modified concurrently")

    def _finish(self, execution: Execution, status: str) -> Execution:
        execution.status = status
        self._save(execution, expect_version=execution.version)
        self.release_session(execution.actor_id, execution.session_id, execution.request_id)
        return execution

    def complete(self, execution: Execution, response: str) -> Execution:
        execution.response = response
        execution.error = None
        execution.interrupts = []
        return self._finish(execution, COMPLETED)

    def fail(self, execution: Execution, error: str) -> Execution:
        execution.error = error[:1000]
        return self._finish(execution, FAILED)

    def interrupt(
        self,
        execution: Execution,
        interrupts: list[dict[str, Any]],
        error: str | None = None,
    ) -> Execution:
        execution.interrupts = interrupts
        execution.error = error[:1000] if error else None
        return self._finish(execution, INTERRUPTED)


class LeaseHeartbeat:
    """Keep the session lease alive while a request runs.

    Renews every third of the lease so a request that outlives ``LEASE_SECONDS`` is not
    taken over by a retry. If a renewal fails the lease was lost (another pod took
    over after a long stall); ``lost`` is set and the loss is audited by the caller.
    """

    def __init__(self, executions: ExecutionStore, execution: Execution, interval: float | None = None) -> None:
        self._executions = executions
        self._execution = execution
        self._interval = interval or max(executions.lease_seconds / 3, 0.05)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="lease-heartbeat", daemon=True)
        self.lost = False
        self.renewals = 0

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                renewed = self._executions.renew_lease(
                    self._execution.actor_id,
                    self._execution.session_id,
                    self._execution.request_id,
                )
            except Exception:
                logger.exception("Lease renewal failed for %s", self._execution.request_id)
                renewed = False
            if not renewed:
                self.lost = True
                logger.error("Lost the session lease for %s", self._execution.request_id)
                return
            self.renewals += 1

    def __enter__(self) -> "LeaseHeartbeat":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
