#!/usr/bin/env bash
set -euo pipefail

STACK_NAME="mortgage-assistant-workshop"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}"
PROFILE=""

usage() {
  cat <<'EOF'
Usage: 00-workshop-setup/scripts/cleanup.sh [options]

Options:
  --stack-name NAME   CloudFormation stack name.
  --region REGION     AWS Region (default: us-west-2).
  --profile PROFILE   AWS CLI profile; omit to use the default profile.
  -h, --help          Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --stack-name) STACK_NAME="$2"; shift 2 ;;
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
  --stack-name "$STACK_NAME" >/dev/null 2>&1; then
  echo "Stack $STACK_NAME does not exist in $REGION."
  exit 0
fi

stack_output() {
  local output_key="$1"
  aws_cli cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='${output_key}'].OutputValue | [0]" \
    --output text
}

delete_bucket_entries() {
  local query="$1"
  local entries

  entries="$(aws_cli s3api list-object-versions \
    --bucket "$BUCKET_NAME" \
    --query "$query" \
    --output text)"

  if [[ -z "$entries" || "$entries" == "None" ]]; then
    return
  fi

  while IFS=$'\t' read -r object_key version_id; do
    if [[ -n "$object_key" && -n "$version_id" ]]; then
      aws_cli s3api delete-object \
        --bucket "$BUCKET_NAME" \
        --key "$object_key" \
        --version-id "$version_id" \
        >/dev/null
    fi
  done <<< "$entries"
}

force_cleanup_vpc_dependencies() {
  if [[ -z "$VPC_ID" || "$VPC_ID" == "None" ]]; then
    return
  fi

  echo "Removing residual resources from dedicated workshop VPC $VPC_ID"

  LOAD_BALANCER_ARNS="$(aws_cli elbv2 describe-load-balancers \
    --query "LoadBalancers[?VpcId=='${VPC_ID}'].LoadBalancerArn" \
    --output text)"
  for load_balancer_arn in $LOAD_BALANCER_ARNS; do
    aws_cli elbv2 delete-load-balancer \
      --load-balancer-arn "$load_balancer_arn" || true
  done

  TARGET_GROUP_ARNS="$(aws_cli elbv2 describe-target-groups \
    --query "TargetGroups[?VpcId=='${VPC_ID}'].TargetGroupArn" \
    --output text)"
  for target_group_arn in $TARGET_GROUP_ARNS; do
    aws_cli elbv2 delete-target-group \
      --target-group-arn "$target_group_arn" || true
  done

  VPC_ENDPOINT_IDS="$(aws_cli ec2 describe-vpc-endpoints \
    --filters "Name=vpc-id,Values=$VPC_ID" \
    --query 'VpcEndpoints[].VpcEndpointId' \
    --output text)"
  if [[ -n "$VPC_ENDPOINT_IDS" && "$VPC_ENDPOINT_IDS" != "None" ]]; then
    aws_cli ec2 delete-vpc-endpoints \
      --vpc-endpoint-ids $VPC_ENDPOINT_IDS >/dev/null || true
  fi

  for _ in $(seq 1 30); do
    AVAILABLE_ENIS="$(aws_cli ec2 describe-network-interfaces \
      --filters "Name=vpc-id,Values=$VPC_ID" "Name=status,Values=available" \
      --query 'NetworkInterfaces[].NetworkInterfaceId' \
      --output text)"
    if [[ -z "$AVAILABLE_ENIS" || "$AVAILABLE_ENIS" == "None" ]]; then
      break
    fi
    for eni_id in $AVAILABLE_ENIS; do
      echo "Deleting residual network interface $eni_id"
      aws_cli ec2 delete-network-interface \
        --network-interface-id "$eni_id" || true
    done
    sleep 10
  done

  NONDEFAULT_SECURITY_GROUPS="$(aws_cli ec2 describe-security-groups \
    --filters "Name=vpc-id,Values=$VPC_ID" \
    --query "SecurityGroups[?GroupName!='default'].GroupId" \
    --output text)"
  for security_group_id in $NONDEFAULT_SECURITY_GROUPS; do
    echo "Deleting residual security group $security_group_id"
    aws_cli ec2 delete-security-group \
      --group-id "$security_group_id" || true
  done
}

