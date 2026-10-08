# Lab 5b: Observe the Strands mortgage assistant with Amazon CloudWatch Omni

This optional side lab sends the traces of the audited, resumable multi-agent
mortgage assistant running on Amazon EKS to Amazon CloudWatch Omni, so you can
see what each agent, model call, and tool call did, how long it took, and how
many tokens it used. It is the same application as Lab 5a. Only the trace
export path differs: the AWS Distro for OpenTelemetry (ADOT) writes to
CloudWatch instead of the application writing to Langfuse.

The lab deploys a second, parallel service, `mortgage-assistant-omni`, in the
same namespace. It does not need Lab 5a or Langfuse, and it does not change any
Lab 5a resource.

## What you build on

Lab 5b is a complete checkpoint of Lab 5a with a different export path. This
README documents only what differs. For the rest of the service, see:

- [Lab 4 (`../../04-memory/README.md`)](../../04-memory/README.md): the FastAPI
  service, the supervisor with three specialists as agents-as-tools (each a
  persistent `Agent` with its own session), DynamoDB session snapshots and
  long-term memory, the hash-chained audit trail, `request_id` idempotency with
  resume and replay, the tool ledger, human approvals, and the
  `app/invoke_eks.py` client.
- [Lab 5a (`../05a-langfuse/README.md`)](../05a-langfuse/README.md): the
  OpenTelemetry tracing concepts, the root request span, the `trace_id` on
  audit records and in API responses, and fault injection. If you did Lab 5a,
  the trace structure here is the same.

What Lab 5b changes relative to Lab 5a:

| Piece | Lab 5a | Lab 5b |
| --- | --- | --- |
| Destination | Self-hosted Langfuse | Amazon CloudWatch Omni (Transaction Search) |
| Exporter | Strands' OTLP exporter in `app/telemetry.py` | ADOT (`opentelemetry-instrument`), SigV4-signed |
| Credentials | `langfuse-otel-auth` Secret from Secrets Manager | EKS Pod Identity role, no Secret |
| Kubernetes objects | `mortgage-assistant` Deployment (2 replicas, PodDisruptionBudget) | `mortgage-assistant-omni` Deployment (1 replica, no PodDisruptionBudget), Service, and API-key Secret |
| Content masking | `--telemetry-mask-content` | Not offered; content stays on the spans (see below) |
| Deploy script | `deploy-observability.sh` | `deploy-omni.sh` (adds the Transaction Search check) |

## Verification status

The unit tests in this lab pass (`uv run python -m unittest discover tests`),
and they cover the application, the telemetry hand-off to ADOT, the manifest,
and the deploy script's contents. The AWS deployment path of this lab could not
be exercised in the test account: Transaction Search, the Omni domain and space,
the ADOT export, and the traces you will open in Omni have not been run end to
end. Treat the deploy script, the exact Omni menu names, and the "expected"
span layout below as unverified until a workshop run confirms them.

## Estimated time

Allow about 50 minutes: roughly 10 minutes of reading, 5 of typing, 17 of
waiting (the image build and rollout take about 12 minutes; traces take about
5 minutes to appear), and 18 of exploring traces in Omni. A first image build
with a cold cache adds about 10 minutes.

## What CloudWatch Omni is

CloudWatch Omni is an AI-powered observability experience built on Amazon
CloudWatch. It has its own sign-in URL and is organized as:

- **Domain**: your sign-in boundary, `https://<domain-name>.cloudwatch-omni.global.app.aws`.
- **Space**: where you work. One space maps to one account and Region.
- **CloudWatch Dataset**: the logs and traces that Omni correlates for you.

For AI agents, Omni provides:

- **Agent traces**: a span waterfall of each request, including model calls,
  tool calls, inputs, outputs, and token usage.
- **Agents**: latency, error, and token trends per agent.
- **Evaluators**: quality scores on the same trace, from a judge model or your
  own criteria, because an agent can return a well-formed answer that is wrong.
- **Omni agent**: ask questions about your telemetry in plain language.

Omni is a view over CloudWatch data. It reads the traces that the OpenTelemetry
(OTel) exporter writes into CloudWatch through Transaction Search. Nothing else
in CloudWatch changes.

## How it is used with Strands on EKS

```text
Strands agents (supervisor + specialists)
   |  built-in OpenTelemetry spans: agent, model, tool
   v
AWS Distro for OpenTelemetry (ADOT) in the pod
   |  OTLP/HTTP, SigV4-signed with the pod's EKS Pod Identity role
   v
X-Ray OTLP endpoint -> Transaction Search -> CloudWatch log group
   v
CloudWatch Omni: Agent traces, Agents, Evaluators
```

