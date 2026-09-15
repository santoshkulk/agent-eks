# AWS Mortgage Assistant Workshop

This workshop builds a mortgage assistant progressively, starting with an
Amazon Bedrock Knowledge Base and ending with a persistent Strands application
running as an HTTP service on Amazon EKS.

The numbered modules are complete checkpoints. Future modules can add
short-term memory, long-term memory, observability, guardrails, and evaluations
while continuing to update the same EKS application.

## Workshop modules

| Lab | Module | Outcome |
| --- | --- | --- |
| 00 | `00-workshop-setup` | Deploy the shared Knowledge Base, EKS cluster, ECR repository, IAM roles, and load balancer controller. |
| 01 | `01-test-knowledge-base` | Query the deployed Knowledge Base directly and inspect retrieved chunks. |
| 02 | `02-local-strands` | Run the multi-agent Strands mortgage assistant on your laptop. |
| 03 | `03-eks-service` | Deploy the assistant as a persistent FastAPI service on EKS and invoke it repeatedly. |

## Architecture

Lab 00 creates:

- An encrypted, versioned S3 bucket containing the mortgage documents.
- An OpenSearch Serverless vector collection and index.
- An Amazon Bedrock Knowledge Base and S3 data source.
- An SSM parameter at `/app/mortgage_assistant/kb_id`.
- A two-AZ VPC with private EKS worker nodes.
- An ECR repository and EKS Pod Identity role for the application.
- AWS Load Balancer Controller support for an internet-facing Network Load Balancer.

Lab 03 adds:

- A two-replica `mortgage-assistant` Kubernetes Deployment.
- A public NLB restricted to the participant's source CIDR.
- A bearer-token protected `POST /invoke` endpoint.
- Unauthenticated liveness and readiness endpoints.

## Prerequisites

Install:

- AWS CLI v2.
- Python 3.12 or later.
- `uv`.
- Docker with Buildx, or Finch with its Docker-compatible CLI.
- `kubectl`.
- Helm.
- `curl` and `openssl`.

Configure AWS credentials on your laptop. The scripts use the default AWS CLI
profile unless `--profile` is provided:

```bash
aws sts get-caller-identity
```

When Docker is provided by Finch:

```bash
finch vm start
```

Make the scripts executable:

```bash
chmod +x \
  00-workshop-setup/scripts/deploy-infrastructure.sh \
  00-workshop-setup/scripts/cleanup.sh \
  03-eks-service/scripts/deploy-application.sh
```

## Lab 00: Deploy the shared infrastructure

From the repository root:

```bash
./00-workshop-setup/scripts/deploy-infrastructure.sh \
  --region us-west-2
```

To use a named profile:

```bash
./00-workshop-setup/scripts/deploy-infrastructure.sh \
  --region us-west-2 \
  --profile YOUR_AWS_PROFILE
```

The script:

1. Deploys the CloudFormation stack.
2. Uploads the mortgage documents.
3. Starts and waits for Knowledge Base ingestion.
4. Configures the local `kubectl` context.
5. Installs AWS Load Balancer Controller.

It does not build or deploy the mortgage application.

## Lab 01: Test the deployed Knowledge Base

```bash
cd 01-test-knowledge-base
uv sync --frozen

uv run query_knowledge_base.py \
  --query "What are the benefits of a 15-year mortgage?"
```

Inspect more results and metadata:

```bash
uv run query_knowledge_base.py \
  --query "When does refinancing make sense?" \
  --number-of-results 5 \
  --json
```

This lab calls the Bedrock `Retrieve` API directly. It confirms that ingestion
worked before the Knowledge Base is used by an agent.

## Lab 02: Run the Strands application locally

```bash
cd ../02-local-strands
uv sync --frozen

uv run mortgage_agent.py \
  --prompt "What are the benefits of a 15-year mortgage?"
```

The local process uses the same AWS profile and Region configuration as the AWS
CLI. To select them explicitly:

```bash
export AWS_PROFILE=default
export AWS_REGION=us-west-2

uv run mortgage_agent.py \
  --prompt "When does refinancing make sense?"
```

## Lab 03: Deploy the persistent application on EKS

Return to the repository root, then enter Lab 03:

```bash
cd ..
cd 03-eks-service

./scripts/deploy-application.sh \
  --region us-west-2
```

The script:

1. Reads the existing Lab 00 stack outputs.
2. Builds one `linux/amd64` container image.
3. Pushes the immutable image to the existing ECR repository.
4. Updates the Kubernetes Secret and Deployment.
5. Creates an internet-facing NLB restricted to the detected laptop IP.
6. Waits for the service and runs one smoke-test prompt.

To supply a stable API key:

```bash
export MORTGAGE_API_KEY="$(openssl rand -hex 32)"
./scripts/deploy-application.sh --region us-west-2
```

To permit a specific source CIDR:

```bash
./scripts/deploy-application.sh \
  --region us-west-2 \
  --service-access-cidr 203.0.113.10/32
```

Do not use `0.0.0.0/0` unless unrestricted public network access is intentional.

### Send prompts without rebuilding

After deployment, send any number of prompts through the existing service:

```bash
python3 app/invoke_eks.py \
  --prompt "When does refinancing make sense?"
```

Print response metadata:

```bash
python3 app/invoke_eks.py \
  --prompt "Compare 15-year and 30-year mortgages" \
  --json
```

Submitting a prompt does not build an image. Rerun
`deploy-application.sh` only after changing application, container, or
Kubernetes code. The script updates the existing Deployment with a rolling
release; it does not recreate EKS.

### Inspect the service

```bash
kubectl get deployment,pods,service \
  --namespace mortgage-assistant

kubectl logs \
  --namespace mortgage-assistant \
  deployment/mortgage-assistant \
  --tail=200
```

Retrieve the endpoint and API key manually:

```bash
export MORTGAGE_API_URL="http://$(
  kubectl get service mortgage-assistant \
    --namespace mortgage-assistant \
    --output jsonpath='{.status.loadBalancer.ingress[0].hostname}'
)"

export MORTGAGE_API_KEY="$(
  kubectl get secret mortgage-assistant-api-key \
    --namespace mortgage-assistant \
    --output jsonpath='{.data.api-key}' |
  base64 --decode
)"
```

## Cleanup

EKS nodes, NAT Gateway, OpenSearch Serverless, and the NLB incur charges while
deployed. Remove all workshop resources when finished:

```bash
cd ..

./00-workshop-setup/scripts/cleanup.sh \
  --region us-west-2
```

The cleanup script removes the Kubernetes service first, empties all S3 object
versions, deletes CloudFormation, and handles residual EKS network interfaces
or security groups if AWS reports a dependency failure.

## Future modules

Each new feature should be a numbered folder containing a complete application
checkpoint, its incremental infrastructure, deployment instructions, test
prompts, and expected results. Later modules should reuse the Lab 00 cluster and
update the same `mortgage-assistant` Kubernetes Deployment.
