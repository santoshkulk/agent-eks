#!/usr/bin/env bash
set -euo pipefail

# Lab 5: Lab 4 features plus OpenTelemetry/Langfuse tracing. Complete Lab 4 first.
# The Langfuse infrastructure is provisioned by Workshop Studio; this script reads
# its endpoint and credentials and creates the langfuse-otel-auth Secret.

MODULE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}"
PROFILE=""
SERVICE_ACCESS_CIDR=""
PROMPT="What is the balance on customer ID 123456's mortgage?"
SESSION_TTL_SECONDS="604800"
MODEL_ID="us.anthropic.claude-sonnet-4-6"
MEMORY_EMBEDDING_MODEL_ID="amazon.titan-embed-text-v2:0"
TELEMETRY_MASK_CONTENT="false"
FAULT_INJECTION_ENABLED="false"
FAULT_INJECTION_TOOL="get_mortgage_details"
FAULT_INJECTION_MODE="delay"
FAULT_INJECTION_DELAY_SECONDS="5"
APPROVAL_REQUIRED_TOOLS="create_loan_application"
LEASE_SECONDS="180"
ENABLE_REASONING="false"
SNAPSHOT_HISTORY="true"
IMAGE_URI_OVERRIDE=""
CLUSTER_PARAMETER_NAME="/workshop/mortgage-assistant/eks/cluster-name"
REPOSITORY_PARAMETER_NAME="/workshop/mortgage-assistant/ecr/repository-uri"
MEMORY_TABLE_PARAMETER_NAME="/workshop/mortgage-assistant/memory/table-name"
MEMORY_VECTOR_INDEX_PARAMETER_NAME="/workshop/mortgage-assistant/memory/vector-index-name"
KB_PARAMETER_NAME="/workshop/mortgage-assistant/bedrock/knowledge-base-id"
LANGFUSE_OTLP_ENDPOINT_PARAMETER_NAME="/workshop/mortgage-assistant/langfuse/otlp-endpoint"
LANGFUSE_SECRET_ARN_PARAMETER_NAME="/workshop/mortgage-assistant/langfuse/secret-arn"
LANGFUSE_URL_PARAMETER_NAME="/workshop/mortgage-assistant/langfuse/url"

usage() {
  cat <<'EOF'
Usage: 05-observability/05a-langfuse/scripts/deploy-observability.sh [options]

Options:
  --region REGION                    AWS Region (default: us-west-2).
  --profile PROFILE                  AWS CLI profile; omit to use the default profile.
  --service-access-cidr CIDR         CIDR allowed to invoke the mortgage-assistant API.
  --session-ttl-seconds N            Short-term session retention (default: 604800).
  --prompt TEXT                      Prompt used for the deployment smoke test.
  --telemetry-mask-content           Redact prompt/response span attributes before export.
  --fault-injection-enabled          Enable the tracing fault-injection exercise.
  --fault-injection-tool NAME        Tool to target (default: get_mortgage_details).
  --fault-injection-mode MODE        "delay", "error", "abort", or "crash" (default: delay).
                                     "abort" fails the request so it can be resumed;
                                     "crash" kills the pod process mid-turn.
  --approval-required-tools LIST     Comma-separated tools that need human approval
                                     (default: create_loan_application; empty disables).
  --lease-seconds N                  Per-session lease; a crashed request can be retried
                                     after it expires (default: 180).
  --enable-reasoning                 Capture Bedrock extended-thinking in the audit trail.
  --no-snapshot-history              Keep only the latest snapshot per agent.
  --image-uri URI                    Deploy an image that is already in ECR and skip the
                                     build and push (change settings in about 2 minutes).
  --fault-injection-delay-seconds N  Delay used in "delay" mode (default: 5).
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
    --approval-required-tools) APPROVAL_REQUIRED_TOOLS="$2"; shift 2 ;;
    --lease-seconds) LEASE_SECONDS="$2"; shift 2 ;;
    --enable-reasoning) ENABLE_REASONING="true"; shift ;;
    --no-snapshot-history) SNAPSHOT_HISTORY="false"; shift ;;
    --image-uri) IMAGE_URI_OVERRIDE="$2"; shift 2 ;;
    --fault-injection-delay-seconds) FAULT_INJECTION_DELAY_SECONDS="$2"; shift 2 ;;
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
if [[ "$FAULT_INJECTION_MODE" != "delay" && "$FAULT_INJECTION_MODE" != "error" \
  && "$FAULT_INJECTION_MODE" != "abort" && "$FAULT_INJECTION_MODE" != "crash" ]]; then
  echo "--fault-injection-mode must be 'delay', 'error', 'abort', or 'crash'." >&2
  exit 2