1. **Strands creates the spans.** Strands has built-in OpenTelemetry tracing:
   the supervisor, each specialist, every model call, and every tool call become
   spans with `gen_ai.*` attributes. The supervisor and specialists are the Lab 4
   agents-as-tools, so a request shows the supervisor span with each specialist
   span nested under the tool call that invoked it. The application adds a root
   span, `mortgage_assistant.invoke`, plus `session.id`, `user.id`, and a
   `request:<request_id>` tag.
2. **ADOT exports them.** The container starts with `opentelemetry-instrument`.
   The distro registers the OTel tracer provider and exports to the regional
   X-Ray OTLP endpoint. For AI agents on EKS this direct export is the whole
   pipeline: no CloudWatch add-on and no collector.
3. **Pod Identity signs the export.** The `mortgage-assistant` ServiceAccount
   already has a Pod Identity role with the X-Ray and CloudWatch Logs write
   permissions. There are no access keys in the pod.
4. **Transaction Search stores them.** The spans land in the log group
   `/workshop/mortgage-assistant/agent-traces`, which Omni reads.
5. **Content stays on the spans.** `AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT=true`
   keeps prompt and response text on the spans so Agent traces and evaluators
   can show it.

Because ADOT auto-instruments the libraries it finds, expect more spans than in
Lab 5a: an HTTP server span for `POST /invoke` above `mortgage_assistant.invoke`,
and spans for the AWS SDK calls the service makes (DynamoDB audit, session, and
memory writes, and Bedrock). The `trace_id` the API returns is the ID of the
trace that contains all of them.

### What the application contains

| Piece | Where |
| --- | --- |
| `aws-opentelemetry-distro` dependency | `pyproject.toml` |
| Start under `opentelemetry-instrument` | `Dockerfile` |
| ADOT and CloudWatch settings (`AGENT_OBSERVABILITY_ENABLED`, `OTEL_*`) | `k8s/service.template.yaml` |
| Hand the distro's tracer provider to Strands, add the root request span, expose `current_trace_id()` | `app/telemetry.py` |
| Client defaults for the `mortgage-assistant-omni` Service and its Secret | `app/invoke_eks.py` |

Everything else under `app/` is the Lab 5a application, unchanged.

The visible specialist tools are `mortgage_education_specialist`,
`existing_mortgage_specialist`, and `mortgage_application_specialist`. Their
stable persistence/audit IDs remain `general`, `existing`, and
`new_application`, respectively. The application specialist retains its
multi-turn field collection and approval-gated `create_loan_application`
side effect.

`OTEL_EXPORTER_OTLP_ENDPOINT` must stay unset. ADOT would use it instead of the
X-Ray endpoint. `app/telemetry.py` never creates a second tracer provider,
because OpenTelemetry keeps only the first one registered. When ADOT is not
running (unit tests, a local run), tracing is disabled, no network calls are
made, and `trace_id` is `null`.

## Prompt and response content

Strands records prompt and response text as span attributes such as
`gen_ai.input.messages`, `gen_ai.output.messages`, and `gen_ai.tool.call.result`.
In this lab they stay on the spans so Omni can show them, which is the point of
the lab for synthetic data. Lab 5a's `--telemetry-mask-content` option and its
redacting exporter are not part of Lab 5b, because the exporter is owned by
ADOT. For real customer data, remove `AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT` from
`k8s/service.template.yaml` so ADOT moves content off the spans, and review the
CloudWatch Omni data protection guidance first.

The Lab 4 audit trail is a separate store. `GET /executions/{request_id}` still
returns the full prompt, tool results, and model text, subject to the Lab 4
redaction and truncation rules, whatever you do with the spans.

## What Workshop Studio provisioned

The `CloudWatchOmni` stack creates Transaction Search, the trace log group, the
Omni domain `mortgage-<account-id>` and its `mortgage-assistant` space, and
Space Admin grants for the participant, ops, and Code Editor roles. You only
sign in.

## Deploy

The deployment uses the shared workshop foundation (EKS cluster, ECR, memory
table, Knowledge Base). It creates its own Secret and its own Deployment and
Service, `mortgage-assistant-omni`. The two services share the DynamoDB table.
The client keeps separate session state per lab directory, so Lab 5a and Lab 5b
use different sessions unless you pass the same `--session-id`. The application
requires `AWS_REGION` (or `AWS_DEFAULT_REGION`) and `KB_PARAMETER_NAME`;
`deploy-omni.sh` injects the selected Region and canonical Knowledge Base
parameter path into the pod.

From the repository root:

