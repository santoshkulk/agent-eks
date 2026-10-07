"""Durable, hash-chained audit trail for supervisor and specialist agents.

``AuditHook`` is a Strands ``HookProvider`` attached to the supervisor and to every
specialist. It records invocations, model decisions, and tool calls (including
specialist agents-as-tools) to DynamoDB through ``ItemStore``. Records are
append-only per request, and each carries ``prev_hash``/``hash`` so tampering or
gaps are detectable (``verify_chain``).

``as_tool`` does not forward ``invocation_state`` to the specialist, so request
identity travels in a ``ContextVar`` (copied into Strands' worker threads).
"""

import contextlib
import contextvars
import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

from strands.hooks import (
    AfterInvocationEvent,
    AfterModelCallEvent,
    AfterToolCallEvent,
    BeforeInvocationEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)

from store import ItemStore

logger = logging.getLogger("audit")

MAX_FIELD_CHARS = 4000
MAX_RECORD_CHARS = 32_000
REDACTED = "[REDACTED]"
GENESIS_HASH = "0" * 64

# Tools that change state or reach an external system. An audit write failure
# blocks them (fail closed); other events degrade to a logged, dropped record.
SIDE_EFFECT_TOOLS = frozenset(
    name.strip()
    for name in os.environ.get(
        "AUDIT_CRITICAL_TOOLS",
        "create_customer_id,create_loan_application,add_memory",
    ).split(",")
    if name.strip()
)
REDACT_KEYS = frozenset(
    name.strip().lower()
    for name in os.environ.get(
        "AUDIT_REDACT_KEYS", "password,token,authorization,api_key,ssn"
    ).split(",")
    if name.strip()
)


class AuditWriteError(RuntimeError):
    """An audit record that must exist could not be written."""


def audit_prefix(session_id: str, request_id: str) -> str:
    return f"audit/{session_id}/{request_id}/"


