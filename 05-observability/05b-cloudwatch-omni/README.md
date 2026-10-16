# Lab 5b: Observe the memory-enabled assistant with Amazon CloudWatch Omni

This optional side lab sends traces from the simplified, memory-enabled Strands
mortgage assistant on Amazon EKS to Amazon CloudWatch Omni. Each request creates
a fresh supervisor and fresh specialists. Only `mortgage_supervisor` persists
state: one latest-only DynamoDB session snapshot with TTL and actor-scoped
semantic memory without TTL. The specialists remain structured `Agent.as_tool`
agents with their existing names, IDs, prompts, and tools, but no session or
memory manager.

Lab 5b deploys a separate `mortgage-assistant-omni` service. It does not require
Lab 5a or Langfuse and does not modify Lab 5a resources. Use only synthetic
workshop data; this is not a production mortgage, lending, identity, credit, or
compliance system.

## What differs from Lab 5a

| Piece | Lab 5a | Lab 5b |
| --- | --- | --- |
| Destination | Self-hosted Langfuse | CloudWatch Omni through Transaction Search |
| Provider/exporter owner | Application telemetry | ADOT `opentelemetry-instrument` |
| Export authentication | Langfuse Secret | EKS Pod Identity and SigV4 |
| Kubernetes objects | `mortgage-assistant`, two replicas, PDB | `mortgage-assistant-omni`, one replica, no PDB |
| Content setting | Optional Langfuse masking | Synthetic content retained for Omni exercises |

Lab 5b intentionally preserves its one-replica, no-PDB lesson topology. It does
not inherit Lab 5a's resource layout.

## Architecture

```text
client
  -> source-CIDR-restricted Network Load Balancer
  -> mortgage-assistant-omni Service
  -> one non-root FastAPI pod
       -> bearer authentication and bounded input
       -> safe span: mortgage_assistant.invoke
       -> mortgage_supervisor (agent_id="supervisor")
            -> latest-only SnapshotSessionManager (TTL)
            -> actor-scoped MemoryManager (no TTL)
            -> CurrentPromptHook + ToolResultCapHook
            -> calculator
            -> three stateless specialist Agent tools
       -> ADOT/X-Ray OTLP -> Transaction Search -> CloudWatch Omni
```

The public API contains only unauthenticated `GET /health`, unauthenticated
`GET /health/ready`, and authenticated `POST /invoke`. The API generates a
request ID for logs and trace correlation. It is not accepted from clients and
is not an idempotency or replay key.

## Memory behavior

All memory is partitioned by validated actor:

```text
pk=user/<actor_id>
```

Only the supervisor writes session state:

```text
sk=session/<session_id>/scopes/agent/supervisor/snapshots/snapshot_latest.json
```

The snapshot is overwritten at message level and receives
`MEMORY_SESSION_TTL_SECONDS`, seven days by default. No immutable snapshot
history or specialist snapshots are created. `CurrentPromptHook` reapplies the
deployed supervisor prompt after restoration, and `ToolResultCapHook` bounds
text before persistence.

Durable memories use `sk=memories/<uuid>`, normalized 1,024-dimension Titan
embeddings, and vector searches that explicitly include the actor partition.
They have no TTL. Never store customer IDs, account numbers, credentials, exact
income, documents, credit information, or real personal data. Old audit,
execution, lock, ledger, approval, or other operational records from prior
revisions are ignored; deployment and cleanup do not migrate or delete them.

## ADOT and CloudWatch ownership

The Docker entrypoint is:

```text
opentelemetry-instrument uvicorn mortgage_api:app
```

ADOT installs the process-wide tracer provider before the application imports
Strands. `app/telemetry.py` never creates or replaces the global provider and
never adds a span processor or exporter. It passes the existing provider to
`StrandsTelemetry`, then uses that provider for the safe request span and
nullable trace ID. Without ADOT, tracing stays disabled and no exporter or
network client is created. Telemetry configuration, span, trace-ID, and
shutdown failures are fail-open.

The manifest preserves these CloudWatch settings:

- `AGENT_OBSERVABILITY_ENABLED`
- `AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT`
- `OTEL_PYTHON_DISTRO` and `OTEL_PYTHON_CONFIGURATOR`
- `OTEL_RESOURCE_ATTRIBUTES`
- `OTEL_EXPORTER_OTLP_TRACES_HEADERS`
- `OTEL_EXPORTER_OTLP_PROTOCOL`
- `OTEL_TRACES_EXPORTER`, `OTEL_LOGS_EXPORTER`, and `OTEL_METRICS_EXPORTER`

`OTEL_EXPORTER_OTLP_ENDPOINT` must remain unset so the AWS configurator derives
the regional X-Ray endpoint. Langfuse OTLP headers, endpoint settings, Secrets,
and masking variables are intentionally absent.

`AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT=true` keeps synthetic prompt, response,
and tool content on spans for this exercise. For real data, review CloudWatch
Omni data-protection guidance before changing the workshop posture.

## Verification status

Local tests are offline and do not call AWS, EKS, Bedrock, models, or deployed
services:

```bash
uv run python -m unittest discover --start-directory tests --verbose
uv lock --check
```

