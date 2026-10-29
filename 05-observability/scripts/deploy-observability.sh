#!/usr/bin/env bash
set -euo pipefail

# Lab 5: supervisor-only memory plus OpenTelemetry/Langfuse tracing.
# Workshop Studio owns the Langfuse infrastructure; this script discovers its
# endpoint and credentials and creates the participant namespace Secret.

MODULE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}"
PROFILE=""
SERVICE_ACCESS_CIDR=""
PROMPT="What is the balance on customer ID 123456's mortgage?"
SESSION_TTL_SECONDS="604800"
MODEL_ID="us.anthropic.claude-sonnet-4-6"
MEMORY_EMBEDDING_MODEL_ID="amazon.titan-embed-text-v2:0"
TELEMETRY_MASK_CONTENT="false"
IMAGE_URI_OVERRIDE=""
CLUSTER_PARAMETER_NAME="/workshop/mortgage-assistant/eks/cluster-name"
REPOSITORY_PARAMETER_NAME="/workshop/mortgage-assistant/ecr/repository-uri"
MEMORY_TABLE_PARAMETER_NAME="/workshop/mortgage-assistant/memory/table-name"
MEMORY_VECTOR_INDEX_PARAMETER_NAME="/workshop/mortgage-assistant/memory/vector-index-name"
KB_PARAMETER_NAME="/workshop/mortgage-assistant/bedrock/knowledge-base-id"
LANGFUSE_OTLP_ENDPOINT_PARAMETER_NAME="/workshop/mortgage-assistant/langfuse/otlp-endpoint"
LANGFUSE_URL_PARAMETER_NAME="/workshop/mortgage-assistant/langfuse/url"

usage() {
  cat <<'EOF'
Usage: 05-observability/scripts/deploy-observability.sh [options]

Options:
  --region REGION                    AWS Region (default: us-west-2).
  --profile PROFILE                  AWS CLI profile; omit for the default profile.
  --service-access-cidr CIDR         CIDR allowed to invoke the API.
  --session-ttl-seconds N            Latest session snapshot retention (default: 604800).
  --prompt TEXT                      Prompt used for the one deployment smoke test.
  --telemetry-mask-content           Redact prompt/response attributes before export.
  --image-uri URI                    Reuse an ECR image and skip build and push.
  -h, --help                         Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region) REGION="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --service-access-cidr) SERVICE_ACCESS_CIDR="$2"; shift 2 ;;
    --session-ttl-seconds) SESSION_TTL_SECONDS="$2"; shift 2 ;;
    --prompt) PROMPT="$2"; shift 2 ;;
    --telemetry-mask-content) TELEMETRY_MASK_CONTENT="true"; shift ;;
    --image-uri) IMAGE_URI_OVERRIDE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

for command_name in aws curl docker grep kubectl openssl python3 sed uv; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Required command not found: $command_name" >&2
    exit 1
  fi
done

if [[ ! "$SESSION_TTL_SECONDS" =~ ^[0-9]+$ ]] ||
  [[ "$SESSION_TTL_SECONDS" -lt 3600 ]]; then
  echo "--session-ttl-seconds must be an integer of at least 3600." >&2
  exit 2
fi
AWS_OPTIONS=(--region "$REGION")
if [[ -n "$PROFILE" ]]; then
  AWS_OPTIONS+=(--profile "$PROFILE")
fi

aws_cli() {
  aws "${AWS_OPTIONS[@]}" "$@"
}

ssm_parameter() {
  local parameter_name="$1"
  local value
  value="$(aws_cli ssm get-parameter \
    --name "$parameter_name" \
    --query 'Parameter.Value' \
    --output text)"
  if [[ -z "$value" || "$value" == "None" ]]; then
    echo "SSM parameter $parameter_name is missing or empty in $REGION." >&2
    return 1
  fi
  printf '%s' "$value"
}

langfuse_otel_headers() {
  kubectl get deployment langfuse-web \
    --namespace langfuse \
    --output json |
    python3 -c '
import base64
import json
import sys

deployment = json.load(sys.stdin)
environment = {}
for container in deployment["spec"]["template"]["spec"]["containers"]:
    for item in container.get("env", []):
        if "value" in item:
            environment[item["name"]] = item["value"]
public_key = environment.get("LANGFUSE_INIT_PROJECT_PUBLIC_KEY", "").strip()
secret_key = environment.get("LANGFUSE_INIT_PROJECT_SECRET_KEY", "").strip()
if not public_key or not secret_key:
    sys.exit("The Workshop Langfuse deployment does not expose bootstrap project keys.")
auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
sys.stdout.write(f"Authorization=Basic {auth},x-langfuse-ingestion-version=4")
'
}

