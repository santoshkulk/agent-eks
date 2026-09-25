#!/usr/bin/env bash
set -euo pipefail

MODULE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}"
PROFILE=""
SERVICE_ACCESS_CIDR=""
PROMPT="What are the benefits of a 15-year mortgage?"
CLUSTER_NAME_PARAMETER="${CLUSTER_NAME_PARAMETER:-/workshop/mortgage-assistant/eks/cluster-name}"
REPOSITORY_URI_PARAMETER="${REPOSITORY_URI_PARAMETER:-/workshop/mortgage-assistant/ecr/repository-uri}"
MODEL_ID="${MODEL_ID:-us.anthropic.claude-sonnet-4-6}"
KB_PARAMETER_NAME="${KB_PARAMETER_NAME:-/workshop/mortgage-assistant/bedrock/knowledge-base-id}"

usage() {
  cat <<'EOF'
Usage: 03-eks-service/scripts/deploy-application.sh [options]

Options:
  --region REGION             AWS Region (default: us-west-2).
  --profile PROFILE           AWS CLI profile; omit to use the default profile.
  --service-access-cidr CIDR  CIDR allowed to invoke the API (default: detected-ip/32).
  --prompt TEXT               Prompt used for the deployment smoke test.
  -h, --help                  Show this help.

Environment variables:
  CLUSTER_NAME_PARAMETER      SSM parameter containing the EKS cluster name.
  REPOSITORY_URI_PARAMETER    SSM parameter containing the ECR repository URI.
  MODEL_ID                    Bedrock model ID used by the application.
  KB_PARAMETER_NAME           SSM parameter containing the Knowledge Base ID.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region) REGION="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --service-access-cidr) SERVICE_ACCESS_CIDR="$2"; shift 2 ;;
    --prompt) PROMPT="$2"; shift 2 ;;
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

AWS_OPTIONS=(--region "$REGION")
if [[ -n "$PROFILE" ]]; then
  AWS_OPTIONS+=(--profile "$PROFILE")
fi

aws_cli() {
  aws "${AWS_OPTIONS[@]}" "$@"
}

read_ssm_parameter() {
  local parameter_name="$1"
  local value

  if ! value="$(aws_cli ssm get-parameter \
    --name "$parameter_name" \
    --query Parameter.Value \
    --output text)"; then
    echo "Unable to read required SSM parameter: $parameter_name" >&2
    return 1
  fi

  if [[ -z "$value" || "$value" == "None" ]]; then
    echo "SSM parameter is empty: $parameter_name" >&2
    return 1
  fi

  printf '%s' "$value"
}

CLUSTER_NAME="$(read_ssm_parameter "$CLUSTER_NAME_PARAMETER")"
REPOSITORY_URI="$(read_ssm_parameter "$REPOSITORY_URI_PARAMETER")"

if [[ -z "$SERVICE_ACCESS_CIDR" ]]; then
  PUBLIC_IP="$(curl --fail --silent --show-error \
    https://checkip.amazonaws.com | tr -d '[:space:]')"
  SERVICE_ACCESS_CIDR="${PUBLIC_IP}/32"
fi

echo "Using Workshop Studio infrastructure"
echo "  Region: $REGION"
echo "  EKS cluster: $CLUSTER_NAME"
echo "  ECR repository: $REPOSITORY_URI"
echo "  Bedrock model: $MODEL_ID"
echo "  Knowledge Base parameter: $KB_PARAMETER_NAME"
echo "  Service access CIDR: $SERVICE_ACCESS_CIDR"

echo "Configuring kubectl for $CLUSTER_NAME"
aws_cli eks update-kubeconfig --name "$CLUSTER_NAME" --alias "$CLUSTER_NAME"

if ! kubectl rollout status \
  --namespace kube-system \
  deployment/aws-load-balancer-controller \
  --timeout=2m; then
  echo "AWS Load Balancer Controller is not ready." >&2
  echo "Verify that the Workshop Studio AWS Load Balancer Controller installation completed." >&2
  exit 1
fi

ACCOUNT_ID="$(aws_cli sts get-caller-identity --query Account --output text)"
IMAGE_TAG="lab03-$(date -u +%Y%m%d%H%M%S)"
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

API_KEY="${MORTGAGE_API_KEY:-$(openssl rand -hex 32)}"
kubectl create secret generic mortgage-assistant-api-key \
  --namespace mortgage-assistant \
  --from-literal="api-key=$API_KEY" \
  --dry-run=client \
  --output=yaml |
  kubectl apply -f -

SERVICE_MANIFEST="$(mktemp "${TMPDIR:-/tmp}/mortgage-service.XXXXXX.yaml")"
trap 'rm -f "$SERVICE_MANIFEST"' EXIT

sed \
  -e "s|__IMAGE_URI__|$IMAGE_URI|g" \
  -e "s|__AWS_REGION__|$REGION|g" \
  -e "s|__MODEL_ID__|$MODEL_ID|g" \
  -e "s|__KB_PARAMETER_NAME__|$KB_PARAMETER_NAME|g" \
  -e "s|__SERVICE_ACCESS_CIDR__|$SERVICE_ACCESS_CIDR|g" \
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

echo "Waiting for the mortgage API health endpoint"
for _ in $(seq 1 60); do
  if curl --fail --silent --show-error \
    --connect-timeout 5 \
    --max-time 10 \
    "http://${SERVICE_ENDPOINT}/health" >/dev/null; then
    break
  fi
  sleep 10
done

if ! curl --fail --silent --show-error \
  --connect-timeout 5 \
  --max-time 10 \
  "http://${SERVICE_ENDPOINT}/health" >/dev/null; then
  kubectl get pods --namespace mortgage-assistant --output wide
  kubectl logs \
    --namespace mortgage-assistant \
    deployment/mortgage-assistant \
    --tail=200 || true
  echo "Mortgage API did not become healthy." >&2
  exit 1
fi

PROMPT_JSON="$(python3 -c \
  'import json, sys; print(json.dumps({"prompt": sys.argv[1]}))' \
  "$PROMPT")"

echo
echo "Mortgage assistant smoke-test response:"
curl --fail --silent --show-error \
  --max-time 300 \
  --request POST \
  "http://${SERVICE_ENDPOINT}/invoke" \
  --header "Authorization: Bearer ${API_KEY}" \
  --header "Content-Type: application/json" \
  --data "$PROMPT_JSON"

cat <<EOF


Lab 03 completed.
  EKS cluster: $CLUSTER_NAME
  Image: $IMAGE_URI
  API endpoint: http://${SERVICE_ENDPOINT}
  API key: $API_KEY

Send another prompt without rebuilding:
  cd 03-eks-service
  uv run app/invoke_eks.py --prompt "When does refinancing make sense?"
EOF
