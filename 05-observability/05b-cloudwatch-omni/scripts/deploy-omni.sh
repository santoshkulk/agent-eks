#!/usr/bin/env bash
set -euo pipefail

MODULE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}"
PROFILE=""
TELEMETRY_BACKEND="both"
PROMPT="What are the benefits of a 15-year mortgage?"
SESSION_TTL_SECONDS="604800"
MODEL_ID="us.anthropic.claude-sonnet-4-6"
MEMORY_EMBEDDING_MODEL_ID="amazon.titan-embed-text-v2:0"
TELEMETRY_MASK_CONTENT="false"
FAULT_INJECTION_ENABLED="false"
FAULT_INJECTION_TOOL="get_mortgage_details"
FAULT_INJECTION_MODE="delay"
FAULT_INJECTION_DELAY_SECONDS="5"
LOCAL_PORT="18080"
CLUSTER_PARAMETER_NAME="/workshop/mortgage-assistant/eks/cluster-name"
REPOSITORY_PARAMETER_NAME="/workshop/mortgage-assistant/ecr/repository-uri"
MEMORY_TABLE_PARAMETER_NAME="/workshop/mortgage-assistant/memory/table-name"
MEMORY_VECTOR_INDEX_PARAMETER_NAME="/workshop/mortgage-assistant/memory/vector-index-name"
KB_PARAMETER_NAME="/workshop/mortgage-assistant/bedrock/knowledge-base-id"
TRACE_LOG_GROUP_PARAMETER_NAME="/workshop/mortgage-assistant/cloudwatch/trace-log-group"
OMNI_DOMAIN_URL_PARAMETER_NAME="/workshop/mortgage-assistant/cloudwatch/omni-domain-url"
LANGFUSE_OTLP_ENDPOINT_PARAMETER_NAME="/workshop/mortgage-assistant/langfuse/otlp-endpoint"
LANGFUSE_URL_PARAMETER_NAME="/workshop/mortgage-assistant/langfuse/url"

usage() {
  cat <<'EOT'
Usage: 05-observability/05b-cloudwatch-omni/scripts/deploy-omni.sh [options]

Deploys a second Deployment, mortgage-assistant-omni, next to the Lab 5a
application. Lab 5a is not modified.

Options:
  --region REGION                    AWS Region (default: us-west-2).
  --profile PROFILE                  AWS CLI profile; omit to use the default profile.
  --telemetry-backend MODE           "both" (CloudWatch Omni and Langfuse, default)
                                     or "cloudwatch" (CloudWatch Omni only).
  --prompt TEXT                      Prompt used for the deployment smoke test.
  --session-ttl-seconds N            Short-term session retention (default: 604800).
  --telemetry-mask-content           Redact prompt/response attributes on the Langfuse path.
  --fault-injection-enabled          Enable the fault-injection exercise at deploy time.
  --fault-injection-tool NAME        Tool name to target (default: get_mortgage_details).
  --fault-injection-mode MODE        "delay" or "error" (default: delay).
  --fault-injection-delay-seconds N  Delay applied in "delay" mode (default: 5).
  -h, --help                         Show this help.
EOT
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region) REGION="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --telemetry-backend) TELEMETRY_BACKEND="$2"; shift 2 ;;
    --prompt) PROMPT="$2"; shift 2 ;;
    --session-ttl-seconds) SESSION_TTL_SECONDS="$2"; shift 2 ;;
    --telemetry-mask-content) TELEMETRY_MASK_CONTENT="true"; shift ;;
    --fault-injection-enabled) FAULT_INJECTION_ENABLED="true"; shift ;;
    --fault-injection-tool) FAULT_INJECTION_TOOL="$2"; shift 2 ;;
    --fault-injection-mode) FAULT_INJECTION_MODE="$2"; shift 2 ;;
    --fault-injection-delay-seconds) FAULT_INJECTION_DELAY_SECONDS="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

for command_name in aws curl docker kubectl openssl python3 sed; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Required command not found: $command_name" >&2
    exit 1
  fi
done

if [[ "$TELEMETRY_BACKEND" != "both" && "$TELEMETRY_BACKEND" != "cloudwatch" ]]; then
  echo "--telemetry-backend must be 'both' or 'cloudwatch'." >&2
  exit 2
fi
if [[ ! "$SESSION_TTL_SECONDS" =~ ^[0-9]+$ ]] || [[ "$SESSION_TTL_SECONDS" -lt 3600 ]]; then
  echo "--session-ttl-seconds must be an integer of at least 3600." >&2
  exit 2
fi
if [[ "$FAULT_INJECTION_MODE" != "delay" && "$FAULT_INJECTION_MODE" != "error" ]]; then
  echo "--fault-injection-mode must be 'delay' or 'error'." >&2
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
  local value
  value="$(aws_cli ssm get-parameter --name "$1" --query 'Parameter.Value' --output text)"
  if [[ -z "$value" || "$value" == "None" ]]; then
    echo "SSM parameter $1 is missing or empty in $REGION." >&2
    return 1
  fi
  printf '%s' "$value"
}

