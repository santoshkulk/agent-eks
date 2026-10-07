"""Tool-result ledger: exactly-once effects for side-effecting tools across retries.

Keyed by ``(session_id, request_id, tool, canonical input hash)``. A retried request
that re-issues a completed side-effecting call gets the recorded result instead of
running the tool again. A PENDING marker with no result means an earlier attempt died
mid-call, so the effect may or may not have happened; the call is re-run and audited
with ``possibly_executed`` (real systems should also pass an idempotency key downstream).

The PENDING claim is a conditional write, so two parallel identical calls in one turn
cannot both start the effect: the loser waits briefly for the winner's result.
"""

import functools
import hashlib
import json
import logging
import time
from collections.abc import Callable
from typing import Any, TypeVar

from audit import AuditTrail, canonical_json, current_trail, record_event
from store import ItemStore

logger = logging.getLogger("ledger")

F = TypeVar("F", bound=Callable[..., Any])

CLAIM_WAIT_SECONDS = 30.0
CLAIM_POLL_SECONDS = 0.2


def input_hash(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:32]


class Ledger:
    def __init__(self, store: ItemStore, actor_id: str, session_id: str, request_id: str) -> None:
        self.store = store
        self._pk = f"user/{actor_id}"
        self.session_id = session_id
        self.request_id = request_id

    def _sk(self, tool: str, payload: Any) -> str:
        return f"ledger/{self.session_id}/{self.request_id}/{tool}/{input_hash(payload)}"

    def lookup(self, tool: str, payload: Any) -> tuple[str, Any]:
        """Return ``("DONE", result)``, ``("PENDING", claiming attempt)`` or ``("NEW", None)``."""
        item = self.store.get(self._pk, self._sk(tool, payload))
        if item is None:
            return "NEW", None
        if item.get("status") == "DONE":
            return "DONE", json.loads(str(item["result"]))
        return "PENDING", int(item.get("attempt", 0))

    def claim(self, tool: str, payload: Any, attempt: int) -> bool:
        """Record PENDING if nobody has. False means another caller got there first."""
        return self.store.put(
            self._pk,
            self._sk(tool, payload),
            {"status": "PENDING", "result": "null", "attempt": attempt},
            if_absent=True,
        )

    def mark_pending(self, tool: str, payload: Any, attempt: int) -> None:
        """Unconditionally (re)record PENDING; used when retaking a dead attempt's claim."""
        self.store.put(
            self._pk,
            self._sk(tool, payload),
            {"status": "PENDING", "result": "null", "attempt": attempt},
        )

    def mark_done(self, tool: str, payload: Any, result: Any) -> None:
        self.store.put(
            self._pk,
            self._sk(tool, payload),
            {"status": "DONE", "result": canonical_json(result)},
        )


def ledger_for(trail: AuditTrail | None) -> Ledger | None:
    if trail is None:
        return None
    return Ledger(trail.store, trail.actor_id, trail.session_id, trail.request_id)


def _wait_for_result(ledger: Ledger, tool: str, payload: Any) -> tuple[str, Any]:
    deadline = time.monotonic() + CLAIM_WAIT_SECONDS
    while True:
        status, recorded = ledger.lookup(tool, payload)
        if status != "PENDING" or time.monotonic() >= deadline:
            return status, recorded
        time.sleep(CLAIM_POLL_SECONDS)


def run_once(tool: str, payload: Any, effect: Callable[[], Any], agent_id: str = "tool") -> Any:
    """Run ``effect`` at most once per request for identical input; replay its result."""
    trail = current_trail()
    ledger = ledger_for(trail)
    if trail is None or ledger is None:
        return effect()
    status, recorded = ledger.lookup(tool, payload)
    if status == "NEW" and not ledger.claim(tool, payload, trail.attempt):
        # A parallel identical call claimed it between our lookup and claim.
        status, recorded = ledger.lookup(tool, payload)
    if status == "PENDING" and recorded == trail.attempt:
        # Claimed by a parallel call in this very attempt: wait for its result.
        status, recorded = _wait_for_result(ledger, tool, payload)
    if status == "DONE":
        record_event("ledger_replay", agent_id, {"tool": tool, "input": payload})
        return recorded
    if status == "PENDING":
        # Claimed by an earlier attempt that died mid-call: the effect may have happened.
        record_event(
            "ledger_pending", agent_id, {"tool": tool, "possibly_executed": True, "input": payload}
        )
        ledger.mark_pending(tool, payload, trail.attempt)
    try:
        result = effect()
    except Exception:
        # Release the in-flight claim (attempt 0 = nobody is running it) so a retry in
        # this attempt does not wait for a result that will never come.
        try:
            ledger.mark_pending(tool, payload, 0)
        except Exception:
            logger.exception("Could not release the ledger claim for %s", tool)
        raise
    for attempt in range(3):
        try:
            ledger.mark_done(tool, payload, result)
            break
        except Exception:
            logger.exception("Could not record the %s result in the ledger (attempt %d)", tool, attempt + 1)
            time.sleep(0.5 * (attempt + 1))
    return result


def idempotent(fn: F) -> F:
    """Decorate a plain function (below ``@tool``) so retries replay its result."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        bound = functools.partial(fn, *args, **kwargs)
        payload = {"args": list(args), "kwargs": kwargs}
        return run_once(fn.__name__, payload, bound, agent_id="ledger")

    return wrapper  # type: ignore[return-value]
