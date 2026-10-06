import asyncio
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

import boto3
from strands.memory import MemoryEntry, MemoryManager
from strands.memory.types import Metadata, SearchOptions
from strands.session import SnapshotSessionManager
from strands_dynamodb_storage import DynamoDBStorage, SearchQuery

from audit import current_trail, record_event
from store import DynamoItemStore, ItemStore


MEMORY_TABLE_NAME = os.environ.get("MEMORY_TABLE_NAME", "")
MEMORY_VECTOR_INDEX_NAME = os.environ.get(
    "MEMORY_VECTOR_INDEX_NAME",
    "vector_index",
)
MEMORY_EMBEDDING_MODEL_ID = os.environ.get(
    "MEMORY_EMBEDDING_MODEL_ID",
    "amazon.titan-embed-text-v2:0",
)
MEMORY_SESSION_TTL_SECONDS = int(
    os.environ.get("MEMORY_SESSION_TTL_SECONDS", str(7 * 24 * 60 * 60))
)
MEMORY_MAX_SEARCH_RESULTS = int(
    os.environ.get("MEMORY_MAX_SEARCH_RESULTS", "5")
)
# Keep an immutable snapshot after every invocation (rollback and forensics).
SNAPSHOT_HISTORY = os.environ.get("SNAPSHOT_HISTORY", "true").strip().lower() == "true"

IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def get_region() -> str:
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-west-2"
    )


def validate_identifier(value: str, label: str) -> str:
    cleaned = value.strip()
    if not IDENTIFIER_PATTERN.fullmatch(cleaned):
        raise ValueError(
            f"{label} must be 1-128 characters and contain only letters, "
            "numbers, '.', '_', ':', or '-'."
        )
    return cleaned


def actor_partition(actor_id: str) -> str:
    return f"user/{validate_identifier(actor_id, 'actor_id')}"


@lru_cache(maxsize=1)
def get_item_store() -> ItemStore:
    """Conditional-write store on the same table for audit, execution, and ledger items."""
    if not MEMORY_TABLE_NAME:
        raise RuntimeError("MEMORY_TABLE_NAME is not configured")
    return DynamoItemStore(MEMORY_TABLE_NAME, get_region())


@lru_cache(maxsize=1)
def get_bedrock_runtime_client() -> Any:
    return boto3.client("bedrock-runtime", region_name=get_region())


def embed_text(text: str) -> list[float]:
    response = get_bedrock_runtime_client().invoke_model(
        modelId=MEMORY_EMBEDDING_MODEL_ID,
        body=json.dumps(
            {
                "inputText": text,
                "dimensions": 1024,
                "normalize": True,
            }
        ),
    )
    result = json.loads(response["body"].read())
    embedding = result.get("embedding")
    if not isinstance(embedding, list) or len(embedding) != 1024:
        raise RuntimeError(
            f"Embedding model {MEMORY_EMBEDDING_MODEL_ID} did not return "
            "a 1024-dimension embedding."
        )
    return [float(component) for component in embedding]


def _flat_metadata(metadata: Metadata | None) -> dict[str, str | int | float | bool]:
    flat: dict[str, str | int | float | bool] = {}
    for key, value in (metadata or {}).items():
        if isinstance(value, (str, int, float, bool)):
            flat[key] = value
    return flat


class DynamoDBMemoryStore:
    """Adapt DynamoDB vector search to the Strands MemoryStore protocol."""

    def __init__(
        self,
        storage: DynamoDBStorage,
        partition: str,
    ) -> None:
        self.storage = storage
        self.partition = partition
        self.name = "mortgage-preferences"
        self.description = (
            "Durable mortgage goals and preferences for this workshop actor, "
            "searched by semantic similarity."
        )
        self.max_search_results = MEMORY_MAX_SEARCH_RESULTS
        self.writable = True
        self.extraction = None

    async def add(
        self,
        content: str,
        metadata: Metadata | None = None,
    ) -> None:
        cleaned = content.strip()
        if not cleaned:
            return
        trail = current_trail()
        provenance: dict[str, str | int | float | bool] = {}
        key = uuid.uuid4().hex
        if trail is not None:
            # Deterministic key: a retried request overwrites instead of duplicating.
            digest = hashlib.sha256(
                f"{trail.request_id}:{cleaned}".encode("utf-8")
            ).hexdigest()
            key = digest[:32]
            provenance = {
                "request_id": trail.request_id,
                "session_id": trail.session_id,
                "actor_id": trail.actor_id,
                "source_agent": "supervisor",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        await self.storage.write(
            f"memories/{key}",
            cleaned.encode("utf-8"),
            vector=embed_text(cleaned),
            metadata={**_flat_metadata(metadata), **provenance},
        )
        await asyncio.to_thread(
            record_event,
            "memory_write",
            "supervisor",
            {"key": f"memories/{key}", "content": cleaned, **provenance},
        )

    async def search(
        self,
        query: str,
        options: SearchOptions | None = None,
    ) -> list[MemoryEntry]:
        results = await self.storage.search(
            SearchQuery(
                vector=embed_text(query),
                top_k=self.max_search_results,
                pk=self.partition,
                include_values=True,
            )
        )
        found = [result for result in results if result.data is not None]
        await asyncio.to_thread(
            record_event,
            "memory_read",
            "supervisor",
            {
                "query": query,
                "results": [
                    {
                        "key": result.key,
                        "score": result.score,
                        "content": (result.data or b"").decode("utf-8")[:500],
                    }
                    for result in found
                ],
            },
        )
        return [
            MemoryEntry(
                content=(result.data or b"").decode("utf-8"),
                metadata=result.metadata,
            )
            for result in found
        ]


def _snapshot_trigger(*, agent_data: Any, **kwargs: Any) -> bool:
    return SNAPSHOT_HISTORY


def _session_storage(actor_id: str) -> DynamoDBStorage:
    return DynamoDBStorage(
        MEMORY_TABLE_NAME,
        region_name=get_region(),
        prefix=actor_partition(actor_id),
        compression="gzip",
        ttl_seconds=MEMORY_SESSION_TTL_SECONDS,
        index_name=MEMORY_VECTOR_INDEX_NAME,
    )


def create_session_manager(actor_id: str, session_id: str) -> SnapshotSessionManager:
    """Message-level durable snapshots for one agent (the agent's own ``agent_id`` scopes the key).

    Every specialist and the supervisor get their own manager, so each keeps its own
    conversation under ``session/<session_id>/scopes/agent/<agent_id>/``.
    """
    if not MEMORY_TABLE_NAME:
        raise RuntimeError("MEMORY_TABLE_NAME is not configured")
    return SnapshotSessionManager(
        validate_identifier(session_id, "session_id"),
        storage=_session_storage(actor_id),
        save_latest_on="message",
        snapshot_trigger=_snapshot_trigger,
    )


def create_memory_manager(actor_id: str) -> MemoryManager:
    if not MEMORY_TABLE_NAME:
        raise RuntimeError("MEMORY_TABLE_NAME is not configured")
    partition = actor_partition(actor_id)
    durable_storage = DynamoDBStorage(
        MEMORY_TABLE_NAME,
        region_name=get_region(),
        prefix=partition,
        compression="gzip",
        index_name=MEMORY_VECTOR_INDEX_NAME,
    )
    return MemoryManager(
        stores=[DynamoDBMemoryStore(storage=durable_storage, partition=partition)],  # type: ignore[list-item]
        add_tool_config=True,
    )
