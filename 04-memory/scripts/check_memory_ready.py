"""Print "<table status> <vector index status>" for the memory table.

The deploy script uses this instead of `aws dynamodb describe-table --query
Table.VectorIndexes...`: older AWS CLI v2 releases do not return vector indexes at
all, which made the check report a missing index on a healthy table. boto3 is
pinned in this project's lock file.
"""

import argparse

import boto3


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table-name", required=True)
    parser.add_argument("--vector-index-name", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--profile")
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    table = session.client("dynamodb").describe_table(TableName=args.table_name)["Table"]
    index = next(
        (
            item
            for item in table.get("VectorIndexes", [])
            if item.get("IndexName") == args.vector_index_name
        ),
        None,
    )
    print(table.get("TableStatus", "UNKNOWN"), index.get("IndexStatus", "MISSING") if index else "MISSING")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
