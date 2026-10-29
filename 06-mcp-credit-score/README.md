# Lab 6: Add a credit-score MCP tool to the traced mortgage assistant

Lab 6 is a complete checkpoint of the simplified Lab 5 assistant. It keeps
OpenTelemetry/Langfuse tracing with application, environment, release
`mortgage-assistant-release-1.1`, and per-Agent generation attribution, plus a
latest-only supervisor session snapshot and actor-scoped durable memory. It then
adds one remote MCP tool named `get_credit_score`. Every request creates a fresh
Strands supervisor and fresh specialists. Only the supervisor has session and
memory managers.

Use only synthetic workshop data. The returned score is synthetic and this
workshop is not a production mortgage, lending, identity, credit, financial,
or compliance system.

## Learning objectives

After this lab, you can:

- inspect and invoke a pre-provisioned Streamable HTTP MCP server;
- enforce a fixed provider identity and exact tool contract;
- keep an MCP client open across supervisor construction, routing, and tool use;
- distinguish supervisor session state from actor-scoped durable memory; and
- correlate API, supervisor, specialist, model, local-tool, and MCP-tool spans
  in self-hosted Langfuse.

## Architecture

```text
client
  -> source-CIDR-restricted Network Load Balancer
  -> Kubernetes Service
  -> one of two non-root FastAPI pods
       -> bearer authentication and bounded validation
       -> root span: mortgage_assistant.invoke
       -> fresh Streamable HTTP MCPClient
            -> discover every tool page
            -> require exactly get_credit_score
       -> mortgage_supervisor (agent_id="supervisor")
            -> latest-only SnapshotSessionManager (TTL)
            -> actor-scoped MemoryManager (no TTL)
            -> CurrentPromptHook + ToolResultCapHook
            -> calculator
            -> get_credit_score remote tool
            -> three stateless specialist Agent.as_tool agents
       -> OTLP/HTTP exporter -> self-hosted Langfuse
```

The specialists retain stable names and agent IDs (`general`, `existing`, and
`new_application`) and return structured reports. They have no session manager,
memory manager, or persistence hooks. Their `preserve_context=True` state lasts
only for one fresh supervisor invocation. The supervisor receives exactly one
latest-only `SnapshotSessionManager`, one durable `MemoryManager`, and only
`CurrentPromptHook` and `ToolResultCapHook`.

## MCP contract and lifecycle

Workshop Studio deploys the MCP server before participants begin. Its required
identity is exactly:

```text
http://credit-score-mcp.credit-services.svc.cluster.local:8081/mcp
```

`CREDIT_SCORE_MCP_URL` cannot override this identity. Missing, malformed,
credential-bearing, or different URLs are rejected. The application uses the
pinned Streamable HTTP transport and a fresh Strands `MCPClient` for every
`/invoke`.

Each invocation follows this order:

1. validate the actor and session identifiers;
2. enter one `credit_score_tool()` context and initialize Streamable HTTP;
3. follow all tool-discovery pages;
4. require exactly one tool named `get_credit_score` and reject missing,
   renamed, duplicate, or additional tools;
5. construct the supervisor with that remote tool; and
6. keep the MCP client context open until the supervisor invocation finishes.

Discovery happens before the supervisor routes the prompt. MCP initialization,
availability, or contract failures therefore affect every `/invoke`, including
requests that would otherwise use only memory, the Knowledge Base, calculator,
or a local specialist. A returned tool-level error remains available for the
supervisor to report safely.

`GET /health/ready` validates only the configured fixed URL identity and also
resolves the Knowledge Base ID. It does not connect to the MCP server, discover
tools, invoke a tool, or disclose the URL. A configured but unavailable server
can leave readiness green while `/invoke` fails. The independent explorer's
`verify` command performs the live protocol and contract check.

## MCP server ownership boundary

The credit-services team owns the `credit-services` namespace, two
`credit-score-mcp` pods, ClusterIP Service, endpoint, image, configuration, and
tool contract. Lab 6 only reads Deployment, Service, and endpoint state and
invokes the fixed server. Participants must not modify, restart, scale, replace,
or delete any credit-services resource, even when workshop permissions permit
it.

The deployment and cleanup scripts modify only the participant-owned
`mortgage-assistant` namespace. Cleanup leaves `credit-services`, the fixed MCP
SSM parameter, and shared AWS resources unchanged.

