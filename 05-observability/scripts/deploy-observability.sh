#!/usr/bin/env bash
set -euo pipefail

MODULE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}"
PROFILE=""
SERVICE_ACCESS_CIDR=""
PROMPT="What are the benefits of a 15-year mortgage?"
SESSION_TTL_SECONDS="604800"
MODEL_ID="us.anthropic.claude-sonnet-4-6"
MEMORY_EMBEDDING_MODEL_ID="amazon.titan-embed-text-v2:0"
TELEMETRY_MASK_CONTENT="false"
FAULT_INJECTION_ENABLED="false"
FAULT_INJECTION_TOOL="get_mortgage_details"
FAULT_INJECTION_MODE="delay"
FAULT_INJECTION_DELAY_SECONDS="5"
CLUSTER_PARAMETER_NAME="/workshop/mortgage-assistant/eks/cluster-name"
REPOSITORY_PARAMETER_NAME="/workshop/mortgage-assistant/ecr/repository-uri"
MEMORY_TABLE_PARAMETER_NAME="/workshop/mortgage-assistant/memory/table-name"
MEMORY_VECTOR_INDEX_PARAMETER_NAME="/workshop/mortgage-assistant/memory/vector-index-name"
KB_PARAMETER_NAME="/workshop/mortgage-assistant/bedrock/knowledge-base-id"
LANGFUSE_OTLP_ENDPOINT_PARAMETER_NAME="/workshop/mortgage-assistant/langfuse/otlp-endpoint"
LANGFUSE_SECRET_ARN_PARAMETER_NAME="/workshop/mortgage-assistant/langfuse/secret-arn"
LANGFUSE_URL_PARAMETER_NAME="/workshop/mortgage-assistant/langfuse/url"
LANGFUSE_INSTANCE_ID_PARAMETER_NAME="/workshop/mortgage-assistant/langfuse/instance-id"