if [[ -z "$SERVICE_ACCESS_CIDR" ]]; then
  PUBLIC_IP="$(curl --fail --silent --show-error https://checkip.amazonaws.com | tr -d '[:space:]')"
  if [[ ! "$PUBLIC_IP" =~ ^([0-9]{1,3}[.]){3}[0-9]{1,3}$ ]]; then
    echo "Could not detect an IPv4 address. Pass --service-access-cidr." >&2
    exit 1
  fi
  SERVICE_ACCESS_CIDR="${PUBLIC_IP}/32"
fi

CLUSTER_NAME="$(ssm_parameter "$CLUSTER_PARAMETER_NAME")"
REPOSITORY_URI="$(ssm_parameter "$REPOSITORY_PARAMETER_NAME")"
MEMORY_TABLE_NAME="$(ssm_parameter "$MEMORY_TABLE_PARAMETER_NAME")"
MEMORY_VECTOR_INDEX_NAME="$(ssm_parameter "$MEMORY_VECTOR_INDEX_PARAMETER_NAME")"
KNOWLEDGE_BASE_ID="$(ssm_parameter "$KB_PARAMETER_NAME")"
LANGFUSE_OTLP_ENDPOINT="$(ssm_parameter "$LANGFUSE_OTLP_ENDPOINT_PARAMETER_NAME")"
LANGFUSE_URL="$(ssm_parameter "$LANGFUSE_URL_PARAMETER_NAME")"

if [[ -z "$KNOWLEDGE_BASE_ID" ]]; then
  echo "The discovered Knowledge Base ID is empty." >&2
  exit 1
fi

MEMORY_CHECK_OPTIONS=(
  --table-name "$MEMORY_TABLE_NAME"
  --vector-index-name "$MEMORY_VECTOR_INDEX_NAME"
  --region "$REGION"
)
if [[ -n "$PROFILE" ]]; then
  MEMORY_CHECK_OPTIONS+=(--profile "$PROFILE")
fi
MEMORY_CHECK_OUTPUT="$(uv run --project "$MODULE_DIR" --frozen python \
  "$MODULE_DIR/scripts/check_memory_ready.py" "${MEMORY_CHECK_OPTIONS[@]}")" || {
  echo "Could not read the memory table $MEMORY_TABLE_NAME." >&2
  exit 1
}
read -r MEMORY_STATUS MEMORY_INDEX_STATUS <<<"$MEMORY_CHECK_OUTPUT"
if [[ "$MEMORY_STATUS" != "ACTIVE" || "$MEMORY_INDEX_STATUS" != "ACTIVE" ]]; then
  echo "Workshop Studio memory storage is not ready." >&2
  echo "  Table: $MEMORY_STATUS" >&2
  echo "  Vector index: $MEMORY_INDEX_STATUS" >&2
  exit 1
fi

echo "Configuring kubectl for $CLUSTER_NAME"
aws_cli eks update-kubeconfig --name "$CLUSTER_NAME" --alias "$CLUSTER_NAME"

if ! kubectl rollout status \
  --namespace kube-system \
  deployment/aws-load-balancer-controller \
  --timeout=2m; then
  echo "AWS Load Balancer Controller is not ready." >&2
  exit 1
fi

echo "Reading Langfuse project credentials from the in-cluster deployment"
if ! langfuse_otel_headers >/dev/null; then
  echo "Workshop Studio Langfuse credentials are not ready." >&2
  exit 1
fi

ACCOUNT_ID="$(aws_cli sts get-caller-identity --query Account --output text)"
IMAGE_TAG="lab05-agent-$(date -u +%Y%m%d%H%M%S)"
IMAGE_URI="${REPOSITORY_URI}:${IMAGE_TAG}"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

if [[ -n "$IMAGE_URI_OVERRIDE" ]]; then
  IMAGE_URI="$IMAGE_URI_OVERRIDE"
  echo "Reusing image $IMAGE_URI (skipping build and push)"
else
  echo "Logging Docker into $REGISTRY"
  aws_cli ecr get-login-password |
    docker login --username AWS --password-stdin "$REGISTRY"
  echo "Building $IMAGE_URI for EKS x86_64 nodes"
  if docker buildx version >/dev/null 2>&1; then
    docker buildx build --platform linux/amd64 --tag "$IMAGE_URI" --load "$MODULE_DIR"
  else
    docker build --platform linux/amd64 --tag "$IMAGE_URI" "$MODULE_DIR"
  fi
  docker push "$IMAGE_URI"
