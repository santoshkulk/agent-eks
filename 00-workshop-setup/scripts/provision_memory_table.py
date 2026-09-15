import argparse
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError


VECTOR_DIMENSIONS = 1024
VECTOR_DISTANCE_FUNCTION = "COSINE"
VECTOR_ATTRIBUTE_NAME = "vector"
TTL_ATTRIBUTE_NAME = "expireAt"


def table_description(client: Any, table_name: str) -> dict | None:
    try:
        return client.describe_table(TableName=table_name)["Table"]
    except client.exceptions.ResourceNotFoundException:
        return None


def create_table(
    client: Any,
    table_name: str,
    vector_index_name: str,
    kms_key_arn: str,
) -> None:
    print(f"Creating DynamoDB memory table {table_name}")
    client.create_table(
        TableName=table_name,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        SSESpecification={
            "Enabled": True,
            "SSEType": "KMS",
            "KMSMasterKeyId": kms_key_arn,
        },
        VectorIndexes=[
            {
                "IndexName": vector_index_name,
                "VectorAttribute": {"AttributeName": VECTOR_ATTRIBUTE_NAME},
                "SearchSchema": [
                    {
                        "AttributeName": "pk",
                        "SearchSchemaElementType": "HASH",
                    }
                ],
                "Projection": {"ProjectionType": "ALL"},
                "Dimensions": VECTOR_DIMENSIONS,
                "DistanceFunction": VECTOR_DISTANCE_FUNCTION,
            }
        ],
        Tags=[
            {"Key": "Workshop", "Value": "mortgage-assistant"},
            {"Key": "Module", "Value": "00-workshop-setup"},
            {"Key": "ManagedBy", "Value": "workshop-setup"},
        ],
    )


def validate_table_shape(
    table: dict,
    table_name: str,
    vector_index_name: str,
) -> None:
    key_schema = {
        entry["AttributeName"]: entry["KeyType"]
        for entry in table.get("KeySchema", [])
    }
    if key_schema != {"pk": "HASH", "sk": "RANGE"}:
        raise RuntimeError(
            f"Existing table {table_name} does not use the required "
            "pk HASH and sk RANGE key schema."
        )

    billing_mode = table.get("BillingModeSummary", {}).get("BillingMode")
    if billing_mode and billing_mode != "PAY_PER_REQUEST":
        raise RuntimeError(
            f"Existing table {table_name} must use PAY_PER_REQUEST billing."
        )

    indexes = table.get("VectorIndexes", [])
    index = next(
        (
            item
            for item in indexes
            if item.get("IndexName") == vector_index_name
        ),
        None,
    )
    if index is None:
        raise RuntimeError(
            f"Existing table {table_name} does not contain vector index "
            f"{vector_index_name}. Delete the table and rerun workshop setup."
        )
    if index.get("Dimensions") != VECTOR_DIMENSIONS:
        raise RuntimeError(
            f"Vector index {vector_index_name} must use "
            f"{VECTOR_DIMENSIONS} dimensions."
        )
    if index.get("DistanceFunction") != VECTOR_DISTANCE_FUNCTION:
        raise RuntimeError(
            f"Vector index {vector_index_name} must use "
            f"{VECTOR_DISTANCE_FUNCTION} distance."
        )


def ensure_kms_key(
    client: Any,
    table: dict,
    table_name: str,
    kms_key_arn: str,
) -> None:
    sse = table.get("SSEDescription", {})
    actual_key_arn = sse.get("KMSMasterKeyArn")
    if sse.get("SSEType") == "KMS" and actual_key_arn == kms_key_arn:
        return

    print(f"Updating {table_name} to use the Lab 00 memory KMS key")
    client.update_table(
        TableName=table_name,
        SSESpecification={
            "Enabled": True,
            "SSEType": "KMS",
            "KMSMasterKeyId": kms_key_arn,
        },
    )


