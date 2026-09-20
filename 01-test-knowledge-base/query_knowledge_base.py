import argparse
import json
import os
import sys
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError


DEFAULT_REGION = "us-west-2"
DEFAULT_PARAMETER_NAME = "/workshop/mortgage-assistant/bedrock/knowledge-base-id"


def create_session(profile: str | None, region: str) -> boto3.Session:
    return boto3.Session(profile_name=profile, region_name=region)


def resolve_knowledge_base_id(
    session: boto3.Session,
    parameter_name: str,
    knowledge_base_id: str | None,
) -> str:
    if knowledge_base_id:
        return knowledge_base_id

    response = session.client("ssm").get_parameter(Name=parameter_name)
    value = response["Parameter"]["Value"].strip()
    if not value:
        raise RuntimeError(f"SSM parameter {parameter_name} is empty")
    return value


def retrieve(
    session: boto3.Session,
    knowledge_base_id: str,
    query: str,
    number_of_results: int,
) -> list[dict[str, Any]]:
    response = session.client("bedrock-agent-runtime").retrieve(
        knowledgeBaseId=knowledge_base_id,
        retrievalQuery={"text": query},
        retrievalConfiguration={
            "vectorSearchConfiguration": {
                "numberOfResults": number_of_results,
            }
        },
    )
    return response.get("retrievalResults", [])


def print_results(results: list[dict[str, Any]]) -> None:
    if not results:
        print("No matching document chunks were returned.")
        return

    for position, result in enumerate(results, start=1):
        content = result.get("content", {}).get("text", "").strip()
        score = result.get("score")
        location = result.get("location", {})
        source = (
            location.get("s3Location", {}).get("uri")
            or location.get("webLocation", {}).get("url")
            or "unknown"
        )

        print(f"\nResult {position}")
        print(f"Score: {score if score is not None else 'not provided'}")
        print(f"Source: {source}")
        print(content)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Query the mortgage Amazon Bedrock Knowledge Base directly."
    )
    parser.add_argument("--query", "-q", required=True, help="Retrieval query.")
    parser.add_argument(
        "--region",
        default=os.environ.get(
            "AWS_REGION",
            os.environ.get("AWS_DEFAULT_REGION", DEFAULT_REGION),
        ),
        help=f"AWS Region (default: {DEFAULT_REGION}).",
    )
    parser.add_argument(
        "--profile",
        default=os.environ.get("AWS_PROFILE"),
        help="AWS profile; defaults to AWS_PROFILE or the default AWS profile.",
    )
    parser.add_argument(
        "--parameter-name",
        default=os.environ.get("KB_PARAMETER_NAME", DEFAULT_PARAMETER_NAME),
        help=f"SSM parameter containing the Knowledge Base ID (default: {DEFAULT_PARAMETER_NAME}).",
    )
    parser.add_argument(
        "--knowledge-base-id",
        default=os.environ.get("KNOWLEDGE_BASE_ID"),
        help="Use this Knowledge Base ID instead of reading SSM.",
    )
    parser.add_argument(
        "--number-of-results",
        type=int,
        default=3,
        choices=range(1, 101),
        metavar="1-100",
        help="Maximum result count (default: 3).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the complete retrieval result as JSON.",
    )
    args = parser.parse_args()

    try:
        session = create_session(args.profile, args.region)
        knowledge_base_id = resolve_knowledge_base_id(
            session,
            args.parameter_name,
            args.knowledge_base_id,
        )
        results = retrieve(
            session,
            knowledge_base_id,
            args.query.strip(),
            args.number_of_results,
        )
    except (BotoCoreError, ClientError, RuntimeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    print(f"Knowledge Base ID: {knowledge_base_id}")
    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        print_results(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