fi
if [[ ! "$LEASE_SECONDS" =~ ^[0-9]+$ || "$LEASE_SECONDS" -lt 30 ]]; then
  echo "--lease-seconds must be an integer of at least 30." >&2
  exit 2
fi
if [[ ! "$FAULT_INJECTION_TOOL" =~ ^[A-Za-z0-9_]+$ ]]; then
  echo "--fault-injection-tool must be a tool name (letters, digits, underscores)." >&2
  exit 2
fi
if [[ ! "$APPROVAL_REQUIRED_TOOLS" =~ ^[A-Za-z0-9_,]*$ ]]; then
  echo "--approval-required-tools must be a comma-separated list of tool names." >&2
  exit 2
fi
if [[ ! "$FAULT_INJECTION_DELAY_SECONDS" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  echo "--fault-injection-delay-seconds must be a non-negative number." >&2
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
  if [[ ! "$PUBLIC_IP" =~ ^([0-9]{1,3}[.]){3}[0-9]{1,3}$ ]]; then
    echo "Could not detect an IPv4 address (got: $PUBLIC_IP). Pass --service-access-cidr." >&2
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
LANGFUSE_SECRET_ARN="$(ssm_parameter "$LANGFUSE_SECRET_ARN_PARAMETER_NAME")"
LANGFUSE_URL="$(ssm_parameter "$LANGFUSE_URL_PARAMETER_NAME")"

if [[ -z "$KNOWLEDGE_BASE_ID" ]]; then
  echo "The discovered Knowledge Base ID is empty." >&2
  exit 1
fi

# boto3 (pinned in uv.lock) instead of the AWS CLI: older CLI v2 releases omit VectorIndexes.
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
fi

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
if [[ -z "${API_KEY//[[:space:]]/}" ]]; then
  echo "The mortgage-assistant API key must not be empty." >&2
  exit 1
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

SERVICE_MANIFEST="$(mktemp "${TMPDIR:-/tmp}/mortgage-lab05-service.XXXXXX.yaml")"
trap 'rm -f "$SERVICE_MANIFEST"' EXIT

for template_variable in \
  IMAGE_URI REGION MODEL_ID KB_PARAMETER_NAME MEMORY_TABLE_NAME \
  MEMORY_VECTOR_INDEX_NAME MEMORY_EMBEDDING_MODEL_ID SESSION_TTL_SECONDS \
  LANGFUSE_OTLP_ENDPOINT TELEMETRY_MASK_CONTENT \
  FAULT_INJECTION_ENABLED FAULT_INJECTION_TOOL FAULT_INJECTION_MODE \
  FAULT_INJECTION_DELAY_SECONDS SERVICE_ACCESS_CIDR APPROVAL_REQUIRED_TOOLS \
  LEASE_SECONDS ENABLE_REASONING SNAPSHOT_HISTORY; do
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
  -e "s|__FAULT_INJECTION_ENABLED__|$FAULT_INJECTION_ENABLED|g" \
  -e "s|__FAULT_INJECTION_TOOL__|$FAULT_INJECTION_TOOL|g" \
  -e "s|__FAULT_INJECTION_MODE__|$FAULT_INJECTION_MODE|g" \
  -e "s|__FAULT_INJECTION_DELAY_SECONDS__|$FAULT_INJECTION_DELAY_SECONDS|g" \
  -e "s|__APPROVAL_REQUIRED_TOOLS__|$APPROVAL_REQUIRED_TOOLS|g" \
  -e "s|__LEASE_SECONDS__|$LEASE_SECONDS|g" \
  -e "s|__ENABLE_REASONING__|$ENABLE_REASONING|g" \
  -e "s|__SNAPSHOT_HISTORY__|$SNAPSHOT_HISTORY|g" \
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

echo "Waiting for the Lab 05 mortgage API readiness endpoint"
# The NLB's DNS name and targets come up in stages, so require two successes in a row.
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
  kubectl logs \
    --namespace mortgage-assistant \
    deployment/mortgage-assistant \
    --tail=200 || true
  echo "Lab 05 mortgage API did not become ready." >&2
  exit 1
fi

SMOKE_ACTOR="deployment-smoke-test"
SMOKE_SESSION="session-$(date -u +%Y%m%d%H%M%S)"
SMOKE_REQUEST="smoke-$(date -u +%Y%m%d%H%M%S)"
PROMPT_JSON="$(python3 -c \
  'import json, sys; print(json.dumps({"prompt": sys.argv[1], "actor_id": sys.argv[2], "session_id": sys.argv[3], "request_id": sys.argv[4]}))' \
  "$PROMPT" "$SMOKE_ACTOR" "$SMOKE_SESSION" "$SMOKE_REQUEST")"

echo
echo "Lab 05 smoke-test response:"
SMOKE_RESPONSE="$(curl --fail-with-body --silent --show-error \
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

if [[ -z "$SMOKE_TRACE_ID" ]]; then
  echo "The Lab 5 smoke response did not include a trace_id." >&2
  echo "Check the OTLP endpoint, langfuse-otel-auth Secret, and mortgage-assistant application logs." >&2
  exit 1
fi

echo
echo "Verifying the audit trail and idempotent replay for request ${SMOKE_REQUEST}:"
SMOKE_TRAIL="$(curl --fail-with-body --silent --show-error \
  --max-time 60 \
  --get "http://${SERVICE_ENDPOINT}/executions/${SMOKE_REQUEST}" \
  --data-urlencode "actor_id=${SMOKE_ACTOR}" \
  --data-urlencode "session_id=${SMOKE_SESSION}" \
  --header "Authorization: Bearer ${API_KEY}")"
SMOKE_REPLAY="$(curl --fail-with-body --silent --show-error \
  --max-time 60 \
  --request POST \
  "http://${SERVICE_ENDPOINT}/invoke" \
  --header "Authorization: Bearer ${API_KEY}" \
  --header "Content-Type: application/json" \
  --data "$PROMPT_JSON")"
