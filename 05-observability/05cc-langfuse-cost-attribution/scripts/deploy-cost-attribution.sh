#!/usr/bin/env bash
set -euo pipefail

# Lab 5CC: replace only the existing Lab 5A application image. Workshop Studio
# and Lab 5A own the shared service, secrets, memory, and Langfuse resources.

MODULE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}"
PROFILE=""
PROMPT="What is the balance on customer ID 123456's mortgage?"
IMAGE_URI_OVERRIDE=""
CLUSTER_PARAMETER_NAME="/workshop/mortgage-assistant/eks/cluster-name"
REPOSITORY_PARAMETER_NAME="/workshop/mortgage-assistant/ecr/repository-uri"
MEMORY_TABLE_PARAMETER_NAME="/workshop/mortgage-assistant/memory/table-name"
MEMORY_VECTOR_INDEX_PARAMETER_NAME="/workshop/mortgage-assistant/memory/vector-index-name"
KB_PARAMETER_NAME="/workshop/mortgage-assistant/bedrock/knowledge-base-id"
LANGFUSE_URL_PARAMETER_NAME="/workshop/mortgage-assistant/langfuse/url"
NAMESPACE="mortgage-assistant"
DEPLOYMENT="mortgage-assistant"
CONTAINER="mortgage-assistant"
SERVICE="mortgage-assistant"
API_SECRET="mortgage-assistant-api-key"
LANGFUSE_SECRET="langfuse-otel-auth"

usage() {
  cat <<'EOF'
Usage: 05-observability/05cc-langfuse-cost-attribution/scripts/deploy-cost-attribution.sh [options]

Options:
  --region REGION    AWS Region (default: us-west-2).
  --profile PROFILE  AWS CLI profile; omit for the default profile.
  --prompt TEXT      Synthetic prompt used for the deployment smoke test.
  --image-uri URI    Reuse an ECR image and skip build and push.
  -h, --help         Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region) REGION="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --prompt) PROMPT="$2"; shift 2 ;;
    --image-uri) IMAGE_URI_OVERRIDE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

for command_name in aws curl kubectl python3 uv; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Required command not found: $command_name" >&2
    exit 1
  fi
done
if [[ -z "$IMAGE_URI_OVERRIDE" ]]; then
  for command_name in docker; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
      echo "Required command not found: $command_name" >&2
      exit 1
    fi
  done
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

CLUSTER_NAME="$(ssm_parameter "$CLUSTER_PARAMETER_NAME")"
REPOSITORY_URI="$(ssm_parameter "$REPOSITORY_PARAMETER_NAME")"
MEMORY_TABLE_NAME="$(ssm_parameter "$MEMORY_TABLE_PARAMETER_NAME")"
MEMORY_VECTOR_INDEX_NAME="$(ssm_parameter "$MEMORY_VECTOR_INDEX_PARAMETER_NAME")"
KNOWLEDGE_BASE_ID="$(ssm_parameter "$KB_PARAMETER_NAME")"
LANGFUSE_URL="$(ssm_parameter "$LANGFUSE_URL_PARAMETER_NAME")"

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

if [[ -z "$KNOWLEDGE_BASE_ID" ]]; then
  echo "The discovered Knowledge Base ID is empty." >&2
  exit 1
fi

echo "Configuring kubectl for $CLUSTER_NAME"
aws_cli eks update-kubeconfig --name "$CLUSTER_NAME" --alias "$CLUSTER_NAME"

# 5CC is deliberately an image-only overlay. Require the Lab 5A resources and
# preserve their Service, source ranges, Secrets, environment, PDB, and identity.
for resource in \
  "deployment/$DEPLOYMENT" \
  "service/$SERVICE" \
  "serviceaccount/mortgage-assistant" \
  "poddisruptionbudget/mortgage-assistant" \
  "secret/$API_SECRET" \
  "secret/$LANGFUSE_SECRET"; do
  if ! kubectl get "$resource" --namespace "$NAMESPACE" >/dev/null 2>&1; then
    echo "Required Lab 5A resource is missing: $NAMESPACE/$resource" >&2
    echo "Deploy Lab 5A before running the isolated 5CC pilot." >&2
    exit 1
  fi
done

PREVIOUS_IMAGE="$(kubectl get deployment "$DEPLOYMENT" \
  --namespace "$NAMESPACE" \
  --output jsonpath='{.spec.template.spec.containers[?(@.name=="mortgage-assistant")].image}')"
CURRENT_REPLICAS="$(kubectl get deployment "$DEPLOYMENT" \
  --namespace "$NAMESPACE" \
  --output jsonpath='{.spec.replicas}')"
