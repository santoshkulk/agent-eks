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

## Run

The deployment uses the shared workshop foundation (EKS cluster, ECR, memory
table, Knowledge Base). It creates its own Secret and its own Deployment and
Service, `mortgage-assistant-omni`. It does not need Lab 5a or Langfuse.

```bash
cd /workshop/05-observability/05b-cloudwatch-omni
uv run python -m unittest discover --start-directory tests --verbose
./scripts/deploy-omni.sh --region us-west-2
```

The Service is ClusterIP only. To send more requests, open a tunnel:

```bash
kubectl port-forward --namespace mortgage-assistant service/mortgage-assistant-omni 18080:80 &
uv run app/invoke_eks.py --region us-west-2 --url http://127.0.0.1:18080 \
  --prompt "What are the benefits of a 15-year mortgage?"
```

Then open the Omni URL from `/workshop/mortgage-assistant/cloudwatch/omni-domain-url`,
choose the `mortgage-assistant` space, and open **Agent traces**. A trace
appears within about five minutes.

## Clean up

```bash
./scripts/cleanup-omni.sh --region us-west-2
```

This removes only the `mortgage-assistant-omni` Deployment, Service, and Secret.
