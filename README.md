# Build and deploy Strands agents to Amazon EKS

This repository contains complete participant checkpoints for building a Strands mortgage assistant, deploying it to Amazon EKS, adding DynamoDB-backed memory, instrumenting it with OpenTelemetry traces exported to self-hosted Langfuse, and integrating a provider-owned Model Context Protocol (MCP) tool.

Workshop Studio events provision the shared AWS environment, self-hosted Langfuse infrastructure, and MCP provider before participants begin. The standalone `00-workshop-setup` module can provision the shared Bedrock, EKS, ECR, load-balancing, and memory resources, but it does not provision the Lab 5 Langfuse stack or Lab 6 credit-score provider.

## Workshop Modules

| Lab | Module | Outcome |
| --- | --- | --- |
| 0 | `00-workshop-setup` | Optional standalone setup for shared Knowledge Base, EKS, ECR, IAM, load-balancing, and DynamoDB memory resources. |
| 1 | `01-test-knowledge-base` and Lab 6 explorer | Query the Knowledge Base, inspect EKS, and independently initialize/list/call the installed MCP server. |
| 2 | `02-local-strands` | Run the multi-agent Strands mortgage assistant locally. |
| 3 | `03-eks-service` | Deploy the assistant as a persistent two-replica FastAPI service on EKS. |
| 4 | `04-memory` | Add short-term sessions and durable semantic memory backed by DynamoDB. |
| 5 | `05-observability` | Export correlated Strands traces to self-hosted Langfuse and inspect model/tool latency, token usage, and controlled failures. |
| 6 | `06-mcp-credit-score` | Explore an MCP server, integrate its tool with the Strands supervisor, and deploy the updated agent to EKS. |

Numbered application modules are self-contained checkpoints. Runtime modules do not import code from earlier lab directories.

## Application Progression

Lab 3 creates the consumer-owned `mortgage-assistant` namespace, a two-replica Deployment, an NLB-backed Kubernetes Service, an API-key Secret, and health and invocation routes.

Lab 4 updates those resources in place and adds:

- Strands `SnapshotSessionManager` for short-term conversation state.
- Strands `MemoryManager` for actor-scoped durable mortgage preferences.
- DynamoDB session, memory, and vector-search storage.
- A stateful participant client and deterministic hydration utilities.

Lab 5 updates the memory-enabled service with OpenTelemetry/Langfuse tracing,
request correlation, content masking, and controlled fault injection.

Lab 6 is a complete checkpoint of Lab 5. Participants first use the installed MCP server independently, then the application adds:

- `mcp==2.1.1` and Streamable HTTP.
- A fixed-target explorer for info, discovery, schema inspection, invocation, and live contract verification.
- A Strands `MCPClient` opened for each `/invoke` request.
- Exact validation that the provider exposes only `get_credit_score`.
- A synthetic score of `80` that is explicitly not a lending decision.

Every `/invoke` initializes and discovers MCP before the supervisor chooses a route. Provider failure therefore affects all invocation routes. For MCP, `GET /health/ready` checks the fixed configured URL identity without connecting to the server; it also resolves the Knowledge Base ID. The explorer `verify` operation performs the live protocol and tool-contract check.

## Provider Ownership

Workshop Studio owns and provisions:

```text
credit-services/service/credit-score-mcp:8081
http://credit-score-mcp.credit-services.svc.cluster.local:8081/mcp
```

Participants may inspect and call the provider, but must not modify, restart, replace, or delete its namespace, Deployment, pods, Service, endpoint, image, or configuration. Broad workshop permissions do not transfer ownership.

The standalone `00-workshop-setup` path does not create this provider or the `/workshop/mortgage-assistant/mcp/credit-score-url` Parameter Store value. Lab 6 requires a Workshop Studio-provisioned environment.

## Prerequisites

Install AWS CLI v2, Python 3.12 or later, `uv`, Docker with Buildx or a Docker-compatible builder, `kubectl`, `curl`, and `openssl`. Configure the intended AWS identity and Region before running deployment commands:

```bash
aws sts get-caller-identity
aws configure get region
```

