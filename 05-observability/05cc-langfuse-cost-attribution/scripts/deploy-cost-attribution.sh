#!/usr/bin/env bash
set -euo pipefail

# Lab 5CC supports two safe deployment modes:
# - fresh: create the participant-owned application resources when the namespace is absent;
# - overlay: replace only the image of a complete Lab 5A or prior 5CC deployment.

MODULE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}"
PROFILE=""
SERVICE_ACCESS_CIDR=""
PROMPT="What is the balance on customer ID 123456's mortgage?"
SESSION_TTL_SECONDS="604800"
TELEMETRY_MASK_CONTENT="false"
IMAGE_URI_OVERRIDE=""
MODEL_ID="us.anthropic.claude-sonnet-4-6"
MEMORY_EMBEDDING_MODEL_ID="amazon.titan-embed-text-v2:0"
CLUSTER_PARAMETER_NAME="/workshop/mortgage-assistant/eks/cluster-name"
REPOSITORY_PARAMETER_NAME="/workshop/mortgage-assistant/ecr/repository-uri"
MEMORY_TABLE_PARAMETER_NAME="/workshop/mortgage-assistant/memory/table-name"
MEMORY_VECTOR_INDEX_PARAMETER_NAME="/workshop/mortgage-assistant/memory/vector-index-name"
KB_PARAMETER_NAME="/workshop/mortgage-assistant/bedrock/knowledge-base-id"
LANGFUSE_OTLP_ENDPOINT_PARAMETER_NAME="/workshop/mortgage-assistant/langfuse/otlp-endpoint"
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
  --region REGION                    AWS Region (default: us-west-2).
  --profile PROFILE                  AWS CLI profile; omit for the default profile.
  --service-access-cidr CIDR         Fresh install only: CIDR allowed to invoke the API.
  --session-ttl-seconds N            Fresh install only: session retention (default: 604800).
  --prompt TEXT                      Synthetic prompt used for the deployment smoke test.
  --telemetry-mask-content           Fresh install only: redact prompt/response attributes.
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

for command_name in aws curl grep kubectl openssl python3 sed tr uv; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Required command not found: $command_name" >&2
    exit 1
  fi
done
if [[ -z "$IMAGE_URI_OVERRIDE" ]] && ! command -v docker >/dev/null 2>&1; then
  echo "Required command not found: docker" >&2
  exit 1
fi
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

CLUSTER_NAME="$(ssm_parameter "$CLUSTER_PARAMETER_NAME")"
REPOSITORY_URI="$(ssm_parameter "$REPOSITORY_PARAMETER_NAME")"
MEMORY_TABLE_NAME="$(ssm_parameter "$MEMORY_TABLE_PARAMETER_NAME")"
MEMORY_VECTOR_INDEX_NAME="$(ssm_parameter "$MEMORY_VECTOR_INDEX_PARAMETER_NAME")"
KNOWLEDGE_BASE_ID="$(ssm_parameter "$KB_PARAMETER_NAME")"
LANGFUSE_OTLP_ENDPOINT="$(ssm_parameter "$LANGFUSE_OTLP_ENDPOINT_PARAMETER_NAME")"
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

DEPLOY_MODE="fresh"
if ! NAMESPACE_RESULT="$(kubectl get namespace "$NAMESPACE" \
  --ignore-not-found \
  --output name)"; then
  echo "Could not determine whether namespace $NAMESPACE exists; refusing to deploy." >&2
  exit 1
fi
if [[ -n "$NAMESPACE_RESULT" ]]; then
  DEPLOY_MODE="overlay"
fi

