"""Invoke the Lab 5 API while keeping the current session per actor locally."""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request
import uuid


DEFAULT_NAMESPACE = "mortgage-assistant"
DEFAULT_SERVICE = "mortgage-assistant"
DEFAULT_SECRET = "mortgage-assistant-api-key"
DEFAULT_REGION = os.environ.get(
    "AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-west-2")
)
DEFAULT_STATE_FILE = Path(__file__).resolve().parents[1] / ".workshop" / "client-state.json"


class ApiError(RuntimeError):
    """The API answered with an HTTP error."""

    def __init__(self, status: int, message: str) -> None:
        """Store the HTTP status with a user-friendly message."""
        super().__init__(message)
        self.status = status


def run_command(command: list[str], description: str) -> str:
    """Run a local discovery command and return its non-empty output."""
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as error:
        raise RuntimeError(f"{command[0]} is not installed or is not on PATH") from error
    except subprocess.CalledProcessError as error:
        message = error.stderr.strip() or error.stdout.strip()
        raise RuntimeError(f"{description} failed: {message}") from error
    value = result.stdout.strip()
    if not value:
        raise RuntimeError(f"{description} returned no value")
    return value


def kubectl_jsonpath(resource: str, name: str, namespace: str, jsonpath: str) -> str:
    """Read one Kubernetes resource field through kubectl."""
    return run_command(
        [
            "kubectl",
            "get",
            resource,
            name,
            "--namespace",
            namespace,
            "--output",
            f"jsonpath={jsonpath}",
        ],
        "kubectl",
    )


def discover_api_url(namespace: str, service: str) -> str:
    """Discover the source-restricted NLB endpoint."""
    hostname = kubectl_jsonpath(
        "service",
        service,
        namespace,
        "{.status.loadBalancer.ingress[0].hostname}",
    )
    return f"http://{hostname}"


def discover_api_key(namespace: str, secret: str) -> str:
    """Read and decode the API key from the Kubernetes Secret."""
    encoded_key = kubectl_jsonpath("secret", secret, namespace, "{.data.api-key}")
    try:
        return base64.b64decode(encoded_key, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as error:
        raise RuntimeError(f"Secret {secret} contains an invalid API key") from error


def discover_actor_id(profile: str | None, region: str) -> str:
    """Derive the workshop actor from the caller's AWS account ID."""
    command = [
        "aws",
        "sts",
        "get-caller-identity",
        "--query",
        "Account",
        "--output",
        "text",
        "--region",
        region,
    ]
    if profile:
        command.extend(["--profile", profile])
    account_id = run_command(command, "AWS identity lookup")
    if not account_id.isdigit() or len(account_id) != 12:
        raise RuntimeError(f"AWS returned an unexpected account ID: {account_id}")
    return f"participant-{account_id}"


def load_state(path: Path) -> dict:
    """Load only per-actor current-session selections from local state."""
    if not path.exists():
        return {"sessions": {}}
    try:
        raw_state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Unable to read client state from {path}: {error}") from error
    sessions = raw_state.get("sessions", {}) if isinstance(raw_state, dict) else None
    if not isinstance(sessions, dict):
        raise RuntimeError(f"Client state file {path} has an invalid format")
    return {"sessions": sessions}


def save_state(path: Path, state: dict) -> None:
    """Persist only per-actor current-session selections."""
    sessions = state.get("sessions", {})
    if not isinstance(sessions, dict):
        raise RuntimeError("Client session state has an invalid format")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"sessions": sessions}, indent=2) + "\n", encoding="utf-8")
    except OSError as error:
        raise RuntimeError(f"Unable to save client state to {path}: {error}") from error


def select_session(
    state: dict,
    actor_id: str,
    supplied_session_id: str | None,
    new_session: bool,
) -> str:
    """Select and remember the current session for an actor."""
    sessions = state.setdefault("sessions", {})
    if supplied_session_id:
        session_id = supplied_session_id
    elif new_session or actor_id not in sessions:
        session_id = f"session-{uuid.uuid4()}"
    else:
        session_id = sessions[actor_id]
    sessions[actor_id] = session_id
    return session_id


def call_api(
    api_url: str,
    api_key: str,
    method: str,
    path: str,
    timeout: int,
    body: dict | None = None,
) -> dict:
    """Call the authenticated API and translate transport failures."""
    request = urllib.request.Request(
        url=f"{api_url.rstrip('/')}{path}",
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise ApiError(error.code, f"EKS API returned HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Unable to reach the EKS API: {error.reason}") from error
    except (http.client.HTTPException, ConnectionError, TimeoutError) as error:
        raise RuntimeError(
            f"The EKS API closed the connection ({type(error).__name__}); "
            "the request outcome is unknown"
        ) from error


def invoke(
    api_url: str,
    api_key: str,
    prompt: str,
    actor_id: str,
    session_id: str,
    timeout: int,
) -> dict:
    """Send one prompt using the selected actor and session."""
    return call_api(
        api_url,
        api_key,
        "POST",
        "/invoke",
        timeout,
        body={"prompt": prompt, "actor_id": actor_id, "session_id": session_id},
    )


def main() -> int:
    """Parse options, select context, and optionally invoke the API."""
    parser = argparse.ArgumentParser(
        description="Send a stateful prompt to the mortgage assistant on EKS."
    )
    parser.add_argument("--prompt", "-p", help="Prompt to send. Optional with --show-context.")
    parser.add_argument("--actor-id", help="Override participant-<AWS-account-id> actor.")
    parser.add_argument("--session-id", help="Use and remember an explicit session.")
    parser.add_argument("--new-session", action="store_true", help="Create a new session.")
    parser.add_argument(
        "--show-context", action="store_true", help="Print actor and session without invoking."
    )
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument("--profile", help="AWS CLI profile used to derive the actor ID.")
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    parser.add_argument("--service", default=DEFAULT_SERVICE)
    parser.add_argument("--secret", default=DEFAULT_SECRET)
    parser.add_argument("--url", default=os.environ.get("MORTGAGE_API_URL"))
    parser.add_argument("--api-key", default=os.environ.get("MORTGAGE_API_KEY"))
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--json", action="store_true", help="Print complete JSON response.")
    args = parser.parse_args()

    if not args.prompt and not args.show_context:
        parser.error("--prompt is required unless --show-context is used")

    try:
        actor_id = args.actor_id or discover_actor_id(args.profile, args.region)
        state = load_state(args.state_file)
        session_id = select_session(state, actor_id, args.session_id, args.new_session)
        save_state(args.state_file, state)

        context_stream = sys.stderr if args.json else sys.stdout
        print(f"Actor:   {actor_id}", file=context_stream)
        print(f"Session: {session_id}", file=context_stream)
        if not args.prompt:
            return 0

        result = invoke(
            api_url=args.url or discover_api_url(args.namespace, args.service),
            api_key=args.api_key or discover_api_key(args.namespace, args.secret),
            prompt=args.prompt,
            actor_id=actor_id,
            session_id=session_id,
            timeout=args.timeout,
        )
    except RuntimeError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print()
        print(result.get("response") or "(no response text)")
        print()
        print(f"Request ID: {result.get('request_id')}")
        if result.get("trace_id"):
            print(f"Trace ID: {result['trace_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
