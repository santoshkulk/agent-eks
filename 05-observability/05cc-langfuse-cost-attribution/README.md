# Lab 5CC: Pilot per-Agent token and estimated-cost attribution

This isolated pilot copies the Lab 5A Langfuse checkpoint and adds only
filterable application, release, environment, and Agent metadata. It reuses the
existing `mortgage-assistant` Service, Network Load Balancer, API-key Secret,
DynamoDB memory, Pod Identity association, and self-hosted Langfuse project.
Deploying it replaces the current `mortgage-assistant` pods with the 5CC image;
it does not modify the 5A or 5B source directories.

Use only workshop data. Langfuse cost is an estimate for model generations, not
the complete application cost or AWS invoice.

## Reporting dimensions

Every request retains the existing `user.id`, `session.id`, request tag, and
trace ID. It also carries these fixed dimensions:

| Dimension | Value |
| --- | --- |
| Application | `mortgage-assistant` |
| Environment | `workshop` |
| Release | `mortgage-assistant-release-1.0` |

Each Agent receives one filterable
`langfuse.observation.metadata.agent_name` value:

- `mortgage_supervisor`
- `mortgage_education_specialist`
- `existing_mortgage_specialist`
- `mortgage_application_specialist`

Strands copies that Agent-specific mapping onto its nested model spans. Report
model usage from observations of type `generation` only. Agent spans contain
cumulative usage and must not be added to their child generations.

## Validate locally

```bash
uv run python -m unittest discover --start-directory tests --verbose
uv lock --check
```

The tests are offline and do not invoke AWS, Bedrock, EKS, or Langfuse.

## Deploy the pilot

Deployment changes charge-incurring participant resources. Confirm the account,
Region, and current Kubernetes context before running it.

```bash
cd /workshop/05-observability/05cc-langfuse-cost-attribution
./scripts/deploy-cost-attribution.sh --region us-west-2
```

The script requires the existing two-replica Lab 5A deployment and its Service,
source ranges, PDB, service account, and Secrets. It builds and pushes a
`lab05cc-agent-<timestamp>` image, verifies the Lab 5A-compatible image lineage,
service account, PDB, replica count, source ranges, and Secret references, then
captures the current application image and changes only the
`mortgage-assistant` container image. It waits for readiness and sends one
authenticated smoke request through the existing client. If rollout, readiness,
or smoke validation fails, it restores the captured image. It never replaces
either Secret and never reapplies the Service or its source ranges. The
deployment shell does not capture, print, or pass credential values in process
arguments; the smoke client reads the API key in-process without printing or
persisting it, while the Langfuse Secret value remains unread. The summary
includes the Langfuse URL and smoke trace ID.

The workshop NLB is source-restricted but uses plaintext HTTP. Bearer
authentication does not encrypt transport. Use only synthetic workshop data;
a production deployment must use TLS or private transport.

## Generate one reporting session

Use one actor and one new session for three clear, single-intent requests:

```bash
export REPORT_ACTOR_ID=usage-report-example
export REPORT_SESSION_ID="usage-report-$(date -u +%Y%m%d%H%M%S)"

echo "Actor ID: $REPORT_ACTOR_ID"
echo "Session ID: $REPORT_SESSION_ID"
```

Existing mortgage:

```bash
uv run app/invoke_eks.py \
  --region us-west-2 \
  --actor-id "$REPORT_ACTOR_ID" \
  --session-id "$REPORT_SESSION_ID" \
  --prompt "For customer CUST-10001, report the outstanding mortgage balance and next payment due date."
```

Mortgage education:

```bash
uv run app/invoke_eks.py \
  --region us-west-2 \
  --actor-id "$REPORT_ACTOR_ID" \
  --session-id "$REPORT_SESSION_ID" \
  --prompt "Compare 15-year and 30-year fixed-rate mortgages."
```

Mortgage application:

```bash
uv run app/invoke_eks.py \
  --region us-west-2 \
  --actor-id "$REPORT_ACTOR_ID" \
  --session-id "$REPORT_SESSION_ID" \
  --prompt "For customer CUST-10001, report the mortgage application status and missing documents."
```

Routing is model-directed. Verify the observed Agent spans rather than requiring
an exact span or generation count.

## Inspect one request

Open one printed trace ID in Langfuse. For each `generation` observation, record:

- `metadata.agent_name`
- model
- input tokens
- output tokens
- total tokens
- latency
- estimated cost, when a model-price definition matches

Supervisor routing and synthesis consume model tokens. Tool execution is a
separate span with no model tokens, but a tool result can increase the input
size of a later generation.

## Build the dashboard

Create a Langfuse dashboard named `Mortgage Assistant Token and Cost Report`.
Create widgets from the Observations data source and filter to model-generation
observations. Apply:

- application metadata: `mortgage-assistant`
- environment metadata: `workshop`
- release: `mortgage-assistant-release-1.0`
- session ID: the printed `REPORT_SESSION_ID`

Group by `metadata.agent_name`. The minimum dashboard always contains:

1. A table with Agent name, generation count, summed input tokens, summed output
   tokens, summed total tokens, and average latency.
2. Total input tokens.
3. Total output tokens.

If Langfuse recognizes the model price, add summed estimated cost to the Agent
table plus total-cost and cost-by-Agent widgets. If pricing is unavailable,
omit those cost measures and widgets and label the pilot result `Pricing
unavailable for us.anthropic.claude-sonnet-4-6`; never substitute `$0.00`.

Use downloaded CSV values or generation details at their available unrounded
precision when reconciling the report. Token totals must exactly equal the sum
of the included generation rows. Cost totals, when available, must match the
unrounded row sum. If only rounded UI values are available, let `n` be the
number of rounded generation rows and `r` the smallest displayed currency unit;
the allowed difference is at most `n × r / 2`, plus `r / 2` when the displayed
aggregate is also rounded. Do not include Agent, tool, memory, retrieval, or API
request spans in the token and cost sum.

For an application time-period view, duplicate or reuse the dashboard, remove
the session filter, and select a UTC calendar-month range. Keep the application,
environment, release, and generation filters. Add cost trends only when pricing
is available. Add daily token trends or top sessions only if the minimum
dashboard remains quick to complete.

## Pricing and scope

Verify that Langfuse recognizes `us.anthropic.claude-sonnet-4-6` before making
cost part of the result. If pricing is unavailable, token reporting remains
valid but cost must be shown as unavailable, never as `$0.00`.

The report excludes EKS, load balancing, DynamoDB, memory embeddings, Knowledge
Base retrieval and storage, OpenSearch Serverless, Langfuse infrastructure, and
MCP provider costs. It reports only model-generation usage observed by
Langfuse.

## Restore the previous checkpoint

The pilot updates the same application Deployment. Kubernetes rollout history
is not a deterministic restore point after repeated pilot deployments. To
restore the Lab 5A application explicitly, redeploy that checkpoint:

```bash
cd /workshop/05-observability/05a-langfuse
./scripts/deploy-observability.sh --region us-west-2
```

Alternatively, continue to Lab 6. Langfuse retains the 5CC traces and dashboard
after the application image changes.