Use only synthetic workshop data. Run Python commands through `uv run`; the
first invocation in each lab creates its local environment and installs the
locked dependencies automatically.

## Lab 1: Explore the Provisioned Environment

Query the Knowledge Base directly:

```bash
cd 01-test-knowledge-base
uv run query_knowledge_base.py \
  --query "What are the benefits of a 15-year mortgage?"
```

The Workshop Studio Lab 1 pages also inspect EKS and use the fixed-target Lab 6 explorer to initialize the installed MCP server, list and inspect `get_credit_score`, call it with a synthetic ID, and run `verify` before any agent integration.

## Lab 2: Run the Local Strands Application

```bash
cd ../02-local-strands
uv run mortgage_agent.py \
  --prompt "Compare 15-year and 30-year mortgages"
```

## Lab 3: Deploy the EKS Service

```bash
cd ../03-eks-service
./scripts/deploy-application.sh --region us-west-2
uv run app/invoke_eks.py \
  --prompt "When does refinancing make sense?"
```

The script discovers provisioned resources, builds and pushes a `linux/amd64` image, applies consumer Kubernetes resources, waits for two ready replicas and the NLB, and runs a smoke request.

## Lab 4: Add Memory

```bash
cd ../04-memory
uv run python -m unittest discover --start-directory tests --verbose
./scripts/deploy-memory.sh --region us-west-2
uv run app/invoke_eks.py --region us-west-2 --show-context
```

See [`04-memory/README.md`](04-memory/README.md) for the session, pod replacement, durable recall, actor-scoping, inspection, and cleanup exercises.

## Lab 5: Add OpenTelemetry and Langfuse Observability

```bash
cd ../05-observability
uv run python -m unittest discover --start-directory tests --verbose
./scripts/deploy-observability.sh --region us-west-2
```

See [`05-observability/README.md`](05-observability/README.md) for trace correlation, content masking, controlled fault injection, and Langfuse exercises.

## Lab 6: Integrate MCP Tools with the Strands Agent

```bash
cd ../06-mcp-credit-score
uv run python -m unittest discover \
  --start-directory tests \
  --verbose
```

Explore the pre-provisioned fixed provider:

```bash
uv run scripts/explore_credit_score_mcp.py info
uv run scripts/explore_credit_score_mcp.py list-tools
uv run scripts/explore_credit_score_mcp.py inspect-tool
uv run scripts/explore_credit_score_mcp.py \
  call-credit-score \
  --customer-id workshop-customer-12345
uv run scripts/explore_credit_score_mcp.py verify
```

Deploy only the updated consumer and invoke it:

```bash
./scripts/deploy-mcp-integration.sh --region us-west-2
uv run app/invoke_eks.py \
  --region us-west-2 \
  --prompt "Get the credit score for synthetic customer ID workshop-customer-12345."
```

The deployment preserves the existing `mortgage-assistant` NLB, Service, service account, Pod Identity association, Knowledge Base, DynamoDB memory resources, and OpenTelemetry configuration. It reapplies the API-key Secret while retaining its value unless explicitly overridden, and reads the existing Langfuse OTLP Secret without reapplying it. It verifies but does not modify `credit-services`.

See [`06-mcp-credit-score/README.md`](06-mcp-credit-score/README.md) for the complete contract, safety, observability, troubleshooting, and consumer-only cleanup exercises.

## Standalone Setup Limitation

To provision only the shared base resources outside Workshop Studio:

```bash
./00-workshop-setup/scripts/deploy-infrastructure.sh --region us-west-2
```

This is sufficient for Labs 1 through 4. It is not sufficient for Lab 5 or
Lab 6 because it does not provision the self-hosted Langfuse stack or the
provider-owned MCP service. Do not substitute another MCP URL or deploy an ad
hoc provider; use a Workshop Studio-provisioned environment for those labs.

## Cleanup

Each deployment module documents its own cleanup scope. Lab 6 cleanup deletes only the consumer `mortgage-assistant` namespace and NLB. It leaves provider-owned `credit-services` and shared AWS resources unchanged.

Shared infrastructure cleanup is destructive and charge-impacting. Run it only in a standalone environment you intentionally provisioned and only after confirming the AWS account and Region.