def _truncate(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_FIELD_CHARS:
        return value[:MAX_FIELD_CHARS] + f"...[truncated {len(value) - MAX_FIELD_CHARS} chars]"
    return value


def redact(value: Any, keys: frozenset[str] = REDACT_KEYS) -> Any:
    """Return a JSON-safe copy with sensitive keys masked and long strings truncated."""
    if isinstance(value, dict):
        return {
            str(key): REDACTED if str(key).lower() in keys else redact(item, keys)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, keys) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return _truncate(value)
    return _truncate(str(value))


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def compute_hash(prev_hash: str, record: dict[str, Any]) -> str:
    body = {key: item for key, item in record.items() if key not in ("hash",)}
    body["prev_hash"] = prev_hash
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def verify_chain(records: list[dict[str, Any]]) -> bool:
    """Check ordering, sequence continuity, and the hash chain of parsed records."""
    prev_hash = GENESIS_HASH
    for expected_seq, record in enumerate(records):
        if record.get("seq") != expected_seq or record.get("prev_hash") != prev_hash:
            return False
        if compute_hash(prev_hash, record) != record.get("hash"):
            return False
        prev_hash = record["hash"]
    return True


class AuditTrail:
    """Append-only, hash-chained records for one request (across attempts)."""

    def __init__(
        self,
        store: ItemStore,
        actor_id: str,
        session_id: str,
        request_id: str,
        attempt: int = 1,
        trace_id: str | None = None,
    ) -> None:
        self.store = store
        self.actor_id = actor_id
        self.session_id = session_id
        self.request_id = request_id
        self.attempt = attempt
        self.trace_id = trace_id
        self._pk = f"user/{actor_id}"
        self._lock = threading.Lock()
        self.dropped = 0
        self.abort_reason: str | None = None
        self.lease_lost = False
        self._reload_tail()

    @property
    def count(self) -> int:
        """Number of records written so far (the next sequence number)."""
        return self._seq

    @property
    def head(self) -> str:
        """Hash of the last record written."""
        return self._prev_hash

    def _reload_tail(self) -> None:
        existing = load_records(self.store, self.actor_id, self.session_id, self.request_id)
        self._seq = len(existing)
        self._prev_hash = existing[-1]["hash"] if existing else GENESIS_HASH

    def record(
        self,
        record_type: str,
        agent_id: str,
        data: dict[str, Any],
        *,
        critical: bool = False,
    ) -> None:
        with self._lock:
            try:
                self._append(record_type, agent_id, data)
            except Exception as error:
                if critical:
                    raise AuditWriteError(
                        f"audit write failed for {record_type}: {error}"
                    ) from error
                self.dropped += 1
                logger.exception(
                    "Dropped non-critical audit record %s for request %s",
                    record_type,
                    self.request_id,
                )

    def _append(self, record_type: str, agent_id: str, data: dict[str, Any]) -> None:
        # Two attempts: the second reloads the tail after losing a sequence race (for
        # example a worker that took over the session after a lease loss).
        for _ in range(2):
            record = self._build(record_type, agent_id, data)
            payload = canonical_json(record)
            key = f"{audit_prefix(self.session_id, self.request_id)}{self._seq:06d}"
            written = self.store.put(
                self._pk,
                key,
                {
                    "record": payload,
                    "hash": record["hash"],
                    "type": record_type,
                    "seq": self._seq,
                },
                if_absent=True,
            )
            if not written:
                # A retried PutItem whose first response was lost reports "exists" for
                # our own record; treat an identical hash as success.
                stored = self.store.get(self._pk, key)
                written = bool(stored and stored.get("hash") == record["hash"])
            if written:
                self._seq += 1
                self._prev_hash = record["hash"]
                return
            self._reload_tail()
        raise AuditWriteError(f"audit sequence {self._seq} already exists")

    def _build(self, record_type: str, agent_id: str, data: dict[str, Any]) -> dict[str, Any]:
        record: dict[str, Any] = {
            "seq": self._seq,
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": record_type,
            "agent_id": agent_id,
            "attempt": self.attempt,
            "actor_id": self.actor_id,
            "session_id": self.session_id,
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "data": redact(data),
        }
        record["prev_hash"] = self._prev_hash
        record["hash"] = compute_hash(self._prev_hash, record)
        payload = canonical_json(record)
        if len(payload) > MAX_RECORD_CHARS:
            record["data"] = {
                "truncated": True,
                "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                "preview": payload[:1000],
            }
            record["hash"] = compute_hash(self._prev_hash, record)
        return record


def load_records(
    store: ItemStore, actor_id: str, session_id: str, request_id: str
) -> list[dict[str, Any]]:
    items = store.query(f"user/{actor_id}", audit_prefix(session_id, request_id))
    records = []
    for index, item in enumerate(items):
        try:
            records.append(json.loads(str(item["record"])))
        except (ValueError, KeyError):
            # Keep the position so verify_chain fails instead of the read raising.
            records.append({"seq": None, "type": "corrupt", "agent_id": "?", "attempt": 0, "data": {"unreadable": True, "sk": item.get("sk")}})
    return records


_current_trail: contextvars.ContextVar[AuditTrail | None] = contextvars.ContextVar(
    "audit_trail", default=None
)


def current_trail() -> AuditTrail | None:
    return _current_trail.get()


@contextlib.contextmanager
def use_audit_trail(trail: AuditTrail) -> Iterator[AuditTrail]:
    token = _current_trail.set(trail)
    try:
        yield trail
    finally:
        _current_trail.reset(token)


def _blocks(message: Any) -> list[dict[str, Any]]:
    content = (message or {}).get("content") or []
    return [block for block in content if isinstance(block, dict)]


def message_text(message: Any) -> str:
    return "\n".join(block["text"] for block in _blocks(message) if "text" in block)


def message_reasoning(message: Any) -> str:
    parts = []
    for block in _blocks(message):
        reasoning = block.get("reasoningContent", {}).get("reasoningText", {})
        if reasoning.get("text"):
            parts.append(reasoning["text"])
    return "\n".join(parts)


def message_tool_uses(message: Any) -> list[dict[str, Any]]:
    return [
        {
            "name": block["toolUse"].get("name"),
            "toolUseId": block["toolUse"].get("toolUseId"),
            "input": block["toolUse"].get("input"),
        }
        for block in _blocks(message)
        if "toolUse" in block
    ]


class AuditHook(HookProvider):
    """Record one agent's lifecycle, decisions, and tool calls."""

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self._before_invocation)
        registry.add_callback(AfterInvocationEvent, self._after_invocation)
        registry.add_callback(AfterModelCallEvent, self._after_model_call)
        registry.add_callback(BeforeToolCallEvent, self._before_tool_call)
        registry.add_callback(AfterToolCallEvent, self._after_tool_call)

    def _record(self, record_type: str, data: dict[str, Any], *, critical: bool = False) -> None:
        trail = current_trail()
        if trail is not None:
            trail.record(record_type, self.agent_id, data, critical=critical)

    def _before_invocation(self, event: BeforeInvocationEvent) -> None:
        messages = event.messages or []
        last = messages[-1] if messages else None
        self._record(
            "invocation_start",
            {"input": message_text(last) if last else None},
        )

    def _after_invocation(self, event: AfterInvocationEvent) -> None:
        result = event.result
        usage = getattr(getattr(result, "metrics", None), "accumulated_usage", None)
        self._record(
            "invocation_end",
            {
                "stop_reason": getattr(result, "stop_reason", None),
                "usage": dict(usage) if usage else None,
                "interrupts": [
                    {"id": item.id, "name": item.name, "reason": item.reason}
                    for item in (getattr(result, "interrupts", None) or [])
                ],
            },
        )

    def _after_model_call(self, event: AfterModelCallEvent) -> None:
        if event.stop_response is None:
            self._record("model_error", {"exception": str(event.exception)})
            return
        message = event.stop_response.message
        tool_uses = message_tool_uses(message)
        data = {
            "stop_reason": event.stop_response.stop_reason,
            "text": message_text(message),
            "reasoning": message_reasoning(message),
            "tool_uses": tool_uses,
        }
        self._record("decision" if tool_uses else "model_response", data)

    def _before_tool_call(self, event: BeforeToolCallEvent) -> None:
        name = event.tool_use["name"]
        critical = name in SIDE_EFFECT_TOOLS
        trail = current_trail()
        if critical and trail is not None and trail.lease_lost:
            # Another worker may own this session now: do not start side effects.
            event.cancel_tool = "The session lease was lost; the action was not performed."
            return
        try:
            self._record(
                "tool_start",
                {
                    "tool": name,
                    "toolUseId": event.tool_use["toolUseId"],
                    "input": event.tool_use["input"],
                },
                critical=critical,
            )
        except AuditWriteError as error:
            logger.error("Blocking tool %s: %s", name, error)
            event.cancel_tool = "Audit trail unavailable; the action was not performed."

    def _after_tool_call(self, event: AfterToolCallEvent) -> None:
        result = event.result
        self._record(
            "tool_call",
            {
                "tool": event.tool_use["name"],
                "toolUseId": event.tool_use["toolUseId"],
                "tool_type": getattr(event.selected_tool, "tool_type", None),
                "input": event.tool_use["input"],
                "status": result.get("status"),
                "result": result.get("content"),
                "exception": str(event.exception) if event.exception else None,
                "cancel_message": event.cancel_message,
                "duration_s": getattr(event, "duration", None),
            },
            critical=event.tool_use["name"] in SIDE_EFFECT_TOOLS,
        )


