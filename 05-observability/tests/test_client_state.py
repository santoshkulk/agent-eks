import sys
from pathlib import Path
import tempfile
import unittest


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

import invoke_eks  # noqa: E402


class ClientStateTests(unittest.TestCase):
    def test_new_actor_gets_persisted_session(self) -> None:
        state = {"sessions": {}}
        session_id = invoke_eks.select_session(
            state,
            actor_id="participant-123456789012",
            supplied_session_id=None,
            new_session=False,
        )
        self.assertTrue(session_id.startswith("session-"))
        self.assertEqual(
            state["sessions"]["participant-123456789012"],
            session_id,
        )

    def test_existing_actor_reuses_session(self) -> None:
        state = {"sessions": {"participant-1": "session-existing"}}
        session_id = invoke_eks.select_session(
            state,
            actor_id="participant-1",
            supplied_session_id=None,
            new_session=False,
        )
        self.assertEqual(session_id, "session-existing")

    def test_new_session_replaces_current_session(self) -> None:
        state = {"sessions": {"participant-1": "session-old"}}
        session_id = invoke_eks.select_session(
            state,
            actor_id="participant-1",
            supplied_session_id=None,
            new_session=True,
        )
        self.assertNotEqual(session_id, "session-old")
        self.assertEqual(state["sessions"]["participant-1"], session_id)

    def test_state_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "client-state.json"
            expected = {"sessions": {"participant-1": "session-1"}}
            invoke_eks.save_state(path, expected)
            self.assertEqual(invoke_eks.load_state(path), expected)


if __name__ == "__main__":
    unittest.main()
