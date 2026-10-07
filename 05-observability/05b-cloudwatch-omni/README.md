# Lab 5b: Observe the Strands mortgage assistant with Amazon CloudWatch Omni

This lab sends the traces of the multi-agent mortgage assistant running on
Amazon EKS to Amazon CloudWatch Omni, so you can see what each agent, model
call, and tool call did, how long it took, and how many tokens it used.

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
   spans with `gen_ai.*` attributes. The application adds a root span,
   `mortgage_assistant.invoke`, plus `session.id` and `user.id`.
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

### What the application contains

| Piece | Where |
| --- | --- |
| `aws-opentelemetry-distro` dependency | `pyproject.toml` |
| Start under `opentelemetry-instrument` | `Dockerfile` |
| ADOT and CloudWatch settings (`AGENT_OBSERVABILITY_ENABLED`, `OTEL_*`) | `k8s/service.template.yaml` |
| Hand the distro's tracer provider to Strands and add the root request span | `app/telemetry.py` |

`OTEL_EXPORTER_OTLP_ENDPOINT` must stay unset. ADOT would use it instead of the
X-Ray endpoint. The module never creates a second tracer provider, because
OpenTelemetry keeps only the first one registered.

## What Workshop Studio provisioned

The `CloudWatchOmni` stack creates Transaction Search, the trace log group, the
Omni domain `mortgage-<account-id>` and its `mortgage-assistant` space, and
Space Admin grants for the participant, ops, and Code Editor roles. You only
sign in.

## Deploy

The deployment uses the shared workshop foundation (EKS cluster, ECR, memory
table, Knowledge Base). It creates its own Secret and its own Deployment and
Service, `mortgage-assistant-omni`. It does not need Lab 5a or Langfuse.

```bash
cd /workshop/05-observability/05b-cloudwatch-omni
uv run python -m unittest discover --start-directory tests --verbose
./scripts/deploy-omni.sh --region us-west-2
```

The API is exposed through a Network Load Balancer limited to your IP address
(override with `--service-access-cidr`). When the script finishes, it prints the
API endpoint and the **CloudWatch Omni details for this account**: the domain
URL, a console link, and the space name.

## Test the agent

```bash
./scripts/test-agent.sh --region us-west-2
```

The script sends four requests through the deployed API and prints the trace ID
of each:

| Request | What it exercises |
| --- | --- |
| General question: "What are the benefits of a 15-year mortgage?" | Supervisor, then the Knowledge Base specialist and its model calls |
| Existing mortgage lookup: "What is the outstanding principal on account 555000111?" | Supervisor, then the account specialist and the `get_mortgage_details` tool |
| Conversation, turn 1: "I am considering a property worth 600,000 dollars." | A new session |
| Conversation, turn 2: "What property value did I mention in this conversation?" | The same session, with the earlier turn recalled from memory |

For your own prompts, use the same client as the earlier labs:

```bash
uv run app/invoke_eks.py --region us-west-2 --prompt "What are the benefits of a 15-year mortgage?"
```

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
     hour and search for a trace ID printed by `test-agent.sh`.
   - Open a trace to see the span waterfall: `mortgage_assistant.invoke`, the
     supervisor, each specialist, and every model and tool call, with inputs,
     outputs, and token counts.
   - The two "Conversation" requests share one `session.id`, so you can compare
     them.
5. **Allow time.** A trace appears about five minutes after the request. If the
   list is empty, wait, refresh, and widen the time range.

Menu names can differ slightly between console releases. If you do not see
**Agent observability**, search the home page for "traces".

## Clean up

```bash
./scripts/cleanup-omni.sh --region us-west-2
```

This removes only the `mortgage-assistant-omni` Deployment, Service, and Secret.
