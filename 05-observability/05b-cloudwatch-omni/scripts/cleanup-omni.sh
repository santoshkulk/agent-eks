#!/usr/bin/env bash
set -euo pipefail

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}"
PROFILE=""

usage() {
  cat <<'EOT'
Usage: 05-observability/05b-cloudwatch-omni/scripts/cleanup-omni.sh [options]

Removes only the Lab 5b Deployment and Service (mortgage-assistant-omni).
Lab 5a, the namespace, secrets, and Workshop Studio resources are kept.

Options:
  --region REGION     AWS Region (default: us-west-2).
  --profile PROFILE   AWS CLI profile; omit to use the default profile.
  -h, --help          Show this help.
EOT
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

CLUSTER_NAME="$(aws "${AWS_OPTIONS[@]}" ssm get-parameter \
  --name /workshop/mortgage-assistant/eks/cluster-name \
  --query 'Parameter.Value' --output text)"
aws "${AWS_OPTIONS[@]}" eks update-kubeconfig --name "$CLUSTER_NAME" --alias "$CLUSTER_NAME"

echo "Removing the Lab 5b application"
kubectl delete deployment,service mortgage-assistant-omni \
  --namespace mortgage-assistant --ignore-not-found
echo "Lab 5b application removed."
