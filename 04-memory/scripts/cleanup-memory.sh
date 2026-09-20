#!/usr/bin/env bash
set -euo pipefail

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}"
PROFILE=""
CLUSTER_PARAMETER_NAME="/workshop/mortgage-assistant/eks/cluster-name"

usage() {
  cat <<'EOF'
Usage: 04-memory/scripts/cleanup-memory.sh [options]

Options:
  --region REGION    AWS Region (default: us-west-2).
  --profile PROFILE  AWS CLI profile; omit to use the default profile.
  -h, --help         Show this help.

This removes the Lab 04 Kubernetes application only. Shared infrastructure,
including the DynamoDB memory table, remains managed by Workshop Studio.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
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

CLUSTER_NAME="$(aws_cli ssm get-parameter \
  --name "$CLUSTER_PARAMETER_NAME" \
  --query 'Parameter.Value' \
  --output text)"
if [[ -z "$CLUSTER_NAME" || "$CLUSTER_NAME" == "None" ]]; then
  echo "SSM parameter $CLUSTER_PARAMETER_NAME is missing or empty in $REGION." >&2
  exit 1
fi

echo "Removing the Lab 04 EKS application and load balancer"
aws_cli eks update-kubeconfig \
  --name "$CLUSTER_NAME" \
  --alias "$CLUSTER_NAME" >/dev/null
kubectl delete namespace mortgage-assistant \
  --ignore-not-found \
  --wait=true \
  --timeout=15m

echo "Lab 04 application removed."
echo "Shared DynamoDB memory infrastructure remains managed by Workshop Studio."