fi

kubectl apply -f "$MODULE_DIR/k8s/base.yaml"

if [[ "${MORTGAGE_API_KEY+x}" == "x" ]]; then
  if [[ -z "${MORTGAGE_API_KEY//[[:space:]]/}" ]]; then
    echo "The mortgage-assistant API key must not be empty." >&2
    exit 1
  fi
  printf '%s' "$MORTGAGE_API_KEY" |
    kubectl create secret generic mortgage-assistant-api-key \
      --namespace mortgage-assistant \
      --from-file=api-key=/dev/stdin \
      --dry-run=client \
      --output=yaml |
    kubectl apply -f -
  unset MORTGAGE_API_KEY
elif ! kubectl get secret mortgage-assistant-api-key \
  --namespace mortgage-assistant >/dev/null 2>&1; then
  openssl rand -hex 32 | tr -d '\n' |
    kubectl create secret generic mortgage-assistant-api-key \
      --namespace mortgage-assistant \
      --from-file=api-key=/dev/stdin \
      --dry-run=client \
      --output=yaml |
    kubectl apply -f -
fi

# Stream the Basic-auth header into the Secret without putting it in argv or a file.
langfuse_otel_headers |
  kubectl create secret generic langfuse-otel-auth \
    --namespace mortgage-assistant \
    --from-file=otlp-headers=/dev/stdin \
    --dry-run=client \
    --output=yaml |
  kubectl apply -f -

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/mortgage-lab05.XXXXXX")"
SERVICE_MANIFEST="$WORK_DIR/service.yaml"
SMOKE_STATE_FILE="$WORK_DIR/client-state.json"
trap 'rm -f "$SERVICE_MANIFEST" "$SMOKE_STATE_FILE"; rmdir "$WORK_DIR" 2>/dev/null || true' EXIT

for template_variable in \
  IMAGE_URI REGION MODEL_ID KB_PARAMETER_NAME MEMORY_TABLE_NAME \
  MEMORY_VECTOR_INDEX_NAME MEMORY_EMBEDDING_MODEL_ID SESSION_TTL_SECONDS \
  LANGFUSE_OTLP_ENDPOINT TELEMETRY_MASK_CONTENT SERVICE_ACCESS_CIDR; do
  template_value="${!template_variable}"
  case "$template_value" in
    *'&'*|*'|'*|*'\'*|*$'\n'*|*$'\r'*)
      echo "$template_variable contains a value that cannot be rendered safely." >&2
      exit 1
      ;;
  esac
done

sed \
  -e "s|__IMAGE_URI__|$IMAGE_URI|g" \
  -e "s|__AWS_REGION__|$REGION|g" \
  -e "s|__MODEL_ID__|$MODEL_ID|g" \
  -e "s|__KB_PARAMETER_NAME__|$KB_PARAMETER_NAME|g" \
  -e "s|__MEMORY_TABLE_NAME__|$MEMORY_TABLE_NAME|g" \
  -e "s|__MEMORY_VECTOR_INDEX_NAME__|$MEMORY_VECTOR_INDEX_NAME|g" \
  -e "s|__MEMORY_EMBEDDING_MODEL_ID__|$MEMORY_EMBEDDING_MODEL_ID|g" \
  -e "s|__MEMORY_SESSION_TTL_SECONDS__|$SESSION_TTL_SECONDS|g" \
  -e "s|__OTEL_EXPORTER_OTLP_ENDPOINT__|$LANGFUSE_OTLP_ENDPOINT|g" \
  -e "s|__TELEMETRY_MASK_CONTENT__|$TELEMETRY_MASK_CONTENT|g" \
  -e "s|__SERVICE_ACCESS_CIDR__|$SERVICE_ACCESS_CIDR|g" \
  "$MODULE_DIR/k8s/service.template.yaml" > "$SERVICE_MANIFEST"

if grep -Eq '__[A-Z0-9_]+__' "$SERVICE_MANIFEST"; then
  echo "Rendered Lab 5 manifest contains unresolved template placeholders." >&2
  exit 1
fi

kubectl apply -f "$SERVICE_MANIFEST"

if ! kubectl rollout status \
  --namespace mortgage-assistant \
  deployment/mortgage-assistant \
  --timeout=20m; then
  kubectl describe deployment mortgage-assistant --namespace mortgage-assistant || true
  kubectl get pods --namespace mortgage-assistant --output wide || true
  kubectl logs --namespace mortgage-assistant deployment/mortgage-assistant --tail=200 || true
  exit 1