CURRENT_SERVICE_ACCOUNT="$(kubectl get deployment "$DEPLOYMENT" \
  --namespace "$NAMESPACE" \
  --output jsonpath='{.spec.template.spec.serviceAccountName}')"
CURRENT_PDB_MIN_AVAILABLE="$(kubectl get poddisruptionbudget mortgage-assistant \
  --namespace "$NAMESPACE" \
  --output jsonpath='{.spec.minAvailable}')"
SERVICE_ACCESS_RANGES="$(kubectl get service "$SERVICE" \
  --namespace "$NAMESPACE" \
  --output jsonpath='{.spec.loadBalancerSourceRanges[*]}')"
CURRENT_API_SECRET="$(kubectl get deployment "$DEPLOYMENT" \
  --namespace "$NAMESPACE" \
  --output jsonpath='{.spec.template.spec.containers[?(@.name=="mortgage-assistant")].env[?(@.name=="MORTGAGE_API_KEY")].valueFrom.secretKeyRef.name}')"
CURRENT_LANGFUSE_SECRET="$(kubectl get deployment "$DEPLOYMENT" \
  --namespace "$NAMESPACE" \
  --output jsonpath='{.spec.template.spec.containers[?(@.name=="mortgage-assistant")].env[?(@.name=="OTEL_EXPORTER_OTLP_HEADERS")].valueFrom.secretKeyRef.name}')"

if [[ -z "$PREVIOUS_IMAGE" ]]; then
  echo "The existing mortgage-assistant container image could not be read." >&2
  exit 1
fi
case "$PREVIOUS_IMAGE" in
  "$REPOSITORY_URI":lab05-agent-*|"$REPOSITORY_URI":lab05cc-agent-*) ;;
  *)
    echo "Lab 5CC can overlay only a Lab 5A or repeated Lab 5CC image." >&2
    echo "Redeploy Lab 5A before replacing another checkpoint." >&2
    exit 1
    ;;
esac
if [[ "$CURRENT_REPLICAS" != "2" ]]; then
  echo "Lab 5CC requires the existing Lab 5A deployment with exactly two replicas." >&2
  exit 1
fi
if [[ "$CURRENT_SERVICE_ACCOUNT" != "mortgage-assistant" ]]; then
  echo "Lab 5CC requires the Lab 5A mortgage-assistant service account." >&2
  exit 1
fi
if [[ "$CURRENT_PDB_MIN_AVAILABLE" != "1" ]]; then
  echo "Lab 5CC requires the Lab 5A PodDisruptionBudget with minAvailable 1." >&2
  exit 1
fi
if [[ -z "$SERVICE_ACCESS_RANGES" ]] ||
  [[ " $SERVICE_ACCESS_RANGES " == *" 0.0.0.0/0 "* ]] ||
  [[ " $SERVICE_ACCESS_RANGES " == *" ::/0 "* ]]; then
  echo "The existing Service must have restricted loadBalancerSourceRanges." >&2
  exit 1
fi
if [[ "$CURRENT_API_SECRET" != "$API_SECRET" ]] ||
  [[ "$CURRENT_LANGFUSE_SECRET" != "$LANGFUSE_SECRET" ]]; then
  echo "The existing Lab 5A deployment does not reference the expected Secrets." >&2
  exit 1
fi

if [[ -n "$IMAGE_URI_OVERRIDE" ]]; then
  IMAGE_URI="$IMAGE_URI_OVERRIDE"
  echo "Reusing image $IMAGE_URI (skipping build and push)"
else
  ACCOUNT_ID="$(aws_cli sts get-caller-identity --query Account --output text)"
  IMAGE_TAG="lab05cc-agent-$(date -u +%Y%m%d%H%M%S)"
  IMAGE_URI="${REPOSITORY_URI}:${IMAGE_TAG}"
  REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

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

SMOKE_STATE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/mortgage-lab05cc-smoke.XXXXXX")"
SMOKE_STATE_FILE="$SMOKE_STATE_DIR/client-state.json"
MUTATION_STARTED="false"
DEPLOY_SUCCEEDED="false"

cleanup_on_exit() {
  local status=$?
  trap - EXIT
  rm -f "$SMOKE_STATE_FILE"
  rmdir "$SMOKE_STATE_DIR" 2>/dev/null || true

  if [[ "$MUTATION_STARTED" == "true" && "$DEPLOY_SUCCEEDED" != "true" ]]; then
    echo "Lab 5CC did not complete; restoring the captured application image." >&2
    if kubectl set image "deployment/$DEPLOYMENT" \
      "$CONTAINER=$PREVIOUS_IMAGE" \
      --namespace "$NAMESPACE" >/dev/null &&
      kubectl rollout status \
        --namespace "$NAMESPACE" \
        "deployment/$DEPLOYMENT" \
        --timeout=20m; then
      echo "Restored $PREVIOUS_IMAGE" >&2
    else
      echo "Automatic image restoration failed. Redeploy Lab 5A immediately." >&2
    fi
  fi
  exit "$status"
}
trap cleanup_on_exit EXIT