The AWS deployment path, Transaction Search ingestion, Omni domain, and final
span layout require a workshop environment. A returned trace ID proves a real
provider and active span; it does not by itself prove CloudWatch ingestion.
Allow several minutes before looking for a trace in Omni.

## Deploy

Deployment changes charge-incurring resources. Confirm the account, Region,
credentials, and intended workshop environment before running:

```bash
./scripts/deploy-omni.sh --region us-west-2
```

The script:

1. Discovers the EKS cluster, ECR repository, Knowledge Base, DynamoDB memory,
   trace log group, and Omni domain from canonical Parameter Store paths.
2. Requires Transaction Search plus the memory table and vector index to be
   active.
3. Preserves the shared namespace and ServiceAccount, creates the dedicated
   `mortgage-assistant-omni-api-key` Secret, and creates the dedicated trace log
   stream.
4. Builds and pushes a `linux/amd64` image unless `--image-uri` is supplied.
5. Renders the source-restricted one-replica manifest, waits for rollout, the
   NLB, and two consecutive readiness successes.
6. Sends one authenticated smoke request and validates the reduced response,
   generated request ID, duration, and 32-character trace ID.
7. Prints the endpoint, trace log group, trace ID, and Omni URLs without
   printing the API key.

Supported options are `--region`, `--profile`, `--service-access-cidr`,
`--prompt`, `--session-ttl-seconds`, and `--image-uri`.

## Invoke and inspect traces

The client defaults remain specific to Lab 5b:

- namespace: `mortgage-assistant`
- Service: `mortgage-assistant-omni`
- API-key Secret: `mortgage-assistant-omni-api-key`

It stores only the current session per actor in `.workshop/client-state.json`.

```bash
uv run app/invoke_eks.py --region us-west-2 --show-context
uv run app/invoke_eks.py --region us-west-2 \
  --prompt "I am considering a property worth 600,000 dollars."
uv run app/invoke_eks.py --region us-west-2 \
  --prompt "What property value did I mention?"
uv run app/invoke_eks.py --region us-west-2 --new-session \
  --prompt "What mortgage term do I prefer?"
./scripts/test-agent.sh --region us-west-2
```

The test script exercises the Knowledge Base specialist, existing-mortgage
specialist, and a two-turn supervisor session. It prints each server-generated
request ID and trace ID for lookup in Omni.

## API

`POST /invoke` accepts only:

```json
{
  "prompt": "What did I tell you?",
  "actor_id": "participant-123456789012",
  "session_id": "session-example"
}
```

The reduced traced response is:

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

`trace_id` is null when ADOT is unavailable. The same `session.id`, `user.id`,
and `request:<request_id>` trace attributes are applied while the supervisor
and stateless specialists are constructed and invoked, then the context is
restored.

## Inspect memory

```bash
uv run app/inspect_memory.py --region us-west-2 --sessions
uv run app/inspect_memory.py --region us-west-2 --memories
uv run app/inspect_memory.py --region us-west-2 --search "preferred term"
```

Expect only supervisor `snapshot_latest.json` session keys. Memory hydration
and explicit clear commands are test-data utilities; normal deployment,
invocation, and cleanup do not delete records.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `MODEL_ID` | `us.anthropic.claude-sonnet-4-6` | Supervisor and specialist model |
| `KB_PARAMETER_NAME` | required | Knowledge Base parameter path |
| `MEMORY_TABLE_NAME` | required | Shared DynamoDB table |
| `MEMORY_VECTOR_INDEX_NAME` | `vector_index` | DynamoDB vector index |
| `MEMORY_EMBEDDING_MODEL_ID` | `amazon.titan-embed-text-v2:0` | Embeddings |
| `MEMORY_SESSION_TTL_SECONDS` | `604800` | Latest supervisor snapshot TTL |
| `MEMORY_MAX_SEARCH_RESULTS` | `5` | Actor-scoped search limit |
| `MAX_TOOL_RESULT_CHARS` | `16000` | Retained tool-result block limit |
| `MORTGAGE_API_KEY` | required | Bearer credential |
| `AGENT_CONCURRENCY` | `4` | Concurrent agent turns in the pod |

## Trade-offs

- There is no cross-pod session lease. Concurrent calls for one actor/session
  can restore the same snapshot and overwrite each other; last writer wins.
- A process failure can leave an external side effect or uncertain client
  result. Retrying starts a new invocation.
- Specialists are stateless across requests. Multi-turn field collection must
  remain in the supervisor transcript and be delegated again.
- The shared bearer key does not prove caller-supplied `actor_id`.
- The source-restricted NLB uses plaintext HTTP as a workshop simplification.
- Prompt-based memory restrictions are not a complete governance boundary.
- Vector search and trace ingestion are eventually consistent.

## Cleanup

```bash
./scripts/cleanup-omni.sh --region us-west-2
```

Cleanup removes only the `mortgage-assistant-omni` Deployment, Service, and
API-key Secret. It leaves Lab 5a, the shared namespace and ServiceAccount,
Workshop Studio resources, DynamoDB records, trace log group, and traces.