fi

echo "Waiting for the Network Load Balancer endpoint"
SERVICE_ENDPOINT=""
for _ in $(seq 1 60); do
  SERVICE_ENDPOINT="$(kubectl get service mortgage-assistant \
    --namespace mortgage-assistant \
    --output jsonpath='{.status.loadBalancer.ingress[0].hostname}')"
  if [[ -n "$SERVICE_ENDPOINT" ]]; then
    break
  fi
  sleep 10
done
if [[ -z "$SERVICE_ENDPOINT" ]]; then
  kubectl describe service mortgage-assistant --namespace mortgage-assistant
  echo "Timed out waiting for the Network Load Balancer endpoint." >&2
  exit 1
fi

echo "Waiting for the Lab 05 mortgage API readiness endpoint"
READY_STREAK=0
for _ in $(seq 1 60); do
  if curl --fail --silent --show-error \
    --connect-timeout 5 \
    --max-time 10 \
    "http://${SERVICE_ENDPOINT}/health/ready" >/dev/null 2>&1; then
    READY_STREAK=$((READY_STREAK + 1))
    if [[ "$READY_STREAK" -ge 2 ]]; then
      break
    fi
  else
    READY_STREAK=0
  fi
  sleep 10
done
if [[ "$READY_STREAK" -lt 2 ]]; then
  kubectl get pods --namespace mortgage-assistant --output wide
  kubectl logs --namespace mortgage-assistant deployment/mortgage-assistant --tail=200 || true
  echo "Lab 05 mortgage API did not become ready." >&2
  exit 1
fi

SMOKE_ACTOR="deployment-smoke-test"
SMOKE_SESSION="session-$(date -u +%Y%m%d%H%M%S)"

echo
echo "Lab 05 smoke-test response:"
SMOKE_RESPONSE="$(uv run --project "$MODULE_DIR" --frozen python \
  "$MODULE_DIR/app/invoke_eks.py" \
  --region "$REGION" \
  --url "http://${SERVICE_ENDPOINT}" \
  --namespace mortgage-assistant \
  --secret mortgage-assistant-api-key \
  --actor-id "$SMOKE_ACTOR" \
  --session-id "$SMOKE_SESSION" \
  --state-file "$SMOKE_STATE_FILE" \
  --prompt "$PROMPT" \
  --timeout 300 \
  --json)"
echo "$SMOKE_RESPONSE"
read -r SMOKE_REQUEST_ID SMOKE_TRACE_ID < <(
  printf '%s' "$SMOKE_RESPONSE" | python3 -c '
import json
import re
import sys

response = json.load(sys.stdin)
required = ("request_id", "actor_id", "session_id", "response", "duration_ms", "trace_id")
missing = [field for field in required if field not in response]
if missing:
    sys.exit("Lab 5 smoke response is missing: " + ", ".join(missing))
if response["actor_id"] != "deployment-smoke-test":
    sys.exit("Lab 5 smoke response returned the wrong actor_id")
if not isinstance(response.get("duration_ms"), int):
    sys.exit("Lab 5 smoke response duration_ms is not an integer")
trace_id = response.get("trace_id")
if not isinstance(trace_id, str) or not re.fullmatch(r"[0-9a-f]{32}", trace_id):
    sys.exit("Lab 5 smoke response did not include a valid trace_id")
print(response["request_id"], trace_id)
'
)

cat <<EOF

Lab 05 completed.
  EKS cluster: $CLUSTER_NAME
  Memory table: $MEMORY_TABLE_NAME
  Vector index: $MEMORY_VECTOR_INDEX_NAME
  Image: $IMAGE_URI
  Mortgage-assistant API endpoint: http://${SERVICE_ENDPOINT}
  Langfuse OTLP endpoint: $LANGFUSE_OTLP_ENDPOINT
  Langfuse UI: $LANGFUSE_URL
  Smoke-test trace ID: $SMOKE_TRACE_ID
  Smoke-test request ID: $SMOKE_REQUEST_ID (ephemeral correlation; session $SMOKE_SESSION)

Credential values were streamed through stdin or read by the smoke client in-process.
They were not printed, placed in child-process arguments, or rendered to files.

Try it (from 05-observability):
  uv run app/invoke_eks.py --region $REGION --prompt \
    "What is the balance on customer ID 123456's mortgage?"

To change settings without rebuilding, rerun with: --image-uri $IMAGE_URI
EOF
