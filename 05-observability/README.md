# Lab 5: Observe the memory-enabled assistant with Langfuse

Lab 5 adds OpenTelemetry tracing and self-hosted Langfuse to the simplified
Lab 4 mortgage assistant. Every request creates a fresh Strands supervisor and
fresh specialists. Only `mortgage_supervisor` has persistence: a latest-only
DynamoDB session snapshot and actor-scoped semantic memory. The specialists
remain structured `Agent.as_tool` agents with stable names, IDs, prompts, and
tools, but no session or memory manager.

Langfuse runs on the workshop EKS cluster with Aurora PostgreSQL, ElastiCache
for Valkey, Amazon S3, and in-cluster ClickHouse. Agents export OTLP/HTTP traces
over the cluster network. The UI is reached through the Workshop Studio
CloudFront URL and private VPC origin. This checkpoint does not use Bedrock
AgentCore, X-Ray, or CloudWatch Application Signals.

Use only synthetic workshop data. This is not a production mortgage, lending,
identity, credit, or compliance system.

## Learning objectives

After this lab, you can:

- relate the root API span, supervisor span, specialist span, model calls, and
  tool calls in one Langfuse trace;
- inspect latency, token usage, routing, tool duration, and the Agent that owns
  each model generation;
- group multiple requests by `session.id` and `user.id`;
- use a generated request ID for ephemeral log and trace correlation;
- compare request and tool latency across normal traces;
- explain native and exporter-side content masking; and
- distinguish supervisor session state from actor-scoped durable memory.

## Architecture

```text
client
  -> source-CIDR-restricted Network Load Balancer
  -> Kubernetes Service
  -> one of two non-root FastAPI pods
       -> bearer authentication and bounded validation
       -> root span: mortgage_assistant.invoke
       -> mortgage_supervisor (agent_id="supervisor")
            -> latest-only SnapshotSessionManager (TTL)
            -> actor-scoped MemoryManager (no TTL)
            -> CurrentPromptHook + ToolResultCapHook
            -> calculator
            -> mortgage_education_specialist as stateless Agent tool
            -> existing_mortgage_specialist as stateless Agent tool
            -> mortgage_application_specialist as stateless Agent tool
       -> OTLP/HTTP exporter -> self-hosted Langfuse
```

`telemetry.init_telemetry()` runs before `mortgage_agent` is imported by the
API. The provider is initialized once per process. Each `/invoke` opens the safe
root span `mortgage_assistant.invoke`; Strands adds supervisor, specialist,
model, and tool spans below it. The API remains available if telemetry setup,
span operations, trace-ID lookup, export, or shutdown flushing fails.

The request context carries shared attributes through a
`contextvars.ContextVar`. Each Agent receives a copied mapping with its own
`langfuse.observation.metadata.agent_name`, so nested model generations can be
attributed directly:

| Attribute | Value |
| --- | --- |
| `session.id` | Caller-selected session ID used by Langfuse Sessions. |
| `user.id` | Caller-selected workshop actor used by Langfuse Users. |
| `tags` | Application, release, and `request:<generated-request-id>`. |
| `langfuse.trace.metadata.application` | `mortgage-assistant` |
| `langfuse.trace.metadata.environment` | `workshop` |
| `langfuse.environment` | `workshop` |
| `langfuse.release` | `mortgage-assistant-release-1.0` |
| `langfuse.observation.metadata.agent_name` | `mortgage_supervisor` or the selected specialist. |

The context is reset after every invocation, including failures, so concurrent
requests do not inherit another request's identifiers. The 32-character trace
ID is captured while the root span is active and returned as nullable
`trace_id`.

## Memory behavior

All memory is partitioned by validated actor:

```text
pk=user/<actor_id>
```

The supervisor's one short-term snapshot is:

```text
sk=session/<session_id>/scopes/agent/supervisor/snapshots/snapshot_latest.json
```

It is overwritten at message level and receives
`MEMORY_SESSION_TTL_SECONDS`, seven days by default. There is no immutable
history and no specialist snapshot. `CurrentPromptHook` reapplies the deployed
supervisor prompt after restoration, and `ToolResultCapHook` bounds text before
it enters the snapshot.

Durable memories use `sk=memories/<uuid>`, normalized 1,024-dimension Titan
embeddings, and explicit `pk=user/<actor_id>` vector searches. Durable memories
have no TTL. The prompt restricts memory to synthetic mortgage goals and
preferences; never store customer IDs, account numbers, credentials, exact
income, documents, credit information, or real personal data.

Old execution, lock, ledger, approval, or other operational DynamoDB records
from earlier revisions are ignored. Deployment and cleanup do not migrate or
delete them.

## Telemetry export and masking

