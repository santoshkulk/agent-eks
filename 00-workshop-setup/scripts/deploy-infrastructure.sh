#!/usr/bin/env bash
set -euo pipefail

MODULE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STACK_NAME="mortgage-assistant-workshop"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}"
PROFILE=""
ADMIN_PRINCIPAL_ARN=""
PUBLIC_ACCESS_CIDR=""
PROJECT_NAME="mortgage-assistant"
LOAD_BALANCER_CONTROLLER_VERSION="3.5.0"

usage() {
  cat <<'EOF'
Usage: 00-workshop-setup/scripts/deploy-infrastructure.sh [options]

Options:
  --stack-name NAME           CloudFormation stack name.
  --region REGION             AWS Region (default: us-west-2).
  --profile PROFILE           AWS CLI profile; omit to use the default profile.
  --admin-principal-arn ARN   IAM role/user ARN granted EKS administrator access.
  --public-access-cidr CIDR   CIDR allowed to reach the EKS API (default: detected-ip/32).
  --project-name NAME         Lowercase resource prefix (default: mortgage-assistant).
  -h, --help                  Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --stack-name) STACK_NAME="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --admin-principal-arn) ADMIN_PRINCIPAL_ARN="$2"; shift 2 ;;
    --public-access-cidr) PUBLIC_ACCESS_CIDR="$2"; shift 2 ;;
    --project-name) PROJECT_NAME="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

for command_name in aws curl helm kubectl; do
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

if [[ -z "$ADMIN_PRINCIPAL_ARN" ]]; then
  CALLER_ARN="$(aws_cli sts get-caller-identity --query Arn --output text)"
  if [[ "$CALLER_ARN" =~ ^arn:([^:]+):sts::([0-9]{12}):assumed-role/(.+)/[^/]+$ ]]; then
    ADMIN_PRINCIPAL_ARN="arn:${BASH_REMATCH[1]}:iam::${BASH_REMATCH[2]}:role/${BASH_REMATCH[3]}"
  elif [[ "$CALLER_ARN" =~ ^arn:[^:]+:iam::[0-9]{12}:(role|user)/.+$ ]]; then
    ADMIN_PRINCIPAL_ARN="$CALLER_ARN"
  else
    echo "Could not convert caller ARN to an IAM role/user ARN: $CALLER_ARN" >&2
    echo "Pass --admin-principal-arn explicitly." >&2
    exit 1
  fi
fi

if [[ -z "$PUBLIC_ACCESS_CIDR" ]]; then
  PUBLIC_IP="$(curl --fail --silent --show-error https://checkip.amazonaws.com | tr -d '[:space:]')"
  PUBLIC_ACCESS_CIDR="${PUBLIC_IP}/32"
fi

echo "Deploying shared workshop infrastructure"
echo "  Stack: $STACK_NAME"
echo "  Region: $REGION"
echo "  Administrator: $ADMIN_PRINCIPAL_ARN"
echo "  EKS API access: $PUBLIC_ACCESS_CIDR"

aws_cli cloudformation deploy \
  --stack-name "$STACK_NAME" \
  --template-file "$MODULE_DIR/infrastructure/main.yaml" \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
    ProjectName="$PROJECT_NAME" \
    AdminPrincipalArn="$ADMIN_PRINCIPAL_ARN" \
    ClusterPublicAccessCidr="$PUBLIC_ACCESS_CIDR"

stack_output() {
  local output_key="$1"
  aws_cli cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='${output_key}'].OutputValue | [0]" \
    --output text
}

BUCKET_NAME="$(stack_output KnowledgeBaseBucketName)"
KB_ID="$(stack_output KnowledgeBaseId)"
DATA_SOURCE_ID="$(stack_output KnowledgeBaseDataSourceId)"
KB_PARAMETER_NAME="$(stack_output KnowledgeBaseParameterName)"
CLUSTER_NAME="$(stack_output EksClusterName)"
REPOSITORY_URI="$(stack_output EcrRepositoryUri)"
VPC_ID="$(stack_output VpcId)"

echo "Uploading mortgage documents to s3://$BUCKET_NAME/"
aws_cli s3 sync \
  "$MODULE_DIR/knowledge-base/mortgage_dataset/" \
  "s3://$BUCKET_NAME/" \
  --delete

echo "Starting Bedrock Knowledge Base ingestion"
INGESTION_JOB_ID="$(aws_cli bedrock-agent start-ingestion-job \
  --knowledge-base-id "$KB_ID" \
  --data-source-id "$DATA_SOURCE_ID" \
  --description "Uploaded by $STACK_NAME workshop setup" \
  --query ingestionJob.ingestionJobId \
  --output text)"

while true; do
  INGESTION_STATUS="$(aws_cli bedrock-agent get-ingestion-job \
    --knowledge-base-id "$KB_ID" \
    --data-source-id "$DATA_SOURCE_ID" \
    --ingestion-job-id "$INGESTION_JOB_ID" \
    --query ingestionJob.status \
    --output text)"
  echo "Ingestion status: $INGESTION_STATUS"
  case "$INGESTION_STATUS" in
    COMPLETE) break ;;
    FAILED|STOPPED)
      aws_cli bedrock-agent get-ingestion-job \
        --knowledge-base-id "$KB_ID" \
        --data-source-id "$DATA_SOURCE_ID" \
        --ingestion-job-id "$INGESTION_JOB_ID"
      exit 1
      ;;
  esac
  sleep 15
done

echo "Configuring kubectl for $CLUSTER_NAME"
aws_cli eks update-kubeconfig --name "$CLUSTER_NAME" --alias "$CLUSTER_NAME"

echo "Installing AWS Load Balancer Controller"
helm repo add eks https://aws.github.io/eks-charts --force-update
helm repo update eks
helm upgrade --install aws-load-balancer-controller eks/aws-load-balancer-controller \
  --version "$LOAD_BALANCER_CONTROLLER_VERSION" \
  --namespace kube-system \
  --set "clusterName=$CLUSTER_NAME" \
  --set "region=$REGION" \
  --set "vpcId=$VPC_ID" \
  --set serviceAccount.create=true \
  --set serviceAccount.name=aws-load-balancer-controller \
  --set podDisruptionBudget.maxUnavailable=1 \
  --set resources.requests.cpu=100m \
  --set resources.requests.memory=128Mi \
  --set resources.limits.cpu=500m \
  --set resources.limits.memory=512Mi \
  --wait \
  --timeout 10m

kubectl rollout status \
  --namespace kube-system \
  deployment/aws-load-balancer-controller \
  --timeout=10m

cat <<EOF

Lab 00 completed.
  Knowledge Base ID: $KB_ID
  Knowledge Base parameter: $KB_PARAMETER_NAME
  EKS cluster: $CLUSTER_NAME
  ECR repository: $REPOSITORY_URI

Continue with Lab 01:
  cd 01-test-knowledge-base
  uv sync --frozen
  uv run query_knowledge_base.py --query "What are the benefits of a 15-year mortgage?"
EOF
