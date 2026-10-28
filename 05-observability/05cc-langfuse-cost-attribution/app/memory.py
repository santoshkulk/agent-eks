"""Short-term session snapshots and actor-scoped long-term memory."""

import json
import os
import re
import uuid
from functools import lru_cache
from typing import Any

import boto3
from strands.memory import MemoryEntry, MemoryManager
from strands.memory.types import Metadata, SearchOptions
from strands.session import SnapshotSessionManager
from strands_dynamodb_storage import DynamoDBStorage, SearchQuery


MEMORY_TABLE_NAME = os.environ.get("MEMORY_TABLE_NAME", "")
MEMORY_VECTOR_INDEX_NAME = os.environ.get("MEMORY_VECTOR_INDEX_NAME", "vector_index")
MEMORY_EMBEDDING_MODEL_ID = os.environ.get(
    "MEMORY_EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0"
)
MEMORY_SESSION_TTL_SECONDS = int(
    os.environ.get("MEMORY_SESSION_TTL_SECONDS", str(7 * 24 * 60 * 60))
)
MEMORY_MAX_SEARCH_RESULTS = int(os.environ.get("MEMORY_MAX_SEARCH_RESULTS", "5"))

IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def get_region() -> str:
    """Return the configured AWS Region, or the workshop default."""
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"


def validate_identifier(value: str, label: str) -> str:
    """Validate an actor or session identifier used in storage paths."""
    cleaned = value.strip()
    if not IDENTIFIER_PATTERN.fullmatch(cleaned):
        raise ValueError(
            f"{label} must be 1-128 characters and contain only letters, "
            "numbers, '.', '_', ':', or '-'."
        )
    return cleaned


def actor_partition(actor_id: str) -> str:
    """Return the storage partition for one validated actor."""
    return f"user/{validate_identifier(actor_id, 'actor_id')}"


@lru_cache(maxsize=1)
def get_bedrock_runtime_client() -> Any:
    """Create and cache the Bedrock Runtime client used for embeddings."""
    return boto3.client("bedrock-runtime", region_name=get_region())


def embed_text(text: str) -> list[float]:
    """Create a normalized 1,024-dimension Titan text embedding."""
    response = get_bedrock_runtime_client().invoke_model(
        modelId=MEMORY_EMBEDDING_MODEL_ID,
        body=json.dumps({"inputText": text, "dimensions": 1024, "normalize": True}),
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
    """Adapt actor-partitioned DynamoDB vector storage to Strands memory."""

    def __init__(self, storage: DynamoDBStorage, partition: str) -> None:
        """Initialize the durable memory store for one actor partition."""
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

    async def add(self, content: str, metadata: Metadata | None = None) -> None:
        """Store one durable memory under a new UUID key without a TTL."""
        cleaned = content.strip()
        if not cleaned:
            return
        key = uuid.uuid4().hex
        await self.storage.write(
            f"memories/{key}",
            cleaned.encode("utf-8"),
            vector=embed_text(cleaned),
            metadata=_flat_metadata(metadata),
        )

    async def search(
        self,
        query: str,
        options: SearchOptions | None = None,
    ) -> list[MemoryEntry]:
        """Search only this actor's durable memories by semantic similarity."""
        del options
        results = await self.storage.search(
            SearchQuery(
                vector=embed_text(query),
                top_k=self.max_search_results,
                pk=self.partition,
                include_values=True,
            )
        )
        return [
            MemoryEntry(content=result.data.decode("utf-8"), metadata=result.metadata)
            for result in results
            if result.data is not None
        ]


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
    """Create the supervisor's latest-only, message-level session manager.

    With the supervisor's stable ``agent_id``, Strands writes the physical key
    ``session/<session_id>/scopes/agent/supervisor/snapshots/snapshot_latest.json``
    beneath the actor partition. No immutable history trigger is configured.
    """
    if not MEMORY_TABLE_NAME:
        raise RuntimeError("MEMORY_TABLE_NAME is not configured")
    return SnapshotSessionManager(
        validate_identifier(session_id, "session_id"),
        storage=_session_storage(actor_id),
        save_latest_on="message",
    )


def create_memory_manager(actor_id: str) -> MemoryManager:
    """Create actor-scoped semantic memory with no TTL for durable facts."""
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