## Credit-score safety

The supervisor calls `get_credit_score` only for an explicit credit-score
request containing a synthetic customer ID. It treats score `80` as data only
and never interprets it as approval, denial, pricing, eligibility, advice,
compliance evidence, or a lending decision. It must not invent scores and must
report tool errors. Customer IDs and credit scores must never be written to
durable memory.

Durable memory is limited to synthetic mortgage goals and preferences. Do not
store customer IDs, account numbers, credentials, exact income, uploaded
documents, credit information, or real personal data. Telemetry masking is a
workshop safeguard, not authorization to use sensitive data.

## Memory behavior

All state is partitioned by validated actor:

```text
pk=user/<actor_id>
```

Only the supervisor writes the short-term snapshot:

```text
sk=session/<session_id>/scopes/agent/supervisor/snapshots/snapshot_latest.json
```

It is overwritten after messages and receives `MEMORY_SESSION_TTL_SECONDS`,
seven days by default. `CurrentPromptHook` reapplies the deployed prompt after
restoration. `ToolResultCapHook` bounds local and MCP tool text before snapshot
persistence.

Durable memories use `sk=memories/<uuid>`, normalized 1,024-dimension Titan
embeddings, and vector searches explicitly scoped to `pk=user/<actor_id>`.
Durable memories have no TTL.

## API and authentication

The service exposes exactly three application routes:

- `GET /health` for liveness;
- `GET /health/ready` for non-live configuration readiness; and
- `POST /invoke` for authenticated agent requests.

The probe routes are unauthenticated so Kubernetes and the NLB can use them.
`POST /invoke` requires `Authorization: Bearer <key>`, applies bounded Pydantic
validation with unknown fields forbidden, and accepts only:

```json
{
  "prompt": "Get the credit score for synthetic customer ID workshop-customer-12345.",
  "actor_id": "participant-123456789012",
  "session_id": "session-example"
}
```

The API generates the request ID; clients cannot supply one. The response is:

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

`trace_id` is null when tracing is disabled or unavailable. Errors return a
safe message containing the generated correlation ID without exposing provider
or MCP details. A bounded semaphore returns HTTP 429 when all agent slots are
busy.

## Langfuse tracing

`telemetry.init_telemetry()` runs before `mortgage_agent` is imported by the
API and initializes at most once. Each invocation opens the safe root span
`mortgage_assistant.invoke`; Strands adds supervisor, specialist, model, local
tool, and MCP tool spans below it. Trace attributes carry `session.id`,
`user.id`, and a generated request tag through a request-local context that is
always restored.

The deployment reuses the existing Lab 5 `langfuse-otel-auth` Secret after
validating its headers. It does not print the secret. With
`--telemetry-mask-content`, Strands native redaction and an exporter wrapper
mask prompts, responses, system instructions, and tool arguments/results before
export. Telemetry setup, span, trace-ID, export, and shutdown failures never
block request handling.

## Explore the installed MCP server

The fixed-target explorer accepts no arbitrary server URL. It creates a
temporary loopback-only `kubectl port-forward`, initializes a real MCP session,
follows all tool pages, validates the singleton contract, and always terminates
the tunnel.

```bash
uv run scripts/explore_credit_score_mcp.py info
uv run scripts/explore_credit_score_mcp.py list-tools
uv run scripts/explore_credit_score_mcp.py inspect-tool
uv run scripts/explore_credit_score_mcp.py call-credit-score \
  --customer-id workshop-customer-12345
uv run scripts/explore_credit_score_mcp.py verify
```

`verify` invokes the tool with a synthetic ID and checks the partial structured
result: echoed customer ID, score `80`, and source `synthetic-workshop`.

## Local validation

Run from `06-mcp-credit-score`. Tests are offline and do not call AWS,
Kubernetes, Bedrock, the deployed MCP server, or external networks.

```bash
.venv/bin/python -m unittest discover --start-directory tests --verbose
uv lock --check
bash -n scripts/deploy-mcp-integration.sh scripts/cleanup-mcp-integration.sh
```

The suite includes a real local loopback Streamable HTTP fixture in addition to
unit and source-contract tests.

## Resource discovery

