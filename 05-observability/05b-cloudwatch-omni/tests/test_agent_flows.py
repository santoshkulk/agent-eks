"""Local scripted-model flows for supervisor-only session persistence."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("MEMORY_TABLE_NAME", "test-memory-table")
os.environ.setdefault(
    "KB_PARAMETER_NAME", "/workshop/mortgage-assistant/bedrock/knowledge-base-id"
)
os.environ.setdefault("AWS_REGION", "us-west-2")

from strands.session import SnapshotSessionManager  # noqa: E402
from strands.storage.local_file_storage import LocalFileStorage  # noqa: E402

import mortgage_agent  # noqa: E402
import telemetry  # noqa: E402
from fixtures.scripted_model import ScriptedModel, forced_tool, last_tool_result  # noqa: E402


def user_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message["role"] == "user":
            texts = [block["text"] for block in message["content"] if "text" in block]
            if texts:
                return texts[0]
    return ""


def supervisor_handler(messages, tool_specs, tool_choice):
    if last_tool_result(messages) is not None:
        return [("text", "Here is your answer based on the specialist report.")]
    prompt = user_text(messages)
    if "balance" in prompt:
        return [
            ("text", "Routing rationale: the user asks about an existing account."),
            ("tool", "existing_mortgage_specialist", {"input": prompt}),
        ]
    return [("text", f"You said: {prompt}")]


def specialist_handler(messages, tool_specs, tool_choice):
    if forced_tool(tool_choice) == "SpecialistReport":
        return [
            (
                "tool",
                "SpecialistReport",
                {
                    "answer": "ok",
                    "rationale": "used the account tool",
                    "evidence": ["get_mortgage_details returned principal 150000"],
                },
            )
        ]
    if last_tool_result(messages) is not None:
        return [("text", "done")]
    return [("tool", "get_mortgage_details", {"customer_id": "C1"})]


class SupervisorSessionFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def session_factory(self, session_id: str):
        def make():
            return SnapshotSessionManager(
                session_id,
                storage=LocalFileStorage(self.tmp.name),
                save_latest_on="message",
            )

        return make

    def create_supervisor(self, session_id: str = "session-1"):
        return mortgage_agent.create_supervisor_agent(
            "actor-1",
            session_id,
            "request-1",
            supervisor_model=ScriptedModel(supervisor_handler),
            specialist_model=ScriptedModel(specialist_handler),
            session_factory=self.session_factory(session_id),
            use_memory=False,
            specialists=["existing_mortgage_specialist"],
        )

    def test_supervisor_snapshot_restores_follow_up_context(self) -> None:
        first = self.create_supervisor()
        first("I am considering a property worth 600000 dollars.")
        restored = self.create_supervisor()
        self.assertTrue(restored.messages)
        self.assertIn("600000", str(restored.messages))
        self.assertEqual(restored.agent_id, "supervisor")

    def test_agents_as_tools_route_without_specialist_or_history_snapshots(self) -> None:
        attributes = telemetry.trace_attributes("actor-1", "session-1", "request-1")
        with telemetry.use_trace_attributes(attributes):
            supervisor = self.create_supervisor()
            result = supervisor("What is the balance for customer C1?")
        self.assertIn("specialist report", str(result))
        snapshot_paths = [
            str(path) for path in Path(self.tmp.name).rglob("*") if path.is_file()
        ]
        self.assertTrue(
            any("supervisor" in path and "snapshot_latest" in path for path in snapshot_paths)
        )
        self.assertFalse(any("existing" in path and "snapshot" in path for path in snapshot_paths))
        self.assertFalse(any("immutable_history" in path for path in snapshot_paths))

    def test_new_session_does_not_restore_prior_transcript(self) -> None:
        self.create_supervisor("session-a")("remember this session value")
        self.assertEqual(self.create_supervisor("session-b").messages, [])


if __name__ == "__main__":
    unittest.main()
