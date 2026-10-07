import argparse
import asyncio
import json
import os
import time
from typing import Any

import boto3
from strands_dynamodb_storage import (
    DynamoDBListQuery,
    DynamoDBStorage,
    SearchQuery,
)


MEMORY_TABLE_PARAMETER_NAME = "/workshop/mortgage-assistant/memory/table-name"
MEMORY_VECTOR_INDEX_PARAMETER_NAME = (
    "/workshop/mortgage-assistant/memory/vector-index-name"
)
DEFAULT_EMBEDDING_MODEL_ID = "amazon.titan-embed-text-v2:0"


def ssm_parameter(ssm_client: Any, parameter_name: str) -> str:
    response = ssm_client.get_parameter(Name=parameter_name)
    value = response["Parameter"]["Value"].strip()
    if not value:
        raise RuntimeError(f"SSM parameter {parameter_name} is empty")
    return value


def resource_name(
    override: str | None,
    ssm_client: Any,
    parameter_name: str,
) -> str:
    return override or ssm_parameter(ssm_client, parameter_name)


def default_actor_id(session: boto3.Session) -> str:
    account_id = session.client("sts").get_caller_identity()["Account"]
    return f"participant-{account_id}"


def make_embedder(session: boto3.Session, region: str, model_id: str) -> Any:
    bedrock = session.client("bedrock-runtime", region_name=region)

    def embed(text: str) -> list[float]:
        response = bedrock.invoke_model(
            modelId=model_id,
            body=json.dumps(
                {
                    "inputText": text,
                    "dimensions": 1024,
                    "normalize": True,
                }
            ),
        )
        return json.loads(response["body"].read())["embedding"]

    return embed


async def inspect(
    storage: DynamoDBStorage,
    partition: str,
    show_sessions: bool,
    show_memories: bool,
    search_text: str | None,
    embed: Any,
    wait_seconds: int,
) -> None:
    if show_sessions:
        session_keys = await storage.list(
            DynamoDBListQuery(pk=partition, sk_prefix="session/")
        )
        print("Short-term session records")
        if session_keys:
            for key in session_keys:
                print(f"  {key}")
        else:
            print("  No session records found.")

    if show_memories:
        memory_keys = await storage.list("memories/")
        print("Long-term memory records")
        if memory_keys:
            for key in memory_keys:
                data = await storage.read(key)
                text = data.decode("utf-8") if data is not None else "<unavailable>"
                print(f"  {key}: {text}")
        else:
            print("  No long-term memories found.")

    if search_text:
        deadline = time.monotonic() + wait_seconds
        results = []
        while True:
            results = await storage.search(
                SearchQuery(
                    vector=embed(search_text),
                    top_k=5,
                    pk=partition,
                    include_values=True,
                )
            )
            if results or time.monotonic() >= deadline:
                break
            await asyncio.sleep(5)

        print(f'Semantic search: "{search_text}"')
        if results:
            for result in results:
                text = (
                    result.data.decode("utf-8")
                    if result.data is not None
                    else "<value not projected>"
                )
                print(f"  score={result.score:.6f} {text}")
        else:
            print("  No matching memories found.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect short-term and long-term mortgage assistant memory."
    )
    parser.add_argument(
        "--region",
        default=os.environ.get(
            "AWS_REGION",
            os.environ.get("AWS_DEFAULT_REGION", "us-west-2"),
        ),
    )
    parser.add_argument("--profile")
    parser.add_argument("--table-name")
    parser.add_argument("--vector-index-name")
    parser.add_argument("--actor-id")
    parser.add_argument(
        "--embedding-model-id",
        default=DEFAULT_EMBEDDING_MODEL_ID,
    )
    parser.add_argument("--sessions", action="store_true")
    parser.add_argument("--memories", action="store_true")
    parser.add_argument("--search")
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=60,
        help="Wait for an eventually consistent vector result (default: 60).",
    )
    args = parser.parse_args()

    try:
        session = boto3.Session(profile_name=args.profile, region_name=args.region)
        ssm_client = session.client("ssm", region_name=args.region)
        table_name = resource_name(
            args.table_name,
            ssm_client,
            MEMORY_TABLE_PARAMETER_NAME,
        )
        vector_index_name = resource_name(
            args.vector_index_name,
            ssm_client,
            MEMORY_VECTOR_INDEX_PARAMETER_NAME,
        )
        actor_id = args.actor_id or default_actor_id(session)
        partition = f"user/{actor_id}"
        storage = DynamoDBStorage(
            table_name,
            boto_session=session,
            prefix=partition,
            compression="gzip",
            index_name=vector_index_name,
        )
        show_sessions = args.sessions or (
            not args.sessions and not args.memories and not args.search
        )
        show_memories = args.memories or (
            not args.sessions and not args.memories and not args.search
        )
        asyncio.run(
            inspect(
                storage=storage,
                partition=partition,
                show_sessions=show_sessions,
                show_memories=show_memories,
                search_text=args.search,
                embed=make_embedder(
                    session,
                    args.region,
                    args.embedding_model_id,
                ),
                wait_seconds=args.wait_seconds,
            )
        )
    except Exception as error:
        print(f"Error: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
