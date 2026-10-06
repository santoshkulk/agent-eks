import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))
os.environ.setdefault("MEMORY_TABLE_NAME", "test-memory-table")

import memory  # noqa: E402


class MemoryConfigurationTests(unittest.TestCase):
    def test_default_embedding_model(self) -> None:
        self.assertEqual(
            memory.MEMORY_EMBEDDING_MODEL_ID,
            "amazon.titan-embed-text-v2:0",
        )

    def test_actor_partition(self) -> None:
        self.assertEqual(
            memory.actor_partition("participant-123456789012"),
            "user/participant-123456789012",
        )

    def test_identifier_rejects_path_separator(self) -> None:
        with self.assertRaises(ValueError):
            memory.validate_identifier("participant/other", "actor_id")

    @patch("memory.SnapshotSessionManager")
    @patch("memory.DynamoDBStorage")
    def test_session_manager_is_message_level_with_ttl_and_history(
        self,
        storage_class,
        session_manager_class,
    ) -> None:
        session_storage = object()
        storage_class.return_value = session_storage

        memory.create_session_manager(actor_id="participant-1", session_id="session-1")

        self.assertEqual(
            storage_class.call_args.kwargs["ttl_seconds"],
            memory.MEMORY_SESSION_TTL_SECONDS,
        )
        session_manager_class.assert_called_once_with(
            "session-1",
            storage=session_storage,
            save_latest_on="message",
            snapshot_trigger=memory._snapshot_trigger,
        )

    @patch("memory.MemoryManager")
    @patch("memory.DynamoDBMemoryStore")
    @patch("memory.DynamoDBStorage")
    def test_durable_memory_storage_has_no_ttl(
        self,
        storage_class,
        memory_store_class,
        memory_manager_class,
    ) -> None:
        durable_storage = object()
        storage_class.return_value = durable_storage

        memory.create_memory_manager(actor_id="participant-1")

        self.assertNotIn("ttl_seconds", storage_class.call_args.kwargs)
        memory_store_class.assert_called_once_with(
            storage=durable_storage,
            partition="user/participant-1",
        )
        memory_manager_class.assert_called_once()


if __name__ == "__main__":
    unittest.main()
