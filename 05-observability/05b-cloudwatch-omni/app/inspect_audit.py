"""Read the audit trail straight from DynamoDB (no API key needed, uses your AWS identity).

Examples:
  uv run app/inspect_audit.py --session-id session-123              # list executions
  uv run app/inspect_audit.py --session-id session-123 --request-id req-1 --records
"""

import argparse
import json
from typing import Any

import boto3

from audit import build_explanation, load_records, verify_chain
from execution import ExecutionStore
from store import DynamoItemStore

MEMORY_TABLE_PARAMETER_NAME = "/workshop/mortgage-assistant/memory/table-name"
SPECIALIST_TOOLS = {
    "mortgage_education_specialist",
    "existing_mortgage_specialist",
    "mortgage_application_specialist",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect the mortgage assistant audit trail.")
    parser.add_argument("--profile")
    parser.add_argument("--region", default=None)
    parser.add_argument("--table-name")
    parser.add_argument("--actor-id", help="Defaults to participant-<AWS-account-id>.")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--request-id", help="Show one request instead of listing the session.")
    parser.add_argument("--records", action="store_true", help="Print every audit record.")
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    region = session.region_name or "us-west-2"
    table = args.table_name or session.client("ssm", region_name=region).get_parameter(
        Name=MEMORY_TABLE_PARAMETER_NAME
    )["Parameter"]["Value"].strip()
    actor_id = args.actor_id
    if not actor_id:
        account = session.client("sts").get_caller_identity()["Account"]
        actor_id = f"participant-{account}"

    store = DynamoItemStore(table, region, client=session.client("dynamodb", region_name=region))
    executions = ExecutionStore(store)

    if not args.request_id:
        for item in executions.list_session(actor_id, args.session_id):
            print(f"{item.request_id}  {item.status:<11} attempt={item.attempt}  {item.error or ''}")
        return 0

    records: list[dict[str, Any]] = load_records(store, actor_id, args.session_id, args.request_id)
    execution = executions.get(actor_id, args.session_id, args.request_id)
    print(f"Execution: {execution.summary() if execution else 'not found'}")
    if not records:
        print("No audit records found for this actor, session, and request.")
        return 1
    print(f"Records:   {len(records)}  hash chain valid: {verify_chain(records)}")
    print(json.dumps(build_explanation(records, SPECIALIST_TOOLS), indent=2))
    if args.records:
        for record in records:
            print(json.dumps(record, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
