# Lab 5CC: Pilot per-Agent token and estimated-cost attribution

This isolated pilot copies the Lab 5A Langfuse checkpoint and adds only
filterable application, release, environment, and Agent metadata. It can run
independently when the `mortgage-assistant` namespace is absent, or overlay a
complete Lab 5A or prior 5CC workload by changing only its image. Both modes
reuse the Workshop Studio EKS cluster, ECR repository, DynamoDB memory,
Knowledge Base, Pod Identity association, and self-hosted Langfuse project. The
pilot does not modify the 5A or 5B source directories.

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

The script selects one of two modes automatically:

- **Fresh mode:** When the `mortgage-assistant` namespace is absent, it creates
  the namespace, service account, API-key and Langfuse Kubernetes Secrets,
  two-replica Deployment, PDB, and source-restricted Network Load Balancer. The
  caller's detected public IPv4 `/32` is the default source range; use
  `--service-access-cidr` to provide another restricted CIDR. It streams
  Langfuse bootstrap credentials from the existing in-cluster Langfuse
  deployment directly into the Kubernetes Secret without putting them in
  command arguments or rendered files.
- **Overlay mode:** When a complete canonical Lab 5A or prior 5CC workload
  exists, it preserves the Service, source ranges, Secrets, environment, PDB,
  and service account, and changes only the application image.

Both modes wait for rollout and readiness, then send one authenticated smoke
request. A failed fresh deployment removes only the namespace that invocation
created, after verifying its ownership label. A failed overlay restores the
captured previous image. If the namespace exists but is incomplete or belongs
to another checkpoint, the script fails before mutation.

The smoke client reads the API key in-process without printing or persisting it.
The summary includes the deployment mode, Langfuse URL, and smoke trace ID.

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

Langfuse includes a curated Cost Dashboard with standard totals and breakdowns
by model, environment, user, trace name, and observation name. Open
**Dashboards** and use that managed dashboard as a starting point; editing it
creates a project-owned copy.

Create a dashboard named `Mortgage Assistant Token and Cost Report`. Create
widgets from the Observations data source and filter them to:

- observation type: `generation`
- application metadata: `mortgage-assistant`
- environment: `workshop`
- release: `mortgage-assistant-release-1.0`
- session ID: the printed `REPORT_SESSION_ID`

Langfuse can filter arbitrary metadata, but its dashboard builder cannot
currently use `metadata.agent_name` as a breakdown dimension. Create one Agent
widget, then duplicate it three times and give each copy one of these filters:

```text
metadata.agent_name = mortgage_supervisor
metadata.agent_name = mortgage_education_specialist
metadata.agent_name = existing_mortgage_specialist
metadata.agent_name = mortgage_application_specialist
```

Each Agent widget should show generation count, summed input tokens, summed
output tokens, summed total tokens, and average latency. If Langfuse recognizes
the model price, also include summed estimated cost. If pricing is unavailable,
omit cost measures and label the dashboard `Pricing unavailable for
us.anthropic.claude-sonnet-4-6`; never substitute `$0.00`.

Add separate total input-token and output-token widgets using the same filters
without an Agent-name filter. Add a total-cost widget only when pricing was
confirmed.

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

In overlay mode, Kubernetes rollout history is not a deterministic restore point
after repeated pilot deployments. To restore the Lab 5A application explicitly,
redeploy that checkpoint:

```bash
cd /workshop/05-observability/05a-langfuse
./scripts/deploy-observability.sh --region us-west-2
```

Alternatively, continue to Lab 6. Langfuse retains the 5CC traces and dashboard
after the application image changes.
