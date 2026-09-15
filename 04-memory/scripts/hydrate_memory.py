from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from typing import Any

import boto3
from strands_dynamodb_storage import (
    DynamoDBListQuery,
    DynamoDBStorage,
    SearchQuery,
)


SAMPLE_MEMORIES = [
    (
        "User is considering purchasing a property worth approximately $600,000.",
        {"kind": "mortgage-goal"},
    ),
    (
        "User prefers a 15-year fixed-rate mortgage.",
        {"kind": "mortgage-preference"},
    ),
    (
        "User prioritizes paying off the mortgage loan early.",
        {"kind": "repayment-preference"},
    ),
]


def stack_output(
    session: boto3.Session,
    region: str,
    stack_name: str,
    output_key: str,
) -> str:
    response = session.client("cloudformation", region_name=region).describe_stacks(
        StackName=stack_name
    )
    for output in response["Stacks"][0].get("Outputs", []):
        if output.get("OutputKey") == output_key:
            return output["OutputValue"]
    raise RuntimeError(f"Stack {stack_name} does not contain output {output_key}")


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
        result = json.loads(response["body"].read())
        embedding = result.get("embedding")
        if not isinstance(embedding, list) or len(embedding) != 1024:
            raise RuntimeError(
                f"Embedding model {model_id} did not return 1024 values."
            )
        return [float(component) for component in embedding]

    return embed


async def clear_actor(
    storage: DynamoDBStorage,
    partition: str,
    all_data: bool,
) -> int:
    if all_data:
        keys = await storage.list(DynamoDBListQuery(pk=partition))
    else:
        keys = await storage.list("memories/")
    for key in keys:
        await storage.delete(key)
    return len(keys)


async def seed_actor(
    storage: DynamoDBStorage,
    partition: str,
    embed: Any,
    replace: bool,
    wait_seconds: int,
) -> None:
    if replace:
        removed = await clear_actor(storage, partition, all_data=False)
        print(f"Removed {removed} existing long-term memories.")

    for content, metadata in SAMPLE_MEMORIES:
        await storage.write(
            f"memories/{uuid.uuid4().hex}",
            content.encode("utf-8"),
            vector=embed(content),
            metadata=metadata,
        )
        print(f"Stored: {content}")

    query = "preferred mortgage term, rate structure, and repayment goal"
    deadline = time.monotonic() + wait_seconds
    while True:
        results = await storage.search(
            SearchQuery(
                vector=embed(query),
                top_k=len(SAMPLE_MEMORIES),
                pk=partition,
                include_values=True,
            )
        )
        if results:
            print(f"Vector index returned {len(results)} hydrated memories.")
            return
        if time.monotonic() >= deadline:
            print(
                "Memories were written, but the eventually consistent vector "
                "index did not return them before the wait period expired."
            )
            return
        await asyncio.sleep(5)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Seed or clear sample long-term mortgage memories."
    )
    parser.add_argument("action", choices=["seed", "clear"])
    parser.add_argument(
        "--region",
        default="us-west-2",
    )
    parser.add_argument("--profile")
    parser.add_argument(
        "--stack-name",
        default="mortgage-assistant-workshop",
        help="Lab 00 CloudFormation stack name.",
    )
    parser.add_argument("--actor-id")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="For seed: remove existing long-term memories before hydrating.",
    )
    parser.add_argument(
        "--all-data",
        action="store_true",
        help="For clear: remove sessions and memories for the actor.",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=60,
        help="Wait for vector-index visibility after seeding.",
    )
    args = parser.parse_args()

    if args.action == "seed" and args.all_data:
        parser.error("--all-data can only be used with the clear action")
    if args.action == "clear" and args.replace:
        parser.error("--replace can only be used with the seed action")

    try:
        session = boto3.Session(
            profile_name=args.profile,
            region_name=args.region,
        )
        table_name = stack_output(
            session,
            args.region,
            args.stack_name,
            "MemoryTableName",
        )
        vector_index_name = stack_output(
            session,
            args.region,
            args.stack_name,
            "MemoryVectorIndexName",
        )
        embedding_model_id = stack_output(
            session,
            args.region,
            args.stack_name,
            "MemoryEmbeddingModelId",
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
        print(f"Actor: {actor_id}")

        if args.action == "clear":
            removed = asyncio.run(
                clear_actor(
                    storage=storage,
                    partition=partition,
                    all_data=args.all_data,
                )
            )
            scope = "session and memory" if args.all_data else "long-term memory"
            print(f"Removed {removed} {scope} records.")
        else:
            asyncio.run(
                seed_actor(
                    storage=storage,
                    partition=partition,
                    embed=make_embedder(
                        session,
                        args.region,
                        embedding_model_id,
                    ),
                    replace=args.replace,
                    wait_seconds=args.wait_seconds,
                )
            )
    except Exception as error:
        print(f"Memory hydration failed: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