usage() {
  cat <<'EOF'
Usage: 05-observability/scripts/deploy-observability.sh [options]

Options:
  --region REGION                    AWS Region (default: us-west-2).
  --profile PROFILE                  AWS CLI profile; omit to use the default profile.
  --service-access-cidr CIDR         CIDR allowed to invoke the API.
  --session-ttl-seconds N            Short-term session retention (default: 604800).
  --prompt TEXT                      Prompt used for the deployment smoke test.
  --telemetry-mask-content           Redact prompt/response span attributes before export.
  --fault-injection-enabled          Enable the fault-injection exercise at deploy time.
  --fault-injection-tool NAME        Tool name to target (default: get_mortgage_details).
  --fault-injection-mode MODE        "delay" or "error" (default: delay).
  --fault-injection-delay-seconds N  Delay applied in "delay" mode (default: 5).
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

if [[ ! "$SESSION_TTL_SECONDS" =~ ^[0-9]+$ ]] ||
  [[ "$SESSION_TTL_SECONDS" -lt 3600 ]]; then
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

if [[ -z "$SERVICE_ACCESS_CIDR" ]]; then
  PUBLIC_IP="$(curl --fail --silent --show-error https://checkip.amazonaws.com | tr -d '[:space:]')"
  SERVICE_ACCESS_CIDR="${PUBLIC_IP}/32"
fi

CLUSTER_NAME="$(ssm_parameter "$CLUSTER_PARAMETER_NAME")"
REPOSITORY_URI="$(ssm_parameter "$REPOSITORY_PARAMETER_NAME")"
MEMORY_TABLE_NAME="$(ssm_parameter "$MEMORY_TABLE_PARAMETER_NAME")"
MEMORY_VECTOR_INDEX_NAME="$(ssm_parameter "$MEMORY_VECTOR_INDEX_PARAMETER_NAME")"
LANGFUSE_OTLP_ENDPOINT="$(ssm_parameter "$LANGFUSE_OTLP_ENDPOINT_PARAMETER_NAME")"
LANGFUSE_SECRET_ARN="$(ssm_parameter "$LANGFUSE_SECRET_ARN_PARAMETER_NAME")"
LANGFUSE_URL="$(ssm_parameter "$LANGFUSE_URL_PARAMETER_NAME")"

MEMORY_STATUS="$(aws_cli dynamodb describe-table \
  --table-name "$MEMORY_TABLE_NAME" \
  --query 'Table.TableStatus' \
  --output text)"
MEMORY_INDEX_STATUS="$(aws_cli dynamodb describe-table \
  --table-name "$MEMORY_TABLE_NAME" \
  --query "Table.VectorIndexes[?IndexName=='${MEMORY_VECTOR_INDEX_NAME}'].IndexStatus | [0]" \
  --output text)"
if [[ "$MEMORY_STATUS" != "ACTIVE" || "$MEMORY_INDEX_STATUS" != "ACTIVE" ]]; then
  echo "Workshop Studio memory storage is not ready." >&2
  echo "  Table: $MEMORY_STATUS" >&2
  echo "  Vector index: $MEMORY_INDEX_STATUS" >&2
  exit 1
fi

echo "Reading Langfuse credentials from Secrets Manager"
LANGFUSE_SECRET_JSON="$(aws_cli secretsmanager get-secret-value \
  --secret-id "$LANGFUSE_SECRET_ARN" \
  --query 'SecretString' \
  --output text)"
LANGFUSE_PUBLIC_KEY="$(python3 -c \
  'import json, sys; print(json.loads(sys.argv[1])["init_project_public_key"])' \
  "$LANGFUSE_SECRET_JSON")"
LANGFUSE_SECRET_KEY="$(python3 -c \
  'import json, sys; print(json.loads(sys.argv[1])["init_project_secret_key"])' \
  "$LANGFUSE_SECRET_JSON")"
LANGFUSE_AUTH_STRING="$(printf '%s:%s' "$LANGFUSE_PUBLIC_KEY" "$LANGFUSE_SECRET_KEY" | base64 | tr -d '\n')"
OTLP_HEADERS="Authorization=Basic ${LANGFUSE_AUTH_STRING},x-langfuse-ingestion-version=4"
unset LANGFUSE_SECRET_JSON LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY LANGFUSE_AUTH_STRING

echo "Configuring kubectl for $CLUSTER_NAME"
aws_cli eks update-kubeconfig --name "$CLUSTER_NAME" --alias "$CLUSTER_NAME"

if ! kubectl rollout status \
  --namespace kube-system \
  deployment/aws-load-balancer-controller \
  --timeout=2m; then
  echo "AWS Load Balancer Controller is not ready." >&2
  exit 1
fi

ACCOUNT_ID="$(aws_cli sts get-caller-identity --query Account --output text)"
IMAGE_TAG="lab05-$(date -u +%Y%m%d%H%M%S)"
IMAGE_URI="${REPOSITORY_URI}:${IMAGE_TAG}"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

echo "Logging Docker into $REGISTRY"
aws_cli ecr get-login-password |
  docker login --username AWS --password-stdin "$REGISTRY"

echo "Building $IMAGE_URI for EKS x86_64 nodes"
if docker buildx version >/dev/null 2>&1; then
  docker buildx build \
    --platform linux/amd64 \
    --tag "$IMAGE_URI" \
    --load \
    "$MODULE_DIR"
else
  docker build \
    --platform linux/amd64 \
    --tag "$IMAGE_URI" \
    "$MODULE_DIR"
fi
docker push "$IMAGE_URI"

kubectl apply -f "$MODULE_DIR/k8s/base.yaml"

if [[ -n "${MORTGAGE_API_KEY:-}" ]]; then
  API_KEY="$MORTGAGE_API_KEY"
elif kubectl get secret mortgage-assistant-api-key \
  --namespace mortgage-assistant >/dev/null 2>&1; then
  API_KEY="$(
    kubectl get secret mortgage-assistant-api-key \
      --namespace mortgage-assistant \
      --output jsonpath='{.data.api-key}' |
      base64 --decode
  )"
else
  API_KEY="$(openssl rand -hex 32)"
fi

kubectl create secret generic mortgage-assistant-api-key \
  --namespace mortgage-assistant \
  --from-literal="api-key=$API_KEY" \
  --dry-run=client \
  --output=yaml |
  kubectl apply -f -

# Created directly with kubectl, never sed-templated onto disk, so the
# Basic-auth header (which embeds the Langfuse secret key) never appears in
# a rendered manifest file.
kubectl create secret generic langfuse-otel-auth \
  --namespace mortgage-assistant \
  --from-literal="otlp-headers=$OTLP_HEADERS" \
  --dry-run=client \
  --output=yaml |
  kubectl apply -f -
unset OTLP_HEADERS

SERVICE_MANIFEST="$(mktemp "${TMPDIR:-/tmp}/mortgage-observability-service.XXXXXX.yaml")"
trap 'rm -f "$SERVICE_MANIFEST"' EXIT

