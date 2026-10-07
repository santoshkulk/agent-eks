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
import urllib.parse
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


def call_api(
    api_url: str,
    api_key: str,
    method: str,
    path: str,
    timeout: int,
    body: dict | None = None,
    params: dict | None = None,
) -> dict:
    url = f"{api_url.rstrip('/')}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url=url,
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
        raise RuntimeError(f"EKS API returned HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Unable to reach the EKS API: {error.reason}") from error
    except (http.client.HTTPException, ConnectionError, TimeoutError) as error:
        raise RuntimeError(
            f"The EKS API closed the connection ({type(error).__name__}); "
            "the pod may have restarted"
        ) from error


def invoke(
    api_url: str,
    api_key: str,
    prompt: str,
    actor_id: str,
    session_id: str,
    timeout: int,
    request_id: str | None = None,
) -> dict:
    body = {"prompt": prompt, "actor_id": actor_id, "session_id": session_id}
    if request_id:
        body["request_id"] = request_id
    return call_api(api_url, api_key, "POST", "/invoke", timeout, body=body)


def get_trail(
    api_url: str, api_key: str, actor_id: str, session_id: str, request_id: str, timeout: int
) -> dict:
    return call_api(
        api_url,
        api_key,
        "GET",
        f"/executions/{request_id}",
        timeout,
        params={"actor_id": actor_id, "session_id": session_id},
    )


def resume(
    api_url: str, api_key: str, actor_id: str, session_id: str, request_id: str, timeout: int
) -> dict:
    return call_api(
        api_url,
        api_key,
        "POST",
        f"/executions/{request_id}/resume",
        timeout,
        body={"actor_id": actor_id, "session_id": session_id},
    )


def decide(
    api_url: str,
    api_key: str,
    actor_id: str,
    session_id: str,
    request_id: str,
    approved: bool,
    reviewer: str,
    comment: str,
    timeout: int,
) -> dict:
    """Answer every pending approval on the request with the same decision."""
    trail = get_trail(api_url, api_key, actor_id, session_id, request_id, timeout)
    pending = trail["execution"]["interrupts"]
    if not pending:
        raise RuntimeError(f"Request {request_id} has no pending approvals")
    decisions = [
        {
            "interrupt_id": item["id"],
            "approved": approved,
            "reviewer": reviewer,
            "comment": comment,
        }
        for item in pending
    ]
    return call_api(
        api_url,
        api_key,
        "POST",
        f"/executions/{request_id}/approvals",
        timeout,
        body={"actor_id": actor_id, "session_id": session_id, "decisions": decisions},
    )


def print_trail(trail: dict) -> None:
    execution = trail["execution"]
    print(
        f"Request {execution['request_id']}: {execution['status']} "
        f"(attempt {execution['attempt']}), hash chain "
        f"{'valid' if trail['chain_valid'] else 'INVALID'}"
    )
    explanation = trail["explanation"]
    for step in explanation["route"]:
        print(f"  routed to {step['agent']}: {step['reason'] or '(no rationale recorded)'}")
    for tool_call in explanation["tools_used"]:
        print(f"  tool {tool_call['agent']}/{tool_call['tool']}: {tool_call['status']}")
    for approval in explanation["approvals"]:
        verdict = "approved" if approval["approved"] else "denied"
        print(f"  {approval['tool']} {verdict} by {approval['reviewer']}")
    print()
    for record in trail["records"]:
        print(f"  {record['seq']:>3} a{record['attempt']} {record['agent_id']:<16} {record['type']}")


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
        "--request-id",
        help="Idempotency key for the prompt. Re-send it to resume or replay the request.",
    )
    parser.add_argument(
        "--resume",
        metavar="REQUEST_ID",
        help="Resume a failed request (REQUEST_ID, or 'last' for the previous one).",
    )
    parser.add_argument(
        "--approve",
        metavar="REQUEST_ID",
        help="Approve the pending tool calls on a request ('last' is accepted).",
    )
    parser.add_argument(
        "--deny",
        metavar="REQUEST_ID",
        help="Deny the pending tool calls on a request ('last' is accepted).",
    )
    parser.add_argument(
        "--trail",
        metavar="REQUEST_ID",
        help="Show the audit trail and explanation for a request ('last' is accepted).",
    )
    parser.add_argument("--reviewer", default="workshop-reviewer", help="Reviewer name.")
    parser.add_argument("--comment", default="", help="Reviewer comment.")
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

    actions = [args.resume, args.approve, args.deny, args.trail]
    if sum(1 for action in actions if action) > 1:
        parser.error("use only one of --resume, --approve, --deny, and --trail")
    if not args.prompt and not args.show_context and not any(actions):
        parser.error("--prompt is required unless --show-context or a request action is used")

    sent_request_id: str | None = None
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

        requests = state.setdefault("requests", {})

        def resolve(request_id: str) -> str:
            if request_id != "last":
                return request_id
            if actor_id not in requests:
                raise RuntimeError("No previous request is recorded for this actor")
            return requests[actor_id]

        if not args.prompt and not any(actions):
            return 0

        api_url = args.url or discover_api_url(args.namespace, args.service)
        api_key = args.api_key or discover_api_key(args.namespace, args.secret)
        common = dict(
            api_url=api_url,
            api_key=api_key,
            actor_id=actor_id,
            session_id=session_id,
            timeout=args.timeout,
        )
        if args.trail:
            trail = get_trail(request_id=resolve(args.trail), **common)
            if args.json:
                print(json.dumps(trail, indent=2))
            else:
                print_trail(trail)
            return 0
        if args.resume:
            result = resume(request_id=resolve(args.resume), **common)
        elif args.approve or args.deny:
            result = decide(
                request_id=resolve(args.approve or args.deny),
                approved=bool(args.approve),
                reviewer=args.reviewer,
                comment=args.comment,
                **common,
            )
        else:
            request_id = args.request_id or str(uuid.uuid4())
            sent_request_id = request_id
            requests[actor_id] = request_id
            save_state(args.state_file, state)
            result = invoke(
                prompt=args.prompt,
                request_id=request_id,
                **common,
            )
    except RuntimeError as error:
        print(f"Error: {error}", file=sys.stderr)
        if sent_request_id:
            print(
                f"Request ID: {sent_request_id} "
                f"(retry with --resume {sent_request_id}, or --trail {sent_request_id})",
                file=sys.stderr,
            )
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print()
        if result.get("status") == "awaiting_approval":
            print("Awaiting approval for:")
            for item in result.get("interrupts", []):
                print(f"  {item['name']}: {json.dumps(item.get('reason'))}")
            print()
            print(f"Run with --approve {result['request_id']} or --deny {result['request_id']}")
        else:
            print(result.get("response", json.dumps(result)))
        print()
        print(f"Request ID: {result.get('request_id')} (status: {result.get('status')})")
        trace_id = result.get("trace_id")
        if trace_id:
            print()
            print(f"Trace ID: {trace_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
