import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request


DEFAULT_NAMESPACE = "mortgage-assistant"
DEFAULT_SERVICE = "mortgage-assistant"
DEFAULT_SECRET = "mortgage-assistant-api-key"


def kubectl_jsonpath(
    resource: str,
    name: str,
    namespace: str,
    jsonpath: str,
) -> str:
    command = [
        "kubectl",
        "get",
        resource,
        name,
        "--namespace",
        namespace,
        "--output",
        f"jsonpath={jsonpath}",
    ]

    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise RuntimeError("kubectl is not installed or is not on PATH") from error
    except subprocess.CalledProcessError as error:
        message = error.stderr.strip() or error.stdout.strip()
        raise RuntimeError(f"kubectl failed: {message}") from error

    value = result.stdout.strip()
    if not value:
        raise RuntimeError(
            f"kubectl returned no value for {resource}/{name} using {jsonpath}"
        )
    return value


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


def invoke(api_url: str, api_key: str, prompt: str, timeout: int) -> dict:
    request = urllib.request.Request(
        url=f"{api_url.rstrip('/')}/invoke",
        data=json.dumps({"prompt": prompt}).encode("utf-8"),
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
        description="Send a prompt to the mortgage assistant running on EKS."
    )
    parser.add_argument(
        "--prompt",
        "-p",
        required=True,
        help="Prompt to send to the mortgage assistant.",
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
        help="Print the complete JSON response instead of only the answer.",
    )
    args = parser.parse_args()

    try:
        api_url = args.url or discover_api_url(args.namespace, args.service)
        api_key = args.api_key or discover_api_key(args.namespace, args.secret)
        result = invoke(api_url, api_key, args.prompt, args.timeout)
    except RuntimeError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(result.get("response", json.dumps(result)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
