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
        await self.storage.write(
            f"memories/{uuid.uuid4().hex}",
            cleaned.encode("utf-8"),
            vector=embed_text(cleaned),
            metadata=_flat_metadata(metadata),
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
        return [
            MemoryEntry(
                content=result.data.decode("utf-8"),
                metadata=result.metadata,
            )
            for result in results
            if result.data is not None
        ]


def create_memory_components(
    actor_id: str,
    session_id: str,
) -> tuple[SnapshotSessionManager, MemoryManager]:
    if not MEMORY_TABLE_NAME:
        raise RuntimeError("MEMORY_TABLE_NAME is not configured")

    partition = actor_partition(actor_id)
    validated_session_id = validate_identifier(session_id, "session_id")

    durable_storage = DynamoDBStorage(
        MEMORY_TABLE_NAME,
        region_name=get_region(),
        prefix=partition,
        compression="gzip",
        index_name=MEMORY_VECTOR_INDEX_NAME,
    )
    session_storage = DynamoDBStorage(
        MEMORY_TABLE_NAME,
        region_name=get_region(),
        prefix=partition,
        compression="gzip",
        ttl_seconds=MEMORY_SESSION_TTL_SECONDS,
        index_name=MEMORY_VECTOR_INDEX_NAME,
    )

    session_manager = SnapshotSessionManager(
        validated_session_id,
        storage=session_storage,
    )
    memory_store = DynamoDBMemoryStore(
        storage=durable_storage,
        partition=partition,
    )
    memory_manager = MemoryManager(
        stores=[memory_store],  # type: ignore[list-item]
        add_tool_config=True,
    )
    return session_manager, memory_manager
