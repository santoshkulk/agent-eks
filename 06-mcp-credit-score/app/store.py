"""Small conditional-write item store for audit, execution, and ledger records.

``DynamoDBStorage`` (used for sessions and memory) has no conditional writes, so
append-only audit records, the per-session lease, and the tool ledger use this
helper on the same table. Items live under ``pk=user/<actor_id>`` with the
``audit/``, ``exec/``, and ``ledger/`` sort-key prefixes. It needs only
``dynamodb:GetItem``, ``PutItem``, and ``Query``, which the pod role already has.
"""

import threading
from typing import Any, Protocol

import boto3
from botocore.exceptions import ClientError

Attrs = dict[str, str | int | float | bool]


class ItemStore(Protocol):
    def put(
        self,
        pk: str,
        sk: str,
        attrs: Attrs,
        *,
        if_absent: bool = False,
        expect: Attrs | None = None,
    ) -> bool:
        """Write an item. Return False when a condition fails."""
        ...

    def get(self, pk: str, sk: str) -> Attrs | None: ...

    def query(self, pk: str, sk_prefix: str) -> list[Attrs]:
        """Return items whose sort key starts with ``sk_prefix``, ordered by ``sk``."""
        ...


def _marshal(value: str | int | float | bool) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, (int, float)):
        return {"N": str(value)}
    return {"S": value}


def _unmarshal(attribute: dict[str, Any]) -> str | int | float | bool:
    if "S" in attribute:
        return attribute["S"]
    if "BOOL" in attribute:
        return attribute["BOOL"]
    number = attribute["N"]
    return int(number) if number.lstrip("-").isdigit() else float(number)


class DynamoItemStore:
    def __init__(self, table_name: str, region_name: str, client: Any = None) -> None:
        self._table_name = table_name
        self._client = client or boto3.client("dynamodb", region_name=region_name)

    def put(
        self,
        pk: str,
        sk: str,
        attrs: Attrs,
        *,
        if_absent: bool = False,
        expect: Attrs | None = None,
    ) -> bool:
        item = {"pk": {"S": pk}, "sk": {"S": sk}}
        item.update({name: _marshal(value) for name, value in attrs.items()})
        kwargs: dict[str, Any] = {"TableName": self._table_name, "Item": item}
        if if_absent:
            kwargs["ConditionExpression"] = "attribute_not_exists(pk)"
        elif expect:
            names = {f"#e{index}": name for index, name in enumerate(expect)}
            values = {
                f":e{index}": _marshal(value) for index, value in enumerate(expect.values())
            }
            kwargs["ConditionExpression"] = " AND ".join(
                f"#e{index} = :e{index}" for index in range(len(expect))
            )
            kwargs["ExpressionAttributeNames"] = names
            kwargs["ExpressionAttributeValues"] = values
        try:
            self._client.put_item(**kwargs)
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def get(self, pk: str, sk: str) -> Attrs | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key={"pk": {"S": pk}, "sk": {"S": sk}},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item:
            return None
        return {name: _unmarshal(value) for name, value in item.items()}

    def query(self, pk: str, sk_prefix: str) -> list[Attrs]:
        items: list[Attrs] = []
        start_key: dict[str, Any] | None = None
        while True:
            kwargs: dict[str, Any] = {
                "TableName": self._table_name,
                "KeyConditionExpression": "pk = :pk AND begins_with(sk, :sk)",
                "ExpressionAttributeValues": {
                    ":pk": {"S": pk},
                    ":sk": {"S": sk_prefix},
                },
                "ConsistentRead": True,
            }
            if start_key:
                kwargs["ExclusiveStartKey"] = start_key
            response = self._client.query(**kwargs)
            items.extend(
                {name: _unmarshal(value) for name, value in item.items()}
                for item in response.get("Items", [])
            )
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                return items


class InMemoryItemStore:
    """Thread-safe store with the same conditional semantics, for tests and local runs."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], Attrs] = {}
        self._lock = threading.Lock()

    def put(
        self,
        pk: str,
        sk: str,
        attrs: Attrs,
        *,
        if_absent: bool = False,
        expect: Attrs | None = None,
    ) -> bool:
        with self._lock:
            current = self._items.get((pk, sk))
            if if_absent and current is not None:
                return False
            if expect and (
                current is None
                or any(current.get(name) != value for name, value in expect.items())
            ):
                return False
            self._items[(pk, sk)] = {"pk": pk, "sk": sk, **attrs}
            return True

    def get(self, pk: str, sk: str) -> Attrs | None:
        with self._lock:
            item = self._items.get((pk, sk))
            return dict(item) if item else None

    def query(self, pk: str, sk_prefix: str) -> list[Attrs]:
        with self._lock:
            return [
                dict(item)
                for (item_pk, item_sk), item in sorted(self._items.items())
                if item_pk == pk and item_sk.startswith(sk_prefix)
            ]