BUCKET_NAME="$(stack_output KnowledgeBaseBucketName)"
CLUSTER_NAME="$(stack_output EksClusterName)"
VPC_ID="$(stack_output VpcId)"
MEMORY_TABLE_NAME="$(stack_output MemoryTableName)"
if [[ -z "$MEMORY_TABLE_NAME" || "$MEMORY_TABLE_NAME" == "None" ]]; then
  MEMORY_TABLE_NAME="${STACK_NAME}-memory"
fi
LEGACY_MEMORY_STACK_NAME="${STACK_NAME}-memory"

if [[ -n "$CLUSTER_NAME" && "$CLUSTER_NAME" != "None" ]] &&
  command -v kubectl >/dev/null 2>&1 &&
  aws_cli eks describe-cluster --name "$CLUSTER_NAME" >/dev/null 2>&1; then
  echo "Removing the mortgage API and its load balancer"
  aws_cli eks update-kubeconfig --name "$CLUSTER_NAME" --alias "$CLUSTER_NAME" >/dev/null
  kubectl delete namespace mortgage-assistant \
    --ignore-not-found \
    --wait=true \
    --timeout=15m || true

  if command -v helm >/dev/null 2>&1; then
    helm uninstall aws-load-balancer-controller \
      --namespace kube-system \
      --ignore-not-found \
      --wait \
      --timeout 10m || true
  fi
fi

if aws_cli cloudformation describe-stacks \
  --stack-name "$LEGACY_MEMORY_STACK_NAME" >/dev/null 2>&1; then
  STACK_MEMORY_TABLE_NAME="$(aws_cli cloudformation describe-stacks \
    --stack-name "$LEGACY_MEMORY_STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='MemoryTableName'].OutputValue | [0]" \
    --output text)"
  if [[ -n "$STACK_MEMORY_TABLE_NAME" && "$STACK_MEMORY_TABLE_NAME" != "None" ]]; then
    MEMORY_TABLE_NAME="$STACK_MEMORY_TABLE_NAME"
  fi
fi

if aws_cli dynamodb describe-table \
  --table-name "$MEMORY_TABLE_NAME" >/dev/null 2>&1; then
  echo "Deleting shared DynamoDB memory table $MEMORY_TABLE_NAME"
  aws_cli dynamodb delete-table --table-name "$MEMORY_TABLE_NAME" >/dev/null
  aws_cli dynamodb wait table-not-exists --table-name "$MEMORY_TABLE_NAME"
fi

if aws_cli cloudformation describe-stacks \
  --stack-name "$LEGACY_MEMORY_STACK_NAME" >/dev/null 2>&1; then
  echo "Deleting legacy Lab 04 stack $LEGACY_MEMORY_STACK_NAME"
  aws_cli cloudformation delete-stack --stack-name "$LEGACY_MEMORY_STACK_NAME"
  aws_cli cloudformation wait stack-delete-complete \
    --stack-name "$LEGACY_MEMORY_STACK_NAME"
fi

if [[ -n "$BUCKET_NAME" && "$BUCKET_NAME" != "None" ]]; then
  echo "Emptying all versions and delete markers from s3://$BUCKET_NAME"
  delete_bucket_entries 'Versions[].[Key,VersionId]'
  delete_bucket_entries 'DeleteMarkers[].[Key,VersionId]'
fi

for attempt in 1 2 3; do
  echo "Deleting stack $STACK_NAME (attempt $attempt)"
  aws_cli cloudformation delete-stack --stack-name "$STACK_NAME"
  if aws_cli cloudformation wait stack-delete-complete --stack-name "$STACK_NAME"; then
    echo "Stack deleted."
    exit 0
  fi

  if [[ "$attempt" -lt 3 ]]; then
    echo "CloudFormation deletion failed; cleaning residual VPC dependencies."
    force_cleanup_vpc_dependencies
  fi
done

echo "Unable to fully delete stack $STACK_NAME." >&2
aws_cli cloudformation describe-stack-events \
  --stack-name "$STACK_NAME" \
  --query 'StackEvents[?ResourceStatus==`DELETE_FAILED`].[LogicalResourceId,ResourceType,ResourceStatusReason]' \
  --output table >&2 || true
exit 1