sed \
  -e "s|__IMAGE_URI__|$IMAGE_URI|g" \
  -e "s|__AWS_REGION__|$REGION|g" \
  -e "s|__MODEL_ID__|$MODEL_ID|g" \
  -e "s|__KB_PARAMETER_NAME__|$KB_PARAMETER_NAME|g" \
  -e "s|__MEMORY_TABLE_NAME__|$MEMORY_TABLE_NAME|g" \
  -e "s|__MEMORY_VECTOR_INDEX_NAME__|$MEMORY_VECTOR_INDEX_NAME|g" \
  -e "s|__MEMORY_EMBEDDING_MODEL_ID__|$MEMORY_EMBEDDING_MODEL_ID|g" \
  -e "s|__MEMORY_SESSION_TTL_SECONDS__|$SESSION_TTL_SECONDS|g" \
  -e "s|__SERVICE_ACCESS_CIDR__|$SERVICE_ACCESS_CIDR|g" \
  -e "s|__OTEL_EXPORTER_OTLP_ENDPOINT__|$LANGFUSE_OTLP_ENDPOINT|g" \
  -e "s|__TELEMETRY_MASK_CONTENT__|$TELEMETRY_MASK_CONTENT|g" \
  -e "s|__FAULT_INJECTION_ENABLED__|$FAULT_INJECTION_ENABLED|g" \
  -e "s|__FAULT_INJECTION_TOOL__|$FAULT_INJECTION_TOOL|g" \
  -e "s|__FAULT_INJECTION_MODE__|$FAULT_INJECTION_MODE|g" \
  -e "s|__FAULT_INJECTION_DELAY_SECONDS__|$FAULT_INJECTION_DELAY_SECONDS|g" \
  "$MODULE_DIR/k8s/service.template.yaml" > "$SERVICE_MANIFEST"

kubectl apply -f "$SERVICE_MANIFEST"

if ! kubectl rollout status \
  --namespace mortgage-assistant \
  deployment/mortgage-assistant \
  --timeout=20m; then
  kubectl describe deployment mortgage-assistant \
    --namespace mortgage-assistant || true
  kubectl get pods --namespace mortgage-assistant --output wide || true
  kubectl logs \
    --namespace mortgage-assistant \
    deployment/mortgage-assistant \
    --tail=200 || true
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

echo "Waiting for the mortgage API readiness endpoint"
for _ in $(seq 1 60); do
  if curl --fail --silent --show-error \
    --connect-timeout 5 \
    --max-time 10 \
    "http://${SERVICE_ENDPOINT}/health/ready" >/dev/null; then
    break
  fi
  sleep 10
done

if ! curl --fail --silent --show-error \
  --connect-timeout 5 \
  --max-time 10 \
  "http://${SERVICE_ENDPOINT}/health/ready" >/dev/null; then
  kubectl get pods --namespace mortgage-assistant --output wide
  kubectl logs \
    --namespace mortgage-assistant \
    deployment/mortgage-assistant \
    --tail=200 || true
  echo "Mortgage API did not become ready." >&2
  exit 1
fi

SMOKE_ACTOR="deployment-smoke-test"
SMOKE_SESSION="session-$(date -u +%Y%m%d%H%M%S)"
PROMPT_JSON="$(python3 -c \
  'import json, sys; print(json.dumps({"prompt": sys.argv[1], "actor_id": sys.argv[2], "session_id": sys.argv[3]}))' \
  "$PROMPT" "$SMOKE_ACTOR" "$SMOKE_SESSION")"

echo
echo "Mortgage assistant observability smoke-test response:"
SMOKE_RESPONSE="$(curl --fail --silent --show-error \
  --max-time 300 \
  --request POST \
  "http://${SERVICE_ENDPOINT}/invoke" \
  --header "Authorization: Bearer ${API_KEY}" \
  --header "Content-Type: application/json" \
  --data "$PROMPT_JSON")"
echo "$SMOKE_RESPONSE"

SMOKE_TRACE_ID="$(python3 -c \
  'import json, sys; print(json.loads(sys.argv[1]).get("trace_id") or "")' \
  "$SMOKE_RESPONSE")"

cat <<EOF


Lab 05 completed.
  EKS cluster: $CLUSTER_NAME
  Memory table: $MEMORY_TABLE_NAME
  Vector index: $MEMORY_VECTOR_INDEX_NAME
  Image: $IMAGE_URI
  API endpoint: http://${SERVICE_ENDPOINT}
  API key: $API_KEY
  Langfuse OTLP endpoint: $LANGFUSE_OTLP_ENDPOINT
  Langfuse UI: $LANGFUSE_URL
EOF

if [[ -n "$SMOKE_TRACE_ID" ]]; then
  cat <<EOF
  Smoke-test trace ID: $SMOKE_TRACE_ID

The smoke-test request returned a trace ID, confirming a real trace reached
Langfuse (not just that the deployment is healthy).
EOF
else
  cat <<EOF

WARNING: the smoke-test response did not include a trace_id. Tracing may not
be configured; check the OTEL_EXPORTER_OTLP_ENDPOINT environment variable on
the mortgage-assistant deployment and the pod logs for exporter errors.
EOF
fi

cat <<EOF

To open the Langfuse UI, browse to:
  $LANGFUSE_URL
and sign in with the bootstrapped workshop user (see the Langfuse
credentials secret for the email/password). CloudFront reaches the
Langfuse instance through a private VPC origin, so the instance itself
still has no direct public inbound access.

Start the observability exercises:
  cd 05-observability
  python3 app/invoke_eks.py --region $REGION --prompt \\
    "What are the benefits of a 15-year mortgage?"
EOF
