import json
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


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
        self.assertEqual(state["sessions"]["participant-123456789012"], session_id)

    def test_existing_actor_reuses_session(self) -> None:
        state = {"sessions": {"participant-1": "session-existing"}}
        session_id = invoke_eks.select_session(
            state,
            actor_id="participant-1",
            supplied_session_id=None,
            new_session=False,
        )
        self.assertEqual(session_id, "session-existing")

    def test_explicit_session_becomes_current(self) -> None:
        state = {"sessions": {"participant-1": "session-old"}}
        session_id = invoke_eks.select_session(
            state,
            actor_id="participant-1",
            supplied_session_id="session-explicit",
            new_session=False,
        )
        self.assertEqual(session_id, "session-explicit")
        self.assertEqual(state["sessions"]["participant-1"], session_id)

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

    def test_state_round_trip_keeps_only_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "client-state.json"
            path.write_text(
                json.dumps(
                    {
                        "sessions": {"participant-1": "session-1"},
                        "requests": {"participant-1": "old-request"},
                    }
                )
            )
            state = invoke_eks.load_state(path)
            self.assertEqual(state, {"sessions": {"participant-1": "session-1"}})
            invoke_eks.save_state(path, state)
            self.assertEqual(json.loads(path.read_text()), state)

    def test_invoke_sends_prompt_actor_and_session_only(self) -> None:
        with patch("invoke_eks.call_api", return_value={}) as call:
            invoke_eks.invoke("http://x", "key", "hello", "actor-1", "session-1", 5)
        self.assertEqual(
            call.call_args.kwargs["body"],
            {"prompt": "hello", "actor_id": "actor-1", "session_id": "session-1"},
        )


class CallApiErrorTests(unittest.TestCase):
    def test_dropped_connection_is_reported_without_replay_claim(self) -> None:
        import http.client

        with patch(
            "invoke_eks.urllib.request.urlopen",
            side_effect=http.client.RemoteDisconnected("closed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "outcome is unknown"):
                invoke_eks.call_api("http://x", "key", "POST", "/invoke", 5, body={})


if __name__ == "__main__":
    unittest.main()