```bash
cd 05-observability/05b-cloudwatch-omni
uv run python -m unittest discover --start-directory tests --verbose
./scripts/deploy-omni.sh --region us-west-2
```

The API is exposed through a Network Load Balancer limited to your IP address
(override with `--service-access-cidr`). The deploy script:

1. Reads the Workshop Studio Parameter Store values, including the trace log
   group and the Omni domain URL.
2. Checks that Transaction Search is active, and that the memory table and
   vector index are active (using `scripts/check_memory_ready.py`).
3. Applies `k8s/base.yaml`, creates the `mortgage-assistant-omni-api-key`
   Secret, and creates the log stream that X-Ray delivers spans into.
4. Builds and pushes the `lab05b-<timestamp>` image, unless you pass
   `--image-uri`, then renders and applies `k8s/service.template.yaml`.
5. Waits for the rollout, the load balancer, and two consecutive successful
   readiness checks.
6. Sends a smoke-test request with a client `request_id`, requires a
   `trace_id` in the response (which proves the container is running under
   ADOT), requires `GET /executions/{request_id}` to report `COMPLETED` with a
   valid hash chain, and replays the same `request_id` to confirm the stored
   response comes back without a new attempt.
7. Prints the API endpoint and the **CloudWatch Omni details for this
   account**: the domain URL, a console link, and the space name.

| Flag | Effect |
| --- | --- |
| `--region REGION` / `--profile PROFILE` | AWS Region (default `us-west-2`) and optional CLI profile. |
| `--service-access-cidr CIDR` | CIDR allowed to reach the load balancer. Defaults to your detected IPv4 address (`/32`). |
| `--prompt TEXT` | Prompt for the smoke test. |
| `--session-ttl-seconds N` | Short-term session retention, at least 3600 (default 604800). |
| `--fault-injection-enabled` | Turn on fault injection at deploy time. |
| `--fault-injection-tool NAME` | Target tool (default `get_mortgage_details`). |
| `--fault-injection-mode MODE` | `delay`, `error`, `abort`, or `crash` (default `delay`). |
| `--fault-injection-delay-seconds N` | Delay used in `delay` mode (default 5). |
| `--approval-required-tools LIST` | Tools that pause for human approval (default `create_loan_application`; empty disables). |
| `--lease-seconds N` | Per-session lease, at least 30 (default 180). |
| `--enable-reasoning` | Capture Bedrock extended thinking in the audit trail. |
| `--no-snapshot-history` | Keep only the latest snapshot per agent. |
| `--image-uri URI` | Deploy an image already in ECR and skip the build and push. |

Run `./scripts/deploy-omni.sh --help` for the complete list. The script prints
the final image URI; to change settings later without a rebuild, rerun it with
`--image-uri` set to that value.

## Test the agent

```bash
./scripts/test-agent.sh --region us-west-2
```

The script sends four requests through the deployed API and prints the request
ID, status, and trace ID of each:

| Request | What it exercises |
| --- | --- |
| General question: "What are the benefits of a 15-year mortgage?" | Supervisor, then `mortgage_education_specialist` and its `retrieve_mortgage_knowledge` tool/model calls |
| Existing mortgage lookup: "What is the outstanding principal on account 555000111?" | Supervisor, then `existing_mortgage_specialist` and the `get_mortgage_details` tool |
| Conversation, turn 1: "I am considering a property worth 600,000 dollars." | A new session |
| Conversation, turn 2: "What property value did I mention in this conversation?" | The same session, with the earlier turn recalled from the session snapshot |

For your own prompts, use the same client as the earlier labs. It defaults to
the `mortgage-assistant-omni` Service and Secret:

```bash
uv run app/invoke_eks.py --region us-west-2 --prompt "What is the balance on customer ID 123456's mortgage?"
```

After the response, the client prints `Request ID:` and then `Trace ID:`.

## Join a trace to the audit trail

Every audit record, the execution record, and the `/invoke` response carry the
trace ID of the request, exactly as in Lab 5a. The identifiers match across the
two views:

| Identifier | In Omni | In the audit trail and API |
| --- | --- | --- |
| `request_id` | `request:<request_id>` tag on the root span | Every record; path of `GET /executions/{request_id}` |
| `session_id` | `session.id` span attribute | Every record |
| `actor_id` | `user.id` span attribute | Every record |
| `trace_id` | The trace ID in **Traces** | Every record, `execution.trace_id`, and the response |

- From a response to a trace: paste the `Trace ID:` line into the Omni trace
  search.
- From a request to its audit trail: `uv run app/invoke_eks.py --trail last`
  prints the route, the tool calls, and the hash-chain status. Add `--json` and
  read `trace_id` on the records to find the trace.