CLUSTER_NAME="$(ssm_parameter "$CLUSTER_PARAMETER_NAME")"
REPOSITORY_URI="$(ssm_parameter "$REPOSITORY_PARAMETER_NAME")"
MEMORY_TABLE_NAME="$(ssm_parameter "$MEMORY_TABLE_PARAMETER_NAME")"
MEMORY_VECTOR_INDEX_NAME="$(ssm_parameter "$MEMORY_VECTOR_INDEX_PARAMETER_NAME")"
TRACE_LOG_GROUP="$(ssm_parameter "$TRACE_LOG_GROUP_PARAMETER_NAME")"
OMNI_DOMAIN_URL="$(ssm_parameter "$OMNI_DOMAIN_URL_PARAMETER_NAME")"

# Transaction Search must be on, or Omni cannot list the traces.
TRACE_DESTINATION="$(aws_cli xray get-trace-segment-destination \
  --query '[Destination,Status]' --output text)"
if [[ "$TRACE_DESTINATION" != "CloudWatchLogs"$'\t'"ACTIVE" ]]; then
  echo "Transaction Search is not active ($TRACE_DESTINATION). Wait a few minutes and retry." >&2
  exit 1
fi

LANGFUSE_OTLP_ENDPOINT=""
LANGFUSE_URL=""
if [[ "$TELEMETRY_BACKEND" == "both" ]]; then
  LANGFUSE_OTLP_ENDPOINT="$(ssm_parameter "$LANGFUSE_OTLP_ENDPOINT_PARAMETER_NAME")"
  LANGFUSE_URL="$(ssm_parameter "$LANGFUSE_URL_PARAMETER_NAME")"
fi

MEMORY_STATUS="$(aws_cli dynamodb describe-table --table-name "$MEMORY_TABLE_NAME" \
  --query 'Table.TableStatus' --output text)"
if [[ "$MEMORY_STATUS" != "ACTIVE" ]]; then
  echo "Workshop Studio memory storage is not ready ($MEMORY_STATUS)." >&2
  exit 1
fi

echo "Configuring kubectl for $CLUSTER_NAME"
aws_cli eks update-kubeconfig --name "$CLUSTER_NAME" --alias "$CLUSTER_NAME"

# Lab 5a created these. They are reused read-only, never recreated here.
if ! kubectl get secret mortgage-assistant-api-key --namespace mortgage-assistant >/dev/null 2>&1; then
  echo "Secret mortgage-assistant-api-key not found. Complete Lab 5a first." >&2
  exit 1
fi
if [[ "$TELEMETRY_BACKEND" == "both" ]] &&
  ! kubectl get secret langfuse-otel-auth --namespace mortgage-assistant >/dev/null 2>&1; then
  echo "Secret langfuse-otel-auth not found. Complete Lab 5a first, or use --telemetry-backend cloudwatch." >&2
  exit 1
fi
API_KEY="$(kubectl get secret mortgage-assistant-api-key --namespace mortgage-assistant \
  --output jsonpath='{.data.api-key}' | base64 --decode)"

# X-Ray delivers each span batch into this stream.
aws_cli logs create-log-stream \
  --log-group-name "$TRACE_LOG_GROUP" \
  --log-stream-name mortgage-assistant-omni 2>/dev/null || true

ACCOUNT_ID="$(aws_cli sts get-caller-identity --query Account --output text)"
IMAGE_TAG="lab05b-$(date -u +%Y%m%d%H%M%S)"
IMAGE_URI="${REPOSITORY_URI}:${IMAGE_TAG}"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

echo "Logging Docker into $REGISTRY"
aws_cli ecr get-login-password | docker login --username AWS --password-stdin "$REGISTRY"

echo "Building $IMAGE_URI for EKS x86_64 nodes"
if docker buildx version >/dev/null 2>&1; then
  docker buildx build --platform linux/amd64 --tag "$IMAGE_URI" --load "$MODULE_DIR"
else
  docker build --platform linux/amd64 --tag "$IMAGE_URI" "$MODULE_DIR"
fi
docker push "$IMAGE_URI"

# Namespace and ServiceAccount are identical to Lab 5a; applying is a no-op.
kubectl apply -f "$MODULE_DIR/k8s/base.yaml"

SERVICE_MANIFEST="$(mktemp "${TMPDIR:-/tmp}/mortgage-omni-service.XXXXXX.yaml")"
PORT_FORWARD_PID=""
cleanup_temp() {
  rm -f "$SERVICE_MANIFEST"
  if [[ -n "$PORT_FORWARD_PID" ]]; then
    kill "$PORT_FORWARD_PID" >/dev/null 2>&1 || true
  fi
}
trap cleanup_temp EXIT

