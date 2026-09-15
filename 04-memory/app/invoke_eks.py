from __future__ import annotations

import argparse
import base64
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
    "AWS_REGION",
    os.environ.get("AWS_DEFAULT_REGION", "us-west-2"),
)
DEFAULT_STATE_FILE = (
    Path(__file__).resolve().parents[1] / ".workshop" / "client-state.json"
)


def run_command(command: list[str], description: str) -> str:
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise RuntimeError(f"{command[0]} is not installed or is not on PATH") from error
    except subprocess.CalledProcessError as error:
        message = error.stderr.strip() or error.stdout.strip()
        raise RuntimeError(f"{description} failed: {message}") from error
    value = result.stdout.strip()
    if not value:
        raise RuntimeError(f"{description} returned no value")
    return value


def kubectl_jsonpath(
    resource: str,
    name: str,
    namespace: str,
    jsonpath: str,
) -> str:
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
    hostname = kubectl_jsonpath(
        resource="service",
        name=service,
        namespace=namespace,
        jsonpath="{.status.loadBalancer.ingress[0].hostname}",
    )
    return f"http://{hostname}"


def discover_api_key(namespace: str, secret: str) -> str:
    encoded_key = kubectl_jsonpath(
        resource="secret",
        name=secret,
        namespace=namespace,
        jsonpath="{.data.api-key}",
    )
    try:
        return base64.b64decode(encoded_key, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as error:
        raise RuntimeError(f"Secret {secret} contains an invalid API key") from error


def discover_actor_id(profile: str | None, region: str) -> str:
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
    if not path.exists():
        return {"sessions": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Unable to read client state from {path}: {error}") from error
    if not isinstance(state, dict) or not isinstance(state.get("sessions", {}), dict):
        raise RuntimeError(f"Client state file {path} has an invalid format")
    state.setdefault("sessions", {})
    return state


def save_state(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    except OSError as error:
        raise RuntimeError(f"Unable to save client state to {path}: {error}") from error


def select_session(
    state: dict,
    actor_id: str,
    supplied_session_id: str | None,
    new_session: bool,
) -> str:
    sessions = state.setdefault("sessions", {})
    if supplied_session_id:
        session_id = supplied_session_id
    elif new_session or actor_id not in sessions:
        session_id = f"session-{uuid.uuid4()}"
    else:
        session_id = sessions[actor_id]
    sessions[actor_id] = session_id
    return session_id


def invoke(
    api_url: str,
    api_key: str,
    prompt: str,
    actor_id: str,
    session_id: str,
    timeout: int,
) -> dict:
    request = urllib.request.Request(
        url=f"{api_url.rstrip('/')}/invoke",
        data=json.dumps(
            {
                "prompt": prompt,
                "actor_id": actor_id,
                "session_id": session_id,
            }
        ).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"EKS API returned HTTP {error.code}: {body}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Unable to reach the EKS API: {error.reason}") from error


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Send a stateful prompt to the mortgage assistant on EKS."
    )
    parser.add_argument(
        "--prompt",
        "-p",
        help="Prompt to send. Optional when --show-context is used.",
    )
    parser.add_argument(
        "--actor-id",
        help="Override the default participant-<AWS-account-id> actor.",
    )
    parser.add_argument(
        "--session-id",
        help="Use an explicit session and make it the current session.",
    )
    parser.add_argument(
        "--new-session",
        action="store_true",
        help="Create a new session for the selected actor.",
    )
    parser.add_argument(
        "--show-context",
        action="store_true",
        help="Print the selected actor and session, with no invocation if prompt is omitted.",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_FILE,
        help=f"Local session-state file (default: {DEFAULT_STATE_FILE}).",
    )
    parser.add_argument("--profile", help="AWS CLI profile used to derive the actor ID.")
    parser.add_argument(
        "--region",
        default=DEFAULT_REGION,
        help=f"AWS Region (default: {DEFAULT_REGION}).",
    )
    parser.add_argument(
        "--namespace",
        default=DEFAULT_NAMESPACE,
        help=f"Kubernetes namespace (default: {DEFAULT_NAMESPACE}).",
    )
    parser.add_argument(
        "--service",
        default=DEFAULT_SERVICE,
        help=f"Kubernetes Service name (default: {DEFAULT_SERVICE}).",
    )
    parser.add_argument(
        "--secret",
        default=DEFAULT_SECRET,
        help=f"Kubernetes API-key Secret name (default: {DEFAULT_SECRET}).",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("MORTGAGE_API_URL"),
        help="API base URL; defaults to MORTGAGE_API_URL or EKS discovery.",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("MORTGAGE_API_KEY"),
        help="API key; defaults to MORTGAGE_API_KEY or EKS Secret discovery.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="HTTP timeout in seconds (default: 300).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the complete JSON response.",
    )
    args = parser.parse_args()

    if not args.prompt and not args.show_context:
        parser.error("--prompt is required unless --show-context is used")

    try:
        actor_id = args.actor_id or discover_actor_id(args.profile, args.region)
        state = load_state(args.state_file)
        session_id = select_session(
            state,
            actor_id=actor_id,
            supplied_session_id=args.session_id,
            new_session=args.new_session,
        )
        save_state(args.state_file, state)

        print(f"Actor:   {actor_id}")
        print(f"Session: {session_id}")

        if not args.prompt:
            return 0

        api_url = args.url or discover_api_url(args.namespace, args.service)
        api_key = args.api_key or discover_api_key(args.namespace, args.secret)
        result = invoke(
            api_url=api_url,
            api_key=api_key,
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
        print(result.get("response", json.dumps(result)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