def wait_until_ready(
    client: Any,
    table_name: str,
    vector_index_name: str,
    kms_key_arn: str,
    timeout_seconds: int,
) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last_status = ""
    while time.monotonic() < deadline:
        table = client.describe_table(TableName=table_name)["Table"]
        indexes = table.get("VectorIndexes", [])
        index = next(
            (
                item
                for item in indexes
                if item.get("IndexName") == vector_index_name
            ),
            None,
        )
        table_status = table.get("TableStatus", "UNKNOWN")
        index_status = index.get("IndexStatus", "MISSING") if index else "MISSING"
        backfilling = index.get("Backfilling", False) if index else False
        actual_key_arn = table.get("SSEDescription", {}).get("KMSMasterKeyArn")
        key_ready = actual_key_arn == kms_key_arn
        status = (
            f"table={table_status}, index={index_status}, "
            f"backfilling={backfilling}, kms_key_ready={key_ready}"
        )
        if status != last_status:
            print(f"Waiting for memory storage: {status}")
            last_status = status
        if (
            table_status == "ACTIVE"
            and index_status == "ACTIVE"
            and not backfilling
            and key_ready
        ):
            return table
        time.sleep(10)
    raise TimeoutError(
        f"Timed out after {timeout_seconds}s waiting for {table_name}, "
        f"vector index {vector_index_name}, and its KMS key."
    )


def enable_ttl(client: Any, table_name: str) -> None:
    description = client.describe_time_to_live(TableName=table_name)
    ttl = description.get("TimeToLiveDescription", {})
    if (
        ttl.get("TimeToLiveStatus") in {"ENABLED", "ENABLING"}
        and ttl.get("AttributeName") == TTL_ATTRIBUTE_NAME
    ):
        return
    print(f"Enabling DynamoDB TTL on {TTL_ATTRIBUTE_NAME}")
    client.update_time_to_live(
        TableName=table_name,
        TimeToLiveSpecification={
            "Enabled": True,
            "AttributeName": TTL_ATTRIBUTE_NAME,
        },
    )


def enable_point_in_time_recovery(client: Any, table_name: str) -> None:
    response = client.describe_continuous_backups(TableName=table_name)
    status = response["ContinuousBackupsDescription"][
        "PointInTimeRecoveryDescription"
    ]["PointInTimeRecoveryStatus"]
    if status == "ENABLED":
        return
    print("Enabling DynamoDB point-in-time recovery")
    client.update_continuous_backups(
        TableName=table_name,
        PointInTimeRecoverySpecification={
            "PointInTimeRecoveryEnabled": True,
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Provision the shared workshop DynamoDB memory table."
    )
    parser.add_argument("--table-name", required=True)
    parser.add_argument("--vector-index-name", default="vector_index")
    parser.add_argument("--kms-key-arn", required=True)
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--profile")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    args = parser.parse_args()

    session = boto3.Session(
        profile_name=args.profile,
        region_name=args.region,
    )
    client = session.client("dynamodb", region_name=args.region)

    try:
        table = table_description(client, args.table_name)
        if table is None:
            create_table(
                client,
                table_name=args.table_name,
                vector_index_name=args.vector_index_name,
                kms_key_arn=args.kms_key_arn,
            )
        else:
            print(f"Reusing existing DynamoDB memory table {args.table_name}")
            validate_table_shape(
                table,
                table_name=args.table_name,
                vector_index_name=args.vector_index_name,
            )
            ensure_kms_key(
                client,
                table=table,
                table_name=args.table_name,
                kms_key_arn=args.kms_key_arn,
            )

        table = wait_until_ready(
            client,
            table_name=args.table_name,
            vector_index_name=args.vector_index_name,
            kms_key_arn=args.kms_key_arn,
            timeout_seconds=args.timeout_seconds,
        )
        validate_table_shape(
            table,
            table_name=args.table_name,
            vector_index_name=args.vector_index_name,
        )
        enable_ttl(client, args.table_name)
        enable_point_in_time_recovery(client, args.table_name)
    except (ClientError, RuntimeError, TimeoutError) as error:
        print(f"Memory table provisioning failed: {error}")
        return 1

    print(
        f"Memory table ready: {args.table_name} "
        f"(vector index: {args.vector_index_name})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
