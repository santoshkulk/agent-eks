"""Tool-result ledger: exactly-once effects for side-effecting tools across retries.

Keyed by ``(request_id, tool, canonical input hash)``. A retried request that
re-issues a completed side-effecting call gets the recorded result instead of
running the tool again. A PENDING marker with no result means an earlier
attempt died mid-call, so the effect may or may not have happened; the call is
re-run and audited with ``possibly_executed`` (real systems should also pass an
idempotency key downstream).
"""

import functools
import hashlib
import json
import logging
from collections.abc import Callable
from typing import Any, TypeVar

from audit import AuditTrail, canonical_json, current_trail, record_event
from store import ItemStore

logger = logging.getLogger("ledger")

F = TypeVar("F", bound=Callable[..., Any])


def input_hash(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:32]


class Ledger:
    def __init__(self, store: ItemStore, actor_id: str, request_id: str) -> None:
        self.store = store
        self._pk = f"user/{actor_id}"
        self.request_id = request_id

    def _sk(self, tool: str, payload: Any) -> str:
        return f"ledger/{self.request_id}/{tool}/{input_hash(payload)}"

    def lookup(self, tool: str, payload: Any) -> tuple[str, Any]:
        """Return ``("DONE", result)``, ``("PENDING", None)`` or ``("NEW", None)``."""
        item = self.store.get(self._pk, self._sk(tool, payload))
        if item is None:
            return "NEW", None
        if item.get("status") == "DONE":
            return "DONE", json.loads(str(item["result"]))
        return "PENDING", None

    def mark_pending(self, tool: str, payload: Any) -> None:
        self.store.put(
            self._pk, self._sk(tool, payload), {"status": "PENDING", "result": "null"}
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
    return Ledger(trail.store, trail.actor_id, trail.request_id)


def run_once(tool: str, payload: Any, effect: Callable[[], Any], agent_id: str = "tool") -> Any:
    """Run ``effect`` at most once per request for identical input; replay its result."""
    ledger = ledger_for(current_trail())
    if ledger is None:
        return effect()
    status, recorded = ledger.lookup(tool, payload)
    if status == "DONE":
        record_event("ledger_replay", agent_id, {"tool": tool, "input": payload})
        return recorded
    if status == "PENDING":
        record_event(
            "ledger_pending", agent_id, {"tool": tool, "possibly_executed": True, "input": payload}
        )
    ledger.mark_pending(tool, payload)
    result = effect()
    ledger.mark_done(tool, payload, result)
    return result


def idempotent(fn: F) -> F:
    """Decorate a plain function (below ``@tool``) so retries replay its result."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        bound = functools.partial(fn, *args, **kwargs)
        payload = {"args": list(args), "kwargs": kwargs}
        return run_once(fn.__name__, payload, bound, agent_id="ledger")

    return wrapper  # type: ignore[return-value]