# This is the pilot's only Kubernetes mutation. Every other live setting remains
# exactly as Lab 5A configured it.
MUTATION_STARTED="true"
kubectl set image "deployment/$DEPLOYMENT" \
  "$CONTAINER=$IMAGE_URI" \
  --namespace "$NAMESPACE"

if ! kubectl rollout status \
  --namespace "$NAMESPACE" \
  "deployment/$DEPLOYMENT" \
  --timeout=20m; then
  kubectl describe deployment "$DEPLOYMENT" --namespace "$NAMESPACE" || true
  kubectl get pods --namespace "$NAMESPACE" --output wide || true
  kubectl logs --namespace "$NAMESPACE" "deployment/$DEPLOYMENT" --tail=200 || true
  exit 1
fi

echo "Waiting for the Network Load Balancer endpoint"
SERVICE_ENDPOINT=""
for _ in $(seq 1 60); do
  SERVICE_ENDPOINT="$(kubectl get service "$SERVICE" \
    --namespace "$NAMESPACE" \
    --output jsonpath='{.status.loadBalancer.ingress[0].hostname}')"
  if [[ -n "$SERVICE_ENDPOINT" ]]; then
    break
  fi
  sleep 10
done
if [[ -z "$SERVICE_ENDPOINT" ]]; then
  kubectl describe service "$SERVICE" --namespace "$NAMESPACE"
  echo "Timed out waiting for the existing Network Load Balancer endpoint." >&2
  exit 1
fi

echo "Waiting for the Lab 5CC mortgage API readiness endpoint"
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
  kubectl get pods --namespace "$NAMESPACE" --output wide
  kubectl logs --namespace "$NAMESPACE" "deployment/$DEPLOYMENT" --tail=200 || true
  echo "Lab 5CC mortgage API did not become ready." >&2
  exit 1
fi

SMOKE_ACTOR="deployment-smoke-test"
SMOKE_SESSION="session-$(date -u +%Y%m%d%H%M%S)"

echo
echo "Lab 5CC smoke-test response:"
SMOKE_RESPONSE="$(uv run --project "$MODULE_DIR" --frozen python \
  "$MODULE_DIR/app/invoke_eks.py" \
  --region "$REGION" \
  --url "http://${SERVICE_ENDPOINT}" \
  --namespace "$NAMESPACE" \
  --secret "$API_SECRET" \
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
    sys.exit("Lab 5CC smoke response is missing: " + ", ".join(missing))
if response["actor_id"] != "deployment-smoke-test":
    sys.exit("Lab 5CC smoke response returned the wrong actor_id")
if not isinstance(response.get("duration_ms"), int):
    sys.exit("Lab 5CC smoke response duration_ms is not an integer")
trace_id = response.get("trace_id")
if not isinstance(trace_id, str) or not re.fullmatch(r"[0-9a-f]{32}", trace_id):
    sys.exit("Lab 5CC smoke response did not include a valid trace_id")
print(response["request_id"], trace_id)
'
)

DEPLOY_SUCCEEDED="true"
cat <<EOF

Lab 5CC completed.
  EKS cluster: $CLUSTER_NAME
  Memory table: $MEMORY_TABLE_NAME
  Vector index: $MEMORY_VECTOR_INDEX_NAME
  Image: $IMAGE_URI
  Previous image retained for failure rollback: $PREVIOUS_IMAGE
  Mortgage-assistant API endpoint: http://${SERVICE_ENDPOINT}
  Langfuse UI: $LANGFUSE_URL
  Smoke-test trace ID: $SMOKE_TRACE_ID
  Smoke-test request ID: $SMOKE_REQUEST_ID (ephemeral correlation; session $SMOKE_SESSION)

The deployment shell did not capture, print, or pass credential values in process
arguments. The smoke client read the existing API key in-process for the request
without printing or persisting it. The Langfuse Secret value was not read, and
neither Secret was replaced. The existing Service and source ranges were unchanged.

Try it (from 05-observability/05cc-langfuse-cost-attribution):
  uv run app/invoke_eks.py --region $REGION --prompt \
    "What is the balance on customer ID 123456's mortgage?"

To reuse this image, rerun with: --image-uri $IMAGE_URI
EOF