| Resource | Parameter Store path |
| --- | --- |
| EKS cluster | `/workshop/mortgage-assistant/eks/cluster-name` |
| ECR repository | `/workshop/mortgage-assistant/ecr/repository-uri` |
| Knowledge Base | `/workshop/mortgage-assistant/bedrock/knowledge-base-id` |
| Memory table | `/workshop/mortgage-assistant/memory/table-name` |
| Vector index | `/workshop/mortgage-assistant/memory/vector-index-name` |
| Langfuse OTLP endpoint | `/workshop/mortgage-assistant/langfuse/otlp-endpoint` |
| Langfuse UI | `/workshop/mortgage-assistant/langfuse/url` |
| Credit-score MCP URL | `/workshop/mortgage-assistant/mcp/credit-score-url` |

## Deploy

Deployment builds and pushes an image and changes charge-incurring resources.
Confirm the account, Region, credentials, and intended workshop environment
before running it.

```bash
./scripts/deploy-mcp-integration.sh --region us-west-2
```

The script discovers shared resources, requires the exact MCP URL, checks
memory readiness, performs read-only credit-services inspection, runs explorer
`verify`, validates and reuses the existing Langfuse Secret, builds and pushes
a Linux AMD64 image unless `--image-uri` is supplied, applies only
mortgage-assistant resources, waits for rollout/readiness/NLB, and sends one
authenticated MCP smoke prompt whose body contains only prompt, actor, and
session. The smoke check requires the reduced response contract and a valid
trace ID.

The manifest preserves two replicas, zero-unavailable rolling updates, startup,
readiness, and liveness probes, a PodDisruptionBudget, Pod Identity service
account, disabled service-account token mounting, CPU/memory and temporary
storage bounds, non-root execution, read-only root filesystem, dropped
capabilities, RuntimeDefault seccomp, bearer authentication, and an
internet-facing NLB restricted to the selected source CIDR. The workshop NLB
uses plaintext HTTP; a production service must use TLS and caller-bound
authentication.

## Invoke

The client discovers the NLB and API-key Secret unless explicit values are
provided. It derives the default actor from the AWS account and stores only the
current session per actor in `.workshop/client-state.json`.

```bash
uv run app/invoke_eks.py --region us-west-2 --show-context
uv run app/invoke_eks.py --region us-west-2 \
  --prompt "Get the credit score for synthetic customer ID workshop-customer-12345."
uv run app/invoke_eks.py --region us-west-2 \
  --prompt "Remember that I prefer a 15-year fixed-rate mortgage."
uv run app/invoke_eks.py --region us-west-2 --new-session \
  --prompt "What mortgage term do I prefer?"
```

The client prints the server-generated request ID and trace ID and stores only
the current session selection.

## Inspect memory

```bash
uv run app/inspect_memory.py --region us-west-2 --sessions
uv run app/inspect_memory.py --region us-west-2 --memories
uv run app/inspect_memory.py --region us-west-2 --search "preferred term"
```

Expect only supervisor `snapshot_latest.json` session keys. Inspection and
hydration use synthetic test data and do not change the MCP server.

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
| `CREDIT_SCORE_MCP_URL` | required fixed URL | Streamable HTTP MCP endpoint. |
| `MORTGAGE_API_KEY` | required | Bearer credential. |
| `AGENT_CONCURRENCY` | `4` | Concurrent agent turns per pod. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | optional | Self-hosted Langfuse OTLP/HTTP URL. |
| `OTEL_EXPORTER_OTLP_HEADERS` | Secret-backed | Langfuse authorization headers. |
| `TELEMETRY_MASK_CONTENT` | `false` | Enable native/exporter redaction. |

## Trade-offs

- Concurrent calls for one actor/session can restore the same latest snapshot;
  the last writer wins.
- Specialists are stateless across HTTP requests. Multi-turn collection is
  represented in the supervisor transcript and delegated again.
- The shared bearer key does not bind caller-supplied `actor_id`; production
  systems must derive and authorize identity from verified credentials.
- MCP discovery on every invocation enforces the provider contract but adds
  latency and makes all routes depend on MCP availability.
- Vector search is eventually consistent, and batch trace export means
  Langfuse may display a trace shortly after the response.

## Cleanup

```bash
./scripts/cleanup-mcp-integration.sh --region us-west-2
```

Cleanup removes only participant-owned `mortgage-assistant` resources. It does
not modify or delete credit-services, the MCP SSM endpoint, EKS, ECR, the
Knowledge Base, DynamoDB memory, or self-hosted Langfuse.