if [[ "$DEPLOY_MODE" == "overlay" ]]; then
  if [[ -n "$SERVICE_ACCESS_CIDR" || "$SESSION_TTL_SECONDS" != "604800" ||
    "$TELEMETRY_MASK_CONTENT" != "false" ]]; then
    echo "Fresh-install options cannot change an existing workload." >&2
    echo "Remove those options or remove the participant namespace with the appropriate lab cleanup first." >&2
    exit 2
  fi

  for resource in \
    "deployment/$DEPLOYMENT" \
    "service/$SERVICE" \
    "serviceaccount/mortgage-assistant" \
    "poddisruptionbudget/mortgage-assistant" \
    "secret/$API_SECRET" \
    "secret/$LANGFUSE_SECRET"; do
    if ! kubectl get "$resource" --namespace "$NAMESPACE" >/dev/null 2>&1; then
      echo "The existing $NAMESPACE namespace is incomplete: missing $resource." >&2
      echo "Refusing to overwrite partial or unrelated state." >&2
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

  case "$PREVIOUS_IMAGE" in
    "$REPOSITORY_URI":lab05-agent-*|"$REPOSITORY_URI":lab05cc-agent-*) ;;
    *)
      echo "Lab 5CC can overlay only a canonical Lab 5A or prior Lab 5CC image." >&2
      exit 1
      ;;
  esac
  if [[ "$CURRENT_REPLICAS" != "2" ||
    "$CURRENT_SERVICE_ACCOUNT" != "mortgage-assistant" ||
    "$CURRENT_PDB_MIN_AVAILABLE" != "1" ]]; then
    echo "The existing workload does not match the required Lab 5A/5CC availability contract." >&2
    exit 1
  fi
  if [[ -z "$SERVICE_ACCESS_RANGES" ]] ||
    [[ " $SERVICE_ACCESS_RANGES " == *" 0.0.0.0/0 "* ]] ||
    [[ " $SERVICE_ACCESS_RANGES " == *" ::/0 "* ]]; then
    echo "The existing Service must have restricted loadBalancerSourceRanges." >&2
    exit 1
  fi
  if [[ "$CURRENT_API_SECRET" != "$API_SECRET" ||
    "$CURRENT_LANGFUSE_SECRET" != "$LANGFUSE_SECRET" ]]; then
    echo "The existing workload does not reference the expected Secrets." >&2
    exit 1
  fi
else
  if ! kubectl rollout status \
    --namespace kube-system \
    deployment/aws-load-balancer-controller \
    --timeout=2m; then
    echo "AWS Load Balancer Controller is not ready." >&2
    exit 1
  fi
  if ! langfuse_otel_headers >/dev/null; then
    echo "Workshop Studio Langfuse credentials are not ready." >&2
    exit 1
  fi

  if [[ -z "$SERVICE_ACCESS_CIDR" ]]; then
    PUBLIC_IP="$(curl --fail --silent --show-error https://checkip.amazonaws.com | tr -d '[:space:]')"
    SERVICE_ACCESS_CIDR="${PUBLIC_IP}/32"
  fi
  SERVICE_ACCESS_CIDR="$(python3 - "$SERVICE_ACCESS_CIDR" <<'PYEOF'
import ipaddress
import sys

try:
    network = ipaddress.ip_network(sys.argv[1], strict=False)
except ValueError as error:
    sys.exit(f"Invalid --service-access-cidr: {error}")
if network.prefixlen == 0:
    sys.exit("--service-access-cidr must not allow the entire internet")
print(network)
PYEOF
)"
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

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/mortgage-lab05cc.XXXXXX")"
SERVICE_MANIFEST="$WORK_DIR/service.yaml"
SMOKE_STATE_FILE="$WORK_DIR/client-state.json"
MUTATION_STARTED="false"
FRESH_NAMESPACE_CREATED="false"
NAMESPACE_OWNER_TOKEN=""
DEPLOY_SUCCEEDED="false"
if [[ "$DEPLOY_MODE" == "fresh" ]]; then
  NAMESPACE_OWNER_TOKEN="lab05cc-$(openssl rand -hex 16)"
fi

