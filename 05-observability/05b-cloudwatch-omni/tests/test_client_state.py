import contextlib
import io
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
        session_id = invoke_eks.select_session(state, "participant-1", None, False)
        self.assertTrue(session_id.startswith("session-"))
        self.assertEqual(state["sessions"]["participant-1"], session_id)

    def test_existing_actor_reuses_session(self) -> None:
        state = {"sessions": {"participant-1": "session-existing"}}
        self.assertEqual(
            invoke_eks.select_session(state, "participant-1", None, False),
            "session-existing",
        )

    def test_explicit_and_new_sessions_become_current(self) -> None:
        state = {"sessions": {"participant-1": "session-old"}}
        self.assertEqual(
            invoke_eks.select_session(state, "participant-1", "session-explicit", False),
            "session-explicit",
        )
        new_session = invoke_eks.select_session(state, "participant-1", None, True)
        self.assertNotEqual(new_session, "session-explicit")
        self.assertEqual(state["sessions"]["participant-1"], new_session)

    def test_state_round_trip_keeps_only_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(
                json.dumps({"sessions": {"a": "s"}, "requests": {"a": "old"}})
            )
            state = invoke_eks.load_state(path)
            self.assertEqual(state, {"sessions": {"a": "s"}})
            invoke_eks.save_state(path, state)
            self.assertEqual(json.loads(path.read_text()), state)

    def test_invoke_sends_prompt_actor_and_session_only(self) -> None:
        with patch("invoke_eks.call_api", return_value={}) as call:
            invoke_eks.invoke("http://x", "key", "hello", "actor-1", "session-1", 5)
        self.assertEqual(
            call.call_args.kwargs["body"],
            {"prompt": "hello", "actor_id": "actor-1", "session_id": "session-1"},
        )

    def test_human_output_prints_returned_trace_id(self) -> None:
        argv = [
            "invoke_eks.py",
            "--url",
            "http://x",
            "--api-key",
            "key",
            "--actor-id",
            "actor-1",
            "--session-id",
            "session-1",
            "--prompt",
            "hello",
        ]
        output = io.StringIO()
        with (
            patch.object(sys, "argv", argv),
            patch("invoke_eks.save_state"),
            patch("invoke_eks.load_state", return_value={"sessions": {}}),
            patch(
                "invoke_eks.invoke",
                return_value={"request_id": "request-1", "response": "ok", "trace_id": "b" * 32},
            ),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(invoke_eks.main(), 0)
        self.assertIn(f"Trace ID: {'b' * 32}", output.getvalue())

    def test_omni_resource_defaults_remain_distinct(self) -> None:
        self.assertEqual(invoke_eks.DEFAULT_NAMESPACE, "mortgage-assistant")
        self.assertEqual(invoke_eks.DEFAULT_SERVICE, "mortgage-assistant-omni")
        self.assertEqual(invoke_eks.DEFAULT_SECRET, "mortgage-assistant-omni-api-key")


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