python3 - "$SMOKE_TRAIL" "$SMOKE_RESPONSE" "$SMOKE_REPLAY" <<'PYEOF'
import json
import sys

trail, first, replay = (json.loads(value) for value in sys.argv[1:4])
problems = []
if not trail.get("chain_valid"):
    problems.append("audit hash chain is not valid")
if trail["execution"]["status"] != "COMPLETED":
    problems.append(f"execution status is {trail['execution']['status']}")
if not trail.get("records"):
    problems.append("audit trail is empty")
if replay.get("response") != first.get("response"):
    problems.append("replaying the request_id did not return the stored response")
if replay.get("attempt") != first.get("attempt"):
    problems.append("replaying the request_id started a new attempt")
if problems:
    sys.exit("Lab 5 smoke verification failed: " + "; ".join(problems))
print(
    f"  audit records: {len(trail['records'])}, chain valid, "
    f"route: {[step['agent'] for step in trail['explanation']['route']] or 'none'}"
)
PYEOF

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
  Smoke-test request ID: $SMOKE_REQUEST (session $SMOKE_SESSION)

The mortgage-assistant API key and the langfuse-otel-auth Secret (created from
Secrets Manager) were not printed.

Try it (from 05-observability/05a-langfuse):
  uv run app/invoke_eks.py --region $REGION --prompt \\
    "What is the balance on customer ID 123456's mortgage?"
  uv run app/invoke_eks.py --region $REGION --trail last

To change settings without rebuilding, rerun with: --image-uri $IMAGE_URI
EOF