sed \
  -e "s|__IMAGE_URI__|$IMAGE_URI|g" \
  -e "s|__AWS_REGION__|$REGION|g" \
  -e "s|__MODEL_ID__|$MODEL_ID|g" \
  -e "s|__KB_PARAMETER_NAME__|$KB_PARAMETER_NAME|g" \
  -e "s|__MEMORY_TABLE_NAME__|$MEMORY_TABLE_NAME|g" \
  -e "s|__MEMORY_VECTOR_INDEX_NAME__|$MEMORY_VECTOR_INDEX_NAME|g" \
  -e "s|__MEMORY_EMBEDDING_MODEL_ID__|$MEMORY_EMBEDDING_MODEL_ID|g" \
  -e "s|__MEMORY_SESSION_TTL_SECONDS__|$SESSION_TTL_SECONDS|g" \
  -e "s|__TRACE_LOG_GROUP__|$TRACE_LOG_GROUP|g" \
  -e "s|__LANGFUSE_OTLP_ENDPOINT__|$LANGFUSE_OTLP_ENDPOINT|g" \
  -e "s|__TELEMETRY_MASK_CONTENT__|$TELEMETRY_MASK_CONTENT|g" \
  -e "s|__FAULT_INJECTION_ENABLED__|$FAULT_INJECTION_ENABLED|g" \
  -e "s|__FAULT_INJECTION_TOOL__|$FAULT_INJECTION_TOOL|g" \
  -e "s|__FAULT_INJECTION_MODE__|$FAULT_INJECTION_MODE|g" \
  -e "s|__FAULT_INJECTION_DELAY_SECONDS__|$FAULT_INJECTION_DELAY_SECONDS|g" \
  "$MODULE_DIR/k8s/service.template.yaml" > "$SERVICE_MANIFEST"

kubectl apply -f "$SERVICE_MANIFEST"

if ! kubectl rollout status --namespace mortgage-assistant \
  deployment/mortgage-assistant-omni --timeout=20m; then
  kubectl describe deployment mortgage-assistant-omni --namespace mortgage-assistant || true
  kubectl logs --namespace mortgage-assistant deployment/mortgage-assistant-omni --tail=200 || true
  exit 1
fi

echo "Opening a local tunnel to the in-cluster Service on port $LOCAL_PORT"
kubectl port-forward --namespace mortgage-assistant \
  service/mortgage-assistant-omni "${LOCAL_PORT}:80" >/dev/null 2>&1 &
PORT_FORWARD_PID=$!

for _ in $(seq 1 30); do
  if curl --fail --silent --max-time 5 "http://127.0.0.1:${LOCAL_PORT}/health/ready" >/dev/null; then
    break
  fi
  sleep 5
done
if ! curl --fail --silent --max-time 5 "http://127.0.0.1:${LOCAL_PORT}/health/ready" >/dev/null; then
  kubectl logs --namespace mortgage-assistant deployment/mortgage-assistant-omni --tail=200 || true
  echo "Mortgage API did not become ready." >&2
  exit 1
fi

SMOKE_SESSION="session-$(date -u +%Y%m%d%H%M%S)"
PROMPT_JSON="$(python3 -c \
  'import json, sys; print(json.dumps({"prompt": sys.argv[1], "actor_id": sys.argv[2], "session_id": sys.argv[3]}))' \
  "$PROMPT" "deployment-smoke-test" "$SMOKE_SESSION")"

echo
echo "Smoke-test response:"
SMOKE_RESPONSE="$(curl --fail --silent --show-error --max-time 300 \
  --request POST "http://127.0.0.1:${LOCAL_PORT}/invoke" \
  --header "Authorization: Bearer ${API_KEY}" \
  --header "Content-Type: application/json" \
  --data "$PROMPT_JSON")"
echo "$SMOKE_RESPONSE"

SMOKE_TRACE_ID="$(python3 -c \
  'import json, sys; print(json.loads(sys.argv[1]).get("trace_id") or "")' "$SMOKE_RESPONSE")"

cat <<EOT


Lab 5b completed.
  Image: $IMAGE_URI
  Telemetry backend: $TELEMETRY_BACKEND
  Trace log group: $TRACE_LOG_GROUP
  CloudWatch Omni: $OMNI_DOMAIN_URL
EOT
if [[ -n "$LANGFUSE_URL" ]]; then
  echo "  Langfuse UI: $LANGFUSE_URL"
fi
if [[ -n "$SMOKE_TRACE_ID" ]]; then
  echo "  Smoke-test trace ID: $SMOKE_TRACE_ID"
  echo
  echo "A trace appears under Agent traces in Omni within about five minutes."
else
  echo
  echo "WARNING: the response had no trace_id. Check the pod logs for ADOT startup errors."
fi
cat <<EOT

Invoke more requests through the tunnel:
  kubectl port-forward --namespace mortgage-assistant service/mortgage-assistant-omni ${LOCAL_PORT}:80 &
  uv run app/invoke_eks.py --region $REGION --url http://127.0.0.1:${LOCAL_PORT} \\
    --prompt "What are the benefits of a 15-year mortgage?"
EOT