The deploy script discovers the Langfuse OTLP endpoint and UI URL from
canonical Parameter Store paths. It reads the already-provisioned project keys
from the in-cluster `langfuse-web` Deployment and streams the derived Basic
authorization header through stdin into `langfuse-otel-auth`. Credential values
are never placed in child-process arguments or rendered into temporary files.
The Deployment reads `OTEL_EXPORTER_OTLP_HEADERS` from that Secret and uses the
in-cluster OTLP/HTTP endpoint.

`TELEMETRY_MASK_CONTENT=false` is the workshop default so traces can be
inspected. With `--telemetry-mask-content`, the process enables Strands native
redaction and wraps the OTLP exporter with a second redaction layer. Prompt,
response, system instruction, tool argument/result, query, and result
attributes are replaced before export. Operational identifiers, tool names,
status, latency, and token metadata remain. Do not place sensitive data in
exception messages; status descriptions and exception text are not rewritten.

Telemetry shutdown calls the process-wide provider's shutdown method so the
batch processor can flush. Shutdown errors are logged and ignored.

## Prerequisites and local validation

Complete Lab 4 in a Workshop Studio environment. Workshop Studio must already
provide EKS, ECR, the Knowledge Base, DynamoDB memory, and self-hosted Langfuse.
Commands run from `agent-eks/05-observability`.

Local tests are fully offline: they do not call AWS, EKS, Bedrock, a model, or a
deployed service.

```bash
uv run python -m unittest discover --start-directory tests --verbose
uv lock --check
```

Canonical discovery paths:

| Resource | Parameter Store path |
| --- | --- |
| EKS cluster | `/workshop/mortgage-assistant/eks/cluster-name` |
| ECR repository | `/workshop/mortgage-assistant/ecr/repository-uri` |
| Knowledge Base | `/workshop/mortgage-assistant/bedrock/knowledge-base-id` |
| Memory table | `/workshop/mortgage-assistant/memory/table-name` |
| Vector index | `/workshop/mortgage-assistant/memory/vector-index-name` |
| Langfuse OTLP endpoint | `/workshop/mortgage-assistant/langfuse/otlp-endpoint` |
| Langfuse UI | `/workshop/mortgage-assistant/langfuse/url` |

## Deploy

Deployment changes charge-incurring resources. Confirm the account, Region,
credentials, and intended workshop environment before running it.

```bash
./scripts/deploy-observability.sh --region us-west-2
```

The script preserves Langfuse discovery and Secret handling, verifies memory,
builds and pushes a `linux/amd64` image unless `--image-uri` is supplied,
renders the source-restricted Service, waits for the rolling two-replica
rollout, NLB, and readiness, then sends one normal authenticated smoke request.
It validates the reduced response and its trace ID and prints the image, API,
Langfuse UI, trace ID, and generated correlation ID.

Options:

| Option | Purpose |
| --- | --- |
| `--region REGION` | Select the AWS Region. |
| `--profile PROFILE` | Use a named AWS CLI profile. |
| `--service-access-cidr CIDR` | Restrict NLB access; defaults to caller IPv4 `/32`. |
| `--session-ttl-seconds N` | Set latest supervisor snapshot retention. |
| `--prompt TEXT` | Set the one smoke prompt. |
| `--telemetry-mask-content` | Enable native and exporter redaction. |
| `--image-uri URI` | Skip build/push and deploy an existing image. |

The manifest keeps two replicas, zero-unavailable rolling updates, startup,
readiness, and liveness probes, a PodDisruptionBudget, Pod Identity service
account, disabled service-account token mounting, CPU/memory and temporary
storage bounds, non-root execution, read-only root filesystem, dropped
capabilities, RuntimeDefault seccomp, bearer authentication, and a
source-restricted NLB.

## Invoke

The client discovers the NLB and API-key Secret unless `--url` and `--api-key`
are supplied. It derives the default actor from the AWS account and stores only
the current session per actor in `.workshop/client-state.json`.

```bash
uv run app/invoke_eks.py --region us-west-2 --show-context
uv run app/invoke_eks.py --region us-west-2 \
  --prompt "I am considering a property worth 600,000 dollars."
uv run app/invoke_eks.py --region us-west-2 \
  --prompt "What property value did I mention?"
uv run app/invoke_eks.py --region us-west-2 --new-session \
  --prompt "What mortgage term do I prefer?"
```

The client retains `--actor-id`, `--session-id`, `--new-session`,
`--show-context`, Kubernetes discovery, `--url`, `--api-key`, `--profile`,
`--region`, `--namespace`, `--service`, `--secret`, `--timeout`, `--json`, and
`--state-file`. It prints the request and trace IDs when available.

## API

