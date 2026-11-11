#!/usr/bin/env bash
set -euo pipefail

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}"
PROFILE=""
CLUSTER_NAME_PARAMETER="${CLUSTER_NAME_PARAMETER:-/workshop/mortgage-assistant/eks/cluster-name}"

usage() {
  cat <<'USAGE'
Usage: 03-eks-service/scripts/cleanup-application.sh [options]

Options:
  --region REGION    AWS Region (default: us-west-2).
  --profile PROFILE  AWS CLI profile; omit to use the default profile.
  -h, --help         Show this help.

This removes the Lab 03 participant application resources and Network Load
Balancer from the pre-provisioned mortgage-assistant namespace. It preserves
the namespace and Workshop Studio-managed RBAC. Do not run it if you are
continuing to Lab 04. The EKS cluster, ECR repository, Knowledge Base, and
other shared infrastructure remain managed by Workshop Studio.
USAGE
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
  --name "$CLUSTER_NAME_PARAMETER" \
  --query 'Parameter.Value' \
  --output text)"
if [[ -z "$CLUSTER_NAME" || "$CLUSTER_NAME" == "None" ]]; then
  echo "SSM parameter $CLUSTER_NAME_PARAMETER is missing or empty in $REGION." >&2
  exit 1
fi

echo "Removing the Lab 03 EKS application and load balancer from $CLUSTER_NAME"
aws_cli eks update-kubeconfig \
  --name "$CLUSTER_NAME" \
  --alias "$CLUSTER_NAME" >/dev/null
kubectl delete service/mortgage-assistant \
  --namespace mortgage-assistant \
  --ignore-not-found \
  --wait=true \
  --timeout=15m
kubectl delete deployment/mortgage-assistant \
  --namespace mortgage-assistant \
  --ignore-not-found \
  --wait=true \
  --timeout=15m
kubectl delete poddisruptionbudget/mortgage-assistant \
  --namespace mortgage-assistant \
  --ignore-not-found \
  --wait=true \
  --timeout=15m
kubectl delete secret/mortgage-assistant-api-key \
  --namespace mortgage-assistant \
  --ignore-not-found \
  --wait=true \
  --timeout=15m
kubectl delete serviceaccount/mortgage-assistant \
  --namespace mortgage-assistant \
  --ignore-not-found \
  --wait=true \
  --timeout=15m

echo "Lab 03 participant application resources were removed."
echo "The pre-provisioned namespace and Workshop Studio RBAC were preserved."
echo "Shared Workshop Studio infrastructure was not modified."
