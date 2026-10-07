# Lab 5b: Send the same traces to CloudWatch Omni

Lab 5b is an optional second observability track. It runs a copy of the Lab 5a
mortgage assistant as a separate Deployment, `mortgage-assistant-omni`, and
exports its traces to Amazon CloudWatch Omni. It can also keep exporting to
Langfuse, so you can compare both views of the same request.

Lab 5a is not modified. Its Deployment, Network Load Balancer, and secrets keep
running, and Lab 6 continues to build on Lab 5a.

## How it differs from Lab 5a

| | Lab 5a (Langfuse) | Lab 5b (CloudWatch Omni) |
| --- | --- | --- |
| Tracer provider | Created by `telemetry.py` | Started by the AWS Distro for OpenTelemetry (ADOT) |
| Launch command | `uvicorn` | `opentelemetry-instrument uvicorn` |
| Export path | OTLP/HTTP to the in-cluster Langfuse Service | OTLP/HTTP to the regional X-Ray endpoint, signed with the pod's Pod Identity role |
| Langfuse settings | `OTEL_EXPORTER_OTLP_*` | `LANGFUSE_OTLP_ENDPOINT` and `LANGFUSE_OTLP_HEADERS` |
| Exposure | Internet-facing NLB | ClusterIP Service; use `kubectl port-forward` |

`OTEL_EXPORTER_OTLP_ENDPOINT` is deliberately unset: ADOT would use it instead
of the X-Ray endpoint. `telemetry.py` attaches Strands and an optional Langfuse
span processor to the provider ADOT started and never creates a second one.

## Prerequisites

- Lab 5a is deployed (Lab 5b reuses its API-key and Langfuse secrets).
- Workshop Studio deployed the `CloudWatchOmni` stack: Transaction Search, the
  trace log group, and the Omni domain and space.

## Run

```bash
cd /workshop/05-observability/05b-cloudwatch-omni
uv run python -m unittest discover --start-directory tests --verbose
./scripts/deploy-omni.sh --region us-west-2
```

Use `--telemetry-backend cloudwatch` to export to CloudWatch Omni only.

## Clean up

```bash
./scripts/cleanup-omni.sh --region us-west-2
```

This removes only `mortgage-assistant-omni`.