`GET /health` and `GET /health/ready` are unauthenticated probe endpoints.
`POST /invoke` requires `Authorization: Bearer <key>` and accepts only:

```json
{
  "prompt": "What did I tell you?",
  "actor_id": "participant-123456789012",
  "session_id": "session-example"
}
```

The response is:

```json
{
  "request_id": "generated-correlation-id",
  "actor_id": "participant-123456789012",
  "session_id": "session-example",
  "response": "...",
  "duration_ms": 1234,
  "trace_id": "0123456789abcdef0123456789abcdef"
}
```

`trace_id` is null when tracing is disabled or unavailable. `request_id` is
generated by the API and used only for ephemeral logs and the Langfuse request
tag. It is not accepted from clients and is not an idempotency key.

## Langfuse exercises

1. Open the deployment smoke trace by its printed trace ID. Follow
   `mortgage_assistant.invoke` into the supervisor, selected specialist, model
   generations, memory, and tool spans that are present.
2. Select a `generation` observation and inspect its model, input/output/total
   tokens, latency, estimated cost when available, and
   `metadata.agent_name`. Use generation values rather than cumulative Agent
   span totals to avoid double-counting model usage.
3. Open Langfuse's managed Cost Dashboard, select a time range containing the
   smoke request, and explore its aggregate project views. Do not create,
   duplicate, or edit a dashboard for this exercise.

## Inspect memory

```bash
uv run app/inspect_memory.py --region us-west-2 --sessions
uv run app/inspect_memory.py --region us-west-2 --memories
uv run app/inspect_memory.py --region us-west-2 --search "preferred term"
```

Expect only supervisor `snapshot_latest.json` session keys. Memory hydration and
explicit clear commands are test-data utilities; normal deployment, invocation,
and cleanup do not delete records.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `MODEL_ID` | `us.anthropic.claude-sonnet-4-6` | Supervisor and specialist model. |
| `KB_PARAMETER_NAME` | required | Knowledge Base parameter path. |
| `MEMORY_TABLE_NAME` | required | Shared DynamoDB table. |
| `MEMORY_VECTOR_INDEX_NAME` | `vector_index` | DynamoDB vector index. |
| `MEMORY_EMBEDDING_MODEL_ID` | `amazon.titan-embed-text-v2:0` | Embeddings. |
| `MEMORY_SESSION_TTL_SECONDS` | `604800` | Latest supervisor snapshot TTL. |
| `MEMORY_MAX_SEARCH_RESULTS` | `5` | Actor-scoped search limit. |
| `MAX_TOOL_RESULT_CHARS` | `16000` | Maximum retained tool-result block. |
| `MORTGAGE_API_KEY` | required | Bearer credential. |
| `AGENT_CONCURRENCY` | `4` | Concurrent agent turns per pod. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | optional | Self-hosted Langfuse OTLP/HTTP URL. |
| `OTEL_EXPORTER_OTLP_HEADERS` | Secret-backed | Langfuse authorization headers. |
| `OTEL_SERVICE_NAME` | `mortgage-assistant` | Trace service name. |
| `TELEMETRY_MASK_CONTENT` | `false` | Enable native/exporter redaction. |

## Trade-offs

- There is no cross-pod session lease. Concurrent calls for one actor/session
  can restore the same snapshot and overwrite each other; last writer wins.
- A process failure can leave an external side effect or uncertain client
  result. Retrying starts a new invocation.
- Specialists are stateless across HTTP requests. Multi-turn application field
  collection must be represented in the supervisor transcript and delegated
  again.
- The shared bearer key does not prove caller-supplied `actor_id`. Production
  systems should derive identity from verified credentials and authorize it.
- The source-restricted NLB uses plaintext HTTP. Production systems should use
  TLS and an appropriate authenticated private or edge endpoint.
- Prompt-based memory restrictions and trace masking are workshop safeguards,
  not a complete data-governance boundary.
- Vector search is eventually consistent, and batch export makes Langfuse
  traces appear shortly after a response rather than synchronously.

## Cleanup

```bash
./scripts/cleanup-observability.sh --region us-west-2
```

Cleanup removes only the participant-owned `mortgage-assistant` namespace and
load balancer, including `langfuse-otel-auth`. It leaves the shared EKS cluster,
ECR repository, Knowledge Base, DynamoDB records, and self-hosted Langfuse
resources managed by Workshop Studio.

Lab 5 is complete when only the supervisor latest snapshot is created, durable
memory remains actor scoped, two replicas are healthy, the API returns a
nullable trace ID, traces preserve request/session/user context without leaking
between calls, model generations carry application, environment, release, and
owning-Agent attribution, masking works before export, and telemetry failures
do not block the service.
