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


class CallApiErrorTests(unittest.TestCase):
    def test_dropped_connection_is_reported_without_a_traceback(self) -> None:
        import http.client
        from unittest.mock import patch

        with patch(
            "invoke_eks.urllib.request.urlopen",
            side_effect=http.client.RemoteDisconnected("closed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "closed the connection"):
                invoke_eks.call_api("http://x", "key", "POST", "/invoke", 5, body={})


class LastRequestTests(unittest.TestCase):
    def _run(self, state_file, error=None, response=None):
        from unittest.mock import patch

        argv = [
            "invoke_eks.py", "--url", "http://x", "--api-key", "k", "--actor-id", "a",
            "--session-id", "s", "--state-file", str(state_file), "--prompt", "hi",
        ]
        with patch.object(sys, "argv", argv), patch.object(
            invoke_eks, "invoke", side_effect=error, return_value=response
        ):
            return invoke_eks.main()

    def test_rejected_prompts_do_not_replace_last(self) -> None:
        import json

        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            self._run(state_file, response={"response": "ok", "status": "completed", "request_id": "r1"})
            first = json.loads(state_file.read_text())["requests"]["a"]

            code = self._run(state_file, error=invoke_eks.ApiError(409, "busy"))

            self.assertEqual(code, 1)
            self.assertEqual(json.loads(state_file.read_text())["requests"]["a"], first)

    def test_failed_prompts_stay_resumable_as_last(self) -> None:
        import json

        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            self._run(state_file, response={"response": "ok", "status": "completed", "request_id": "r1"})
            first = json.loads(state_file.read_text())["requests"]["a"]

            self._run(state_file, error=invoke_eks.ApiError(500, "boom"))

            self.assertNotEqual(json.loads(state_file.read_text())["requests"]["a"], first)


if __name__ == "__main__":
    unittest.main()