- From a trace to the audit trail: copy the `request_id` from the tag and the
  `session.id` attribute, then run
  `uv run app/invoke_eks.py --trail <request_id> --session-id <session.id>`.

A request that is resumed or approved has records from more than one attempt,
and each record carries the trace ID of the attempt that wrote it, so one
request can map to more than one trace. The audit trail is the durable record;
the trace is the diagnostic view.

## Try the Lab 4 features in Omni

Because this is the full Lab 5a application, the resilience features show up in
traces too:

- **Approval.** A prompt that creates a loan application pauses for approval.
  The API answers HTTP 202 with status `awaiting_approval`, and the client
  prints the `--approve` and `--deny` commands. Approving continues the request
  as a new attempt with its own trace.
- **Fault injection.** Redeploy with `--image-uri <image> --fault-injection-enabled
  --fault-injection-mode delay` (or `error`, `abort`) and ask the account
  question again. The injected fault is a `fault_injection` event on the tool
  span and a record in the audit trail. With `abort` the request fails with HTTP
  500 and returns no `trace_id`; use `--trail last` to find its trace, then
  `--resume last`.

## Navigate to CloudWatch Omni

The deploy script prints the Omni details. You can also read them any time:

```bash
aws ssm get-parameter --name /workshop/mortgage-assistant/cloudwatch/omni-domain-url \
  --region us-west-2 --query Parameter.Value --output text
```

1. **Sign in to the AWS console** with your workshop role, in `us-west-2`.
2. **Open Omni.** Use the **Console** link from the deploy output
   (`https://us-west-2.console.aws.amazon.com/cloudwatch/home?region=us-west-2#settings:/omni`),
   or the **Domain URL** (`https://mortgage-<account-id>.cloudwatch-omni.global.app.aws`).
   The domain has no identity provider, so it signs you in with your IAM role,
   which works most reliably when you start from the console.
3. **Choose the `mortgage-assistant` space.** You land on the Omni home page. Its
   "Complete your setup" cards are optional and not needed for this lab; use
   **Dismiss**.
4. **Find your traces.**
   - Open **Agent observability** on the home page (for example **Agent
     overview**) to see the agent, its latency, errors, and token usage.
   - Open **Traces** to list individual requests. Set the time range to the last
     hour and search for a trace ID printed by `test-agent.sh` or the client.
   - Open a trace to see the span waterfall: `mortgage_assistant.invoke`, the
     supervisor, each specialist nested under the supervisor's tool call, and
     every model and tool call, with inputs, outputs, and token counts.
   - The two "Conversation" requests share one `session.id`, so you can compare
     them.
5. **Allow time.** A trace appears about five minutes after the request. If the
   list is empty, wait, refresh, and widen the time range.

Menu names can differ slightly between console releases. If you do not see
**Agent observability**, search the home page for "traces".

## Troubleshooting

### The response has no `trace_id`

The container is not running under ADOT, or the distro failed to start. Check
the settings and the logs:

```bash
kubectl get deployment mortgage-assistant-omni \
  --namespace mortgage-assistant \
  --output jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}'

kubectl logs --namespace mortgage-assistant deployment/mortgage-assistant-omni --tail=200 |
grep -i -E 'opentelemetry|otel|telemetry'
```

`OTEL_EXPORTER_OTLP_ENDPOINT` must not appear in the list. The deploy script also
stops with an error if the smoke response has no `trace_id`.

### The response has a `trace_id` but the trace does not appear in Omni

- Wait at least five minutes and widen the time range.
- Confirm Transaction Search is active:
  `aws xray get-trace-segment-destination --region us-west-2` should report
  `CloudWatchLogs` and `ACTIVE`.
- Confirm spans are arriving in the log group
  `/workshop/mortgage-assistant/agent-traces`, in the stream
  `mortgage-assistant-omni`.
- Look for export errors (for example `AccessDenied` on the X-Ray endpoint) in
  the pod logs; they point at the Pod Identity role.

### HTTP 401, 409, or 429

For 401, the client reads the key from the `mortgage-assistant-omni-api-key`
Secret; with `curl`, read it from that Secret rather than the Lab 5a one. 409
and 429 come from the Lab 4 resilience controls (a busy or approval-pending
session, or all agent slots in use), not from tracing. See Lab 4.

## Clean up

```bash
./scripts/cleanup-omni.sh --region us-west-2
```

This removes only the `mortgage-assistant-omni` Deployment, Service, and Secret.
Lab 5a, the namespace, and the shared DynamoDB table are kept. Session, audit,
execution, and ledger items written by this service stay in the table, and the
traces stay in CloudWatch until their retention expires.