cleanup_on_exit() {
  local status=$?
  trap - EXIT
  rm -f "$SERVICE_MANIFEST" "$SMOKE_STATE_FILE"
  rmdir "$WORK_DIR" 2>/dev/null || true

  if [[ "$MUTATION_STARTED" == "true" && "$DEPLOY_SUCCEEDED" != "true" ]]; then
    if [[ "$DEPLOY_MODE" == "overlay" ]]; then
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
    elif [[ "$FRESH_NAMESPACE_CREATED" == "true" ]]; then
      echo "Lab 5CC fresh deployment failed; removing only the namespace created by this run." >&2
      CURRENT_OWNER_TOKEN=""
      if CURRENT_OWNER_TOKEN="$(kubectl get namespace "$NAMESPACE" \
        --output json | python3 -c '
import json
import sys

namespace = json.load(sys.stdin)
print(namespace.get("metadata", {}).get("labels", {}).get("workshop.aws/owner-token", ""))
')" && [[ "$CURRENT_OWNER_TOKEN" == "$NAMESPACE_OWNER_TOKEN" ]]; then
        kubectl delete namespace "$NAMESPACE" --wait=true --timeout=5m ||
          echo "Automatic namespace cleanup failed; inspect $NAMESPACE before retrying." >&2
      else
        echo "Namespace ownership token changed; refusing automatic deletion." >&2
      fi
    fi
  fi
  exit "$status"
}
trap cleanup_on_exit EXIT

if [[ "$DEPLOY_MODE" == "fresh" ]]; then
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
    echo "Rendered Lab 5CC manifest contains unresolved template placeholders." >&2
    exit 1
  fi

  MUTATION_STARTED="true"
  python3 - "$NAMESPACE" "$NAMESPACE_OWNER_TOKEN" <<'PYEOF' |
import json
import sys

name, owner_token = sys.argv[1:]
json.dump(
    {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": name,
            "labels": {
                "workshop.aws/owner-token": owner_token,
                "pod-security.kubernetes.io/enforce": "restricted",
                "pod-security.kubernetes.io/audit": "restricted",
                "pod-security.kubernetes.io/warn": "restricted",
                "elbv2.k8s.aws/pod-readiness-gate-inject": "enabled",
            },
        },
    },
    sys.stdout,
)
PYEOF
    kubectl create -f -
  FRESH_NAMESPACE_CREATED="true"
  kubectl apply -f "$MODULE_DIR/k8s/base.yaml"

  openssl rand -hex 32 | tr -d '\n' |
    kubectl create secret generic "$API_SECRET" \
      --namespace "$NAMESPACE" \
      --from-file=api-key=/dev/stdin \
      --dry-run=client \
      --output=yaml |
    kubectl apply -f -

  langfuse_otel_headers |
    kubectl create secret generic "$LANGFUSE_SECRET" \
      --namespace "$NAMESPACE" \
      --from-file=otlp-headers=/dev/stdin \
      --dry-run=client \
      --output=yaml |
    kubectl apply -f -

  kubectl apply -f "$SERVICE_MANIFEST"
else
  MUTATION_STARTED="true"
  kubectl set image "deployment/$DEPLOYMENT" \
    "$CONTAINER=$IMAGE_URI" \
    --namespace "$NAMESPACE"
fi

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
  echo "Timed out waiting for the Network Load Balancer endpoint." >&2
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
  Deployment mode: $DEPLOY_MODE
  EKS cluster: $CLUSTER_NAME
  Memory table: $MEMORY_TABLE_NAME
  Vector index: $MEMORY_VECTOR_INDEX_NAME
  Image: $IMAGE_URI
  Mortgage-assistant API endpoint: http://${SERVICE_ENDPOINT}
  Langfuse UI: $LANGFUSE_URL
  Smoke-test trace ID: $SMOKE_TRACE_ID
  Smoke-test request ID: $SMOKE_REQUEST_ID (ephemeral correlation; session $SMOKE_SESSION)

The smoke client read the API key in-process without printing or persisting it.
No credential value was placed in a command argument or rendered workload file.

Fresh mode created a source-restricted Service and participant-owned resources.
Overlay mode changed only the existing application image and preserved its Service and Secrets.

Try it (from 05-observability/05cc-langfuse-cost-attribution):
  uv run app/invoke_eks.py --region $REGION --prompt \
    "What is the balance on customer ID 123456's mortgage?"

To reuse this image, rerun with: --image-uri $IMAGE_URI
EOF
