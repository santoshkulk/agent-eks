import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))
os.environ.setdefault("MEMORY_TABLE_NAME", "test-memory-table")

import memory  # noqa: E402


class MemoryConfigurationTests(unittest.TestCase):
    def test_default_embedding_model(self) -> None:
        self.assertEqual(memory.MEMORY_EMBEDDING_MODEL_ID, "amazon.titan-embed-text-v2:0")

    def test_actor_partition_and_identifier_validation(self) -> None:
        self.assertEqual(
            memory.actor_partition("participant-123456789012"),
            "user/participant-123456789012",
        )
        with self.assertRaises(ValueError):
            memory.validate_identifier("participant/other", "actor_id")

    @patch("memory.SnapshotSessionManager")
    @patch("memory.DynamoDBStorage")
    def test_session_manager_is_latest_only_message_level_with_ttl(
        self, storage_class, session_manager_class
    ) -> None:
        storage_class.return_value = session_storage = object()
        memory.create_session_manager("participant-1", "session-1")
        self.assertEqual(
            storage_class.call_args.kwargs["ttl_seconds"],
            memory.MEMORY_SESSION_TTL_SECONDS,
        )
        session_manager_class.assert_called_once_with(
            "session-1", storage=session_storage, save_latest_on="message"
        )
        self.assertNotIn("snapshot_trigger", session_manager_class.call_args.kwargs)

    @patch("memory.MemoryManager")
    @patch("memory.DynamoDBMemoryStore")
    @patch("memory.DynamoDBStorage")
    def test_durable_memory_storage_has_no_ttl(
        self, storage_class, memory_store_class, memory_manager_class
    ) -> None:
        storage_class.return_value = durable_storage = object()
        memory.create_memory_manager("participant-1")
        self.assertNotIn("ttl_seconds", storage_class.call_args.kwargs)
        memory_store_class.assert_called_once_with(
            storage=durable_storage, partition="user/participant-1"
        )
        memory_manager_class.assert_called_once()

    @patch("memory.get_bedrock_runtime_client")
    def test_embedding_is_titan_1024_dimensions_and_normalized(self, get_client) -> None:
        get_client.return_value.invoke_model.return_value = {
            "body": io.BytesIO(json.dumps({"embedding": [0.0] * 1024}).encode())
        }
        self.assertEqual(len(memory.embed_text("fixed rate")), 1024)
        call = get_client.return_value.invoke_model.call_args.kwargs
        self.assertEqual(call["modelId"], "amazon.titan-embed-text-v2:0")
        self.assertEqual(
            json.loads(call["body"]),
            {"inputText": "fixed rate", "dimensions": 1024, "normalize": True},
        )


class DurableMemoryStoreTests(unittest.IsolatedAsyncioTestCase):
    @patch("memory.embed_text", return_value=[0.1] * 1024)
    async def test_vector_search_is_scoped_to_actor_partition(self, _) -> None:
        storage = MagicMock()
        storage.search = AsyncMock(
            return_value=[SimpleNamespace(data=b"prefers 15-year", metadata={})]
        )
        store = memory.DynamoDBMemoryStore(storage, "user/actor-1")
        results = await store.search("preferred term")
        query = storage.search.call_args.args[0]
        self.assertEqual(query.pk, "user/actor-1")
        self.assertEqual(query.top_k, memory.MEMORY_MAX_SEARCH_RESULTS)
        self.assertTrue(query.include_values)
        self.assertEqual(results[0].content, "prefers 15-year")

    @patch("memory.embed_text", return_value=[0.1] * 1024)
    @patch("memory.uuid.uuid4")
    async def test_memory_add_uses_uuid_key(self, uuid4, _) -> None:
        uuid4.return_value.hex = "a" * 32
        storage = MagicMock()
        storage.write = AsyncMock()
        store = memory.DynamoDBMemoryStore(storage, "user/actor-1")
        await store.add("  prefers a fixed rate  ", {"kind": "preference"})
        self.assertEqual(storage.write.call_args.args[0], f"memories/{'a' * 32}")
        self.assertEqual(storage.write.call_args.args[1], b"prefers a fixed rate")
        self.assertNotIn("ttl_seconds", storage.write.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
