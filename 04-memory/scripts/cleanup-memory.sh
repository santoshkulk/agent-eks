#!/usr/bin/env bash
set -euo pipefail

BASE_STACK_NAME="mortgage-assistant-workshop"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}"
PROFILE=""

usage() {
  cat <<'EOF'
Usage: 04-memory/scripts/cleanup-memory.sh [options]

Options:
  --base-stack-name NAME  Lab 00 CloudFormation stack name.
  --region REGION         AWS Region (default: us-west-2).
  --profile PROFILE       AWS CLI profile; omit to use the default profile.
  -h, --help              Show this help.

This removes the Lab 04 Kubernetes application only. Shared infrastructure,
including the DynamoDB memory table, remains owned by Lab 00.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-stack-name) BASE_STACK_NAME="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

AWS_OPTIONS=(--region "$REGION")
if [[ -n "$PROFILE" ]]; then
  AWS_OPTIONS+=(--profile "$PROFILE")
fi

aws_cli() {
  aws "${AWS_OPTIONS[@]}" "$@"
}

if ! aws_cli cloudformation describe-stacks \
  --stack-name "$BASE_STACK_NAME" >/dev/null 2>&1; then
  echo "Lab 00 stack $BASE_STACK_NAME was not found in $REGION." >&2
  exit 1
fi

CLUSTER_NAME="$(aws_cli cloudformation describe-stacks \
  --stack-name "$BASE_STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='EksClusterName'].OutputValue | [0]" \
  --output text)"

echo "Removing the Lab 04 EKS application and load balancer"
aws_cli eks update-kubeconfig \
  --name "$CLUSTER_NAME" \
  --alias "$CLUSTER_NAME" >/dev/null
kubectl delete namespace mortgage-assistant \
  --ignore-not-found \
  --wait=true \
  --timeout=15m

echo "Lab 04 application removed."
echo "Shared DynamoDB memory infrastructure remains managed by Lab 00."
