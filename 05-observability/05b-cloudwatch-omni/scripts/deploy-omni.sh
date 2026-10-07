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
FAULT_INJECTION_ENABLED="false"
FAULT_INJECTION_TOOL="get_mortgage_details"
FAULT_INJECTION_MODE="delay"
FAULT_INJECTION_DELAY_SECONDS="5"
CLUSTER_PARAMETER_NAME="/workshop/mortgage-assistant/eks/cluster-name"
REPOSITORY_PARAMETER_NAME="/workshop/mortgage-assistant/ecr/repository-uri"
MEMORY_TABLE_PARAMETER_NAME="/workshop/mortgage-assistant/memory/table-name"
MEMORY_VECTOR_INDEX_PARAMETER_NAME="/workshop/mortgage-assistant/memory/vector-index-name"
KB_PARAMETER_NAME="/workshop/mortgage-assistant/bedrock/knowledge-base-id"
TRACE_LOG_GROUP_PARAMETER_NAME="/workshop/mortgage-assistant/cloudwatch/trace-log-group"
OMNI_DOMAIN_URL_PARAMETER_NAME="/workshop/mortgage-assistant/cloudwatch/omni-domain-url"

usage() {
  cat <<'EOT'
Usage: 05-observability/05b-cloudwatch-omni/scripts/deploy-omni.sh [options]

Deploys the mortgage assistant as mortgage-assistant-omni with its own
Secret, exporting traces to Amazon CloudWatch Omni. It does not require
Lab 5a and does not modify any Lab 5a resource.

Options:
  --region REGION                    AWS Region (default: us-west-2).
  --profile PROFILE                  AWS CLI profile; omit to use the default profile.
  --service-access-cidr CIDR         CIDR allowed to invoke the API.
  --prompt TEXT                      Prompt used for the deployment smoke test.
  --session-ttl-seconds N            Short-term session retention (default: 604800).
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
    --service-access-cidr) SERVICE_ACCESS_CIDR="$2"; shift 2 ;;
    --prompt) PROMPT="$2"; shift 2 ;;
    --session-ttl-seconds) SESSION_TTL_SECONDS="$2"; shift 2 ;;
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

if [[ -z "$SERVICE_ACCESS_CIDR" ]]; then
  PUBLIC_IP="$(curl --fail --silent --show-error https://checkip.amazonaws.com | tr -d '[:space:]')"
  SERVICE_ACCESS_CIDR="${PUBLIC_IP}/32"
fi

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

MEMORY_STATUS="$(aws_cli dynamodb describe-table --table-name "$MEMORY_TABLE_NAME" \
  --query 'Table.TableStatus' --output text)"
MEMORY_INDEX_STATUS="$(aws_cli dynamodb describe-table --table-name "$MEMORY_TABLE_NAME" \
  --query "Table.VectorIndexes[?IndexName=='${MEMORY_VECTOR_INDEX_NAME}'].IndexStatus | [0]" \
  --output text)"
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

# Namespace and ServiceAccount are the shared workshop objects; applying is idempotent.
kubectl apply -f "$MODULE_DIR/k8s/base.yaml"

# Lab 5b owns its Secret, so it neither requires nor modifies Lab 5a.
if kubectl get secret mortgage-assistant-omni-api-key \
  --namespace mortgage-assistant >/dev/null 2>&1; then
  API_KEY="$(kubectl get secret mortgage-assistant-omni-api-key \
    --namespace mortgage-assistant --output jsonpath='{.data.api-key}' | base64 --decode)"
else
  API_KEY="$(openssl rand -hex 32)"
  kubectl create secret generic mortgage-assistant-omni-api-key \
    --namespace mortgage-assistant --from-literal="api-key=$API_KEY"
fi

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

SERVICE_MANIFEST="$(mktemp "${TMPDIR:-/tmp}/mortgage-omni-service.XXXXXX.yaml")"
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
  -e "s|__TRACE_LOG_GROUP__|$TRACE_LOG_GROUP|g" \
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

echo "Waiting for the Network Load Balancer endpoint"
SERVICE_ENDPOINT=""
for _ in $(seq 1 60); do
  SERVICE_ENDPOINT="$(kubectl get service mortgage-assistant-omni \
    --namespace mortgage-assistant \
    --output jsonpath='{.status.loadBalancer.ingress[0].hostname}')"
  if [[ -n "$SERVICE_ENDPOINT" ]]; then
    break
  fi
  sleep 10
done
if [[ -z "$SERVICE_ENDPOINT" ]]; then
  kubectl describe service mortgage-assistant-omni --namespace mortgage-assistant
  echo "Timed out waiting for the Network Load Balancer endpoint." >&2
  exit 1
fi

echo "Waiting for the mortgage API readiness endpoint"
for _ in $(seq 1 60); do
  if curl --fail --silent --connect-timeout 5 --max-time 10 \
    "http://${SERVICE_ENDPOINT}/health/ready" >/dev/null; then
    break
  fi
  sleep 10
done
if ! curl --fail --silent --connect-timeout 5 --max-time 10 \
  "http://${SERVICE_ENDPOINT}/health/ready" >/dev/null; then
  kubectl logs --namespace mortgage-assistant deployment/mortgage-assistant-omni --tail=200 || true
  echo "Mortgage API did not become ready." >&2
  exit 1
fi

SMOKE_SESSION="session-$(date -u +%Y%m%d%H%M%S)"
PROMPT_JSON="$(python3 -c \
  'import json, sys; print(json.dumps({"prompt": sys.argv[1], "actor_id": sys.argv[2], "session_id": sys.argv[3]}))' \
  "$PROMPT" "deployment-smoke-test" "$SMOKE_SESSION")"

echo
echo "Mortgage assistant smoke-test response:"
SMOKE_RESPONSE="$(curl --fail --silent --show-error --max-time 300 \
  --request POST "http://${SERVICE_ENDPOINT}/invoke" \
  --header "Authorization: Bearer ${API_KEY}" \
  --header "Content-Type: application/json" \
  --data "$PROMPT_JSON")"
echo "$SMOKE_RESPONSE"

SMOKE_TRACE_ID="$(python3 -c \
  'import json, sys; print(json.loads(sys.argv[1]).get("trace_id") or "")' "$SMOKE_RESPONSE")"

OMNI_CONSOLE_URL="https://${REGION}.console.aws.amazon.com/cloudwatch/home?region=${REGION}#settings:/omni"

cat <<EOT


Lab 5b completed.
  Image: $IMAGE_URI
  API endpoint: http://${SERVICE_ENDPOINT}
  API key: $API_KEY
  Trace log group: $TRACE_LOG_GROUP

CloudWatch Omni for this account:
  Domain URL:  $OMNI_DOMAIN_URL
  Console:     $OMNI_CONSOLE_URL
  Space:       mortgage-assistant
Sign in to the AWS console with your workshop role, open the Console link to
reach Omni, then choose the mortgage-assistant space.
EOT
if [[ -n "$SMOKE_TRACE_ID" ]]; then
  echo "  Smoke-test trace ID: $SMOKE_TRACE_ID"
  echo
  echo "A trace appears in CloudWatch Omni within about five minutes."
else
  echo
  echo "WARNING: the response had no trace_id. Check the pod logs for ADOT startup errors."
fi
cat <<EOT

Next, send test requests and look up their traces in Omni:
  cd 05-observability/05b-cloudwatch-omni
  ./scripts/test-agent.sh --region $REGION
EOT
