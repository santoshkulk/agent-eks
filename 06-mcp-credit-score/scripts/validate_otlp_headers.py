"""Validate the encoded Langfuse OTLP headers stored in Kubernetes."""

from __future__ import annotations

import base64
import binascii
import sys


def validate_encoded_headers(encoded_headers: str) -> bool:
    """Return whether a Kubernetes Secret value has usable Langfuse headers."""
    try:
        raw_headers = base64.b64decode(
            encoded_headers.strip(),
            validate=True,
        ).decode("utf-8")
        headers: dict[str, str] = {}
        for pair in raw_headers.split(","):
            key, separator, value = pair.strip().partition("=")
            if separator:
                headers[key.strip()] = value.strip()

        authorization = headers.get("Authorization", "")
        if not authorization.startswith("Basic "):
            return False
        credentials = base64.b64decode(
            authorization.removeprefix("Basic "),
            validate=True,
        ).decode("utf-8")
        public_key, separator, secret_key = credentials.partition(":")
        return bool(
            separator
            and public_key.strip()
            and secret_key.strip()
            and headers.get("x-langfuse-ingestion-version") == "4"
        )
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False


def main() -> int:
    """Read one encoded Secret value from standard input and validate it."""
    if validate_encoded_headers(sys.stdin.read()):
        return 0
    print("Langfuse OTLP headers are invalid.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