def record_event(record_type: str, agent_id: str, data: dict[str, Any]) -> None:
    """Record a non-critical event from application code (memory, approvals, faults)."""
    trail = current_trail()
    if trail is not None:
        trail.record(record_type, agent_id, data)


def specialist_evidence(result: Any) -> list[Any]:
    """Pull ``evidence`` out of a structured specialist report in a tool result."""
    found: list[Any] = []
    for block in result or []:
        report = block.get("json") if isinstance(block, dict) else None
        if isinstance(report, dict):
            found.extend(report.get("evidence") or [])
    return found


def build_explanation(records: list[dict[str, Any]], specialist_tools: set[str]) -> dict[str, Any]:
    """Summarise why and how a request was answered from its audit records.

    Records can be unreadable or altered (that is what the hash chain is for), so a
    malformed record is skipped and counted instead of failing the whole summary.
    """
    rationale_by_tool_use: dict[str, str] = {}
    malformed = 0
    for record in records:
        try:
            if record["type"] == "decision" and not record["data"].get("truncated"):
                for tool_use in record["data"].get("tool_uses", []):
                    rationale_by_tool_use[tool_use["toolUseId"]] = record["data"].get("text", "")
        except (KeyError, TypeError, AttributeError):
            malformed += 1
    route, tools_used, memories, approvals, evidence = [], [], [], [], []
    for record in records:
        try:
            data = record["data"]
            if data.get("truncated"):
                continue  # oversized record: only a digest and preview were kept
            if record["type"] == "tool_call":
                entry = {
                    "agent": record["agent_id"],
                    "tool": data["tool"],
                    "status": data["status"],
                    "attempt": record["attempt"],
                }
                if data["tool"] in specialist_tools:
                    route_entry = {
                        "agent": data["tool"],
                        "reason": rationale_by_tool_use.get(data["toolUseId"], ""),
                        # False when the model delegated without writing a rationale first.
                        "rationale_recorded": bool(rationale_by_tool_use.get(data["toolUseId"])),
                        "status": data["status"],
                    }
                    route.append(route_entry)
                    evidence.extend(specialist_evidence(data.get("result")))
                tools_used.append(entry)
            elif record["type"] == "memory_read":
                memories.extend(data.get("results", []))
            elif record["type"] == "approval":
                approvals.append(data)
        except (KeyError, TypeError, AttributeError):
            malformed += 1
    return {
        "route": route,
        "tools_used": tools_used,
        "evidence": evidence,
        "memories_used": memories,
        "approvals": approvals,
        "records": len(records),
        "malformed_records": malformed,
        "attempts": max(
            (record.get("attempt", 0) for record in records if isinstance(record, dict)),
            default=0,
        ),
    }


def now_epoch() -> int:
    return int(time.time())
