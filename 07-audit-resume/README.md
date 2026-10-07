# Lab 7: Audit trail, explainability, and resumable agents-as-tools

This lab is a complete checkpoint of Lab 6 that makes the multi-agent mortgage assistant auditable, explainable, and recoverable. It does not import code from earlier labs.

| Goal | What Lab 7 adds |
| --- | --- |
| Audit trail and visibility | A durable, hash-chained record of every invocation, model decision, tool call, memory read/write, and approval, for the supervisor **and** each specialist. |
| Explainability | Routing rationale, structured specialist reports (answer, rationale, evidence, assumptions), memory-retrieval provenance, and an `explanation` block on every API response. |
| Resilience | Message-level snapshots for every agent, a per-session lease, request-level idempotency, a tool ledger for exactly-once side effects, rollback of unfinished turns, and human approval pauses that survive pod restarts. |

## Design: the agent-as-tool pattern

```text
POST /invoke ──► Orchestrator (lease, audit trail, status)
                     │
                     ▼
              supervisor Agent  ── hooks: Resume · Approval · Audit · FailFast
                │  tools: calculator, get_credit_score (MCP), add/search_memory
                │
                ├─ general_mortgage_specialist   ┐
                ├─ existing_mortgage_specialist  ├─ Agent.as_tool(preserve_context=True)
                └─ new_application_specialist    ┘   own agent_id, own snapshot, own hooks,
                                                      structured SpecialistReport
```

- Each specialist is a real `Agent` exposed with `Agent.as_tool(preserve_context=True)`. Lab 6 rebuilt a throwaway agent inside a `@tool` function and kept only `str(result)`, so its tool calls and history were lost.
- Every agent has its own `SnapshotSessionManager` under the same `session_id` and a distinct `agent_id` (`supervisor`, `general`, `existing`, `new_application`). The new-application specialist now remembers the fields it has already collected.
- Swarm and Graph were not used: they cannot persist member agents, and `SnapshotSessionManager` rejects multi-agent orchestrators.

All state lives in the existing DynamoDB table, so **no infrastructure or IAM change is needed** for the application. The pod role already allows `GetItem`, `PutItem`, and `Query` on it.

| Sort-key prefix (under `pk=user/<actor_id>`) | Contents |
| --- | --- |
| `session/<session>/scopes/agent/<agent_id>/snapshots/...` | Per-agent snapshots (latest + immutable history). 7-day TTL. |
| `audit/<session>/<request>/<seq>` | Append-only audit records with `prev_hash`/`hash`. No TTL. |
| `exec/<session>/<request>` | Execution record: status, attempt, prompt hash, interrupts, response. |
| `lock/<session>` | Per-session lease (`LEASE_SECONDS`, default 180). |
| `ledger/<request>/<tool>/<input-hash>` | Recorded results of side-effecting tools. |

## Audit trail

`app/audit.py` attaches an `AuditHook` to the supervisor and each specialist. It records, tagged with `agent_id`, `request_id`, `attempt`, and `trace_id`:

- `invocation_start` / `invocation_end` (stop reason, token usage, pending interrupts)
- `decision` (the model's text and the tool calls it chose) and `model_response` / `model_error`
- `tool_start` / `tool_call` (inputs, results, status, duration; specialists appear as tools of the supervisor, and their own tool calls appear under their own `agent_id`)
- `memory_read` (keys and scores retrieved) and `memory_write` (with provenance)
- `approval`, `rollback`, `ledger_replay`, `ledger_pending`, `lease_lost`, `fault_injection`, and the `execution_*` lifecycle

Records are hash-chained. `GET /executions/{request_id}` returns `chain_valid`, which is false if a record is altered, removed, or reordered. Sensitive keys (`AUDIT_REDACT_KEYS`) are masked and long values truncated; this is independent of `TELEMETRY_MASK_CONTENT`.

If an audit record for a side-effecting tool cannot be written, the tool call is **blocked** (fail closed). Other records degrade to a logged, dropped record.

Hash chaining makes tampering evident, not impossible. For production, also stream the table to S3 with Object Lock and enable CloudTrail data events; this lab does not provision either.

## Explainability

- The supervisor writes a `Routing rationale:` sentence before delegating. The audit trail links it to the specialist call it caused.
- Specialists return a structured `SpecialistReport` (`answer`, `rationale`, `evidence`, `assumptions`). The supervisor answers from it, and the evidence survives in the audit trail.
- Every `/invoke` response carries `explanation`: `route`, `tools_used`, `evidence`, `memories_used`, `approvals`, `records`, and `attempts`.
- `ENABLE_REASONING=true` (deploy flag `--enable-reasoning`) also captures Bedrock extended-thinking blocks for the supervisor. It is off by default. It was checked against Bedrock with the default model over multiple tool-using turns; the extended-thinking budget is fixed at 2048 tokens, and models that do not support thinking will reject the request.

## Resilience and resume

1. **Message-level snapshots.** Snapshots are saved after every message, plus an immutable snapshot per invocation (`SNAPSHOT_HISTORY`).
2. **Unfinished-turn rollback.** `ResumeHook` marks a turn in `agent.state` when it starts and clears it when the invocation ends. If a pod dies or the invocation raises, the next invocation rolls the agent back to the start of that turn, so the message list stays valid. The same hook reapplies the current system prompt, because a restored snapshot would otherwise keep the prompt from the day the session started.
3. **Idempotent requests.** `request_id` is the idempotency key. Re-sending it replays a completed request, resumes a failed one (`attempt` increments), and returns `409` while it is still running, the session is busy, or another request in the session is waiting for approval.
4. **Tool ledger.** Side-effecting tools (`create_customer_id`, `create_loan_application`, memory writes) run at most once per request and input; a retry gets the recorded result.
5. **Fail fast.** Strands turns a specialist's exception into an error tool result, which a model can paper over with an apology. `FailFastHook` (supervisor only) fails the request instead, so it is resumable.
6. **Human approval.** `create_loan_application` (configurable with `APPROVAL_REQUIRED_TOOLS`) pauses with a Strands interrupt. `/invoke` returns `202` with the pending approvals. The interrupt is stored in the agent snapshots, so any pod can finish the request after the reviewer answers.

### SDK limits worth knowing

- `Agent.as_tool` resumes a paused specialist from the same `Interrupt` object the parent registered. After a restart the parent and specialist each restore their own copy, so the reviewer's answer would not reach the specialist. `share_interrupts()` in `app/resilience.py` re-links them. It reads the SDK-private `_interrupt_state`, and `tests/test_agent_flows.py` guards it.
- The session lease is renewed by a heartbeat (every third of `LEASE_SECONDS`) while a request runs, so a long request is not taken over. If a renewal fails, the lease was lost to another worker; the trail records `lease_lost`. After a crash the lease simply expires, so a retry waits up to `LEASE_SECONDS`.
- A crash between a side effect and its ledger write can repeat the effect once (the audit trail marks it `ledger_pending` / `possibly_executed`). Real downstream systems should also take an idempotency key.
- `actor_id`, `session_id`, and the approval `reviewer` are caller-supplied and the API uses one shared bearer key, as in earlier labs. Audit records are attributable to those claims, not to verified identities.

## API

| Route | Purpose |
| --- | --- |
| `POST /invoke` | `prompt`, `actor_id`, `session_id`, optional `request_id`. `200` completed, `202` awaiting approval, `409` conflict, `500` failed (retry with the same `request_id`). |
| `POST /executions/{request_id}/resume` | Retry a failed request using its stored prompt. |
| `POST /executions/{request_id}/approvals` | `decisions: [{interrupt_id, approved, comment, reviewer}]` for all pending approvals. |
| `GET /executions/{request_id}?actor_id=&session_id=` | Ordered audit records, `chain_valid`, and the explanation. |
| `GET /sessions/{session_id}/executions?actor_id=` | Execution summaries for a session. |
| `GET /health`, `GET /health/ready` | Probes, unchanged from Lab 6. |

Configuration added to the Deployment: `APPROVAL_REQUIRED_TOOLS`, `LEASE_SECONDS`, `ENABLE_REASONING`, `SNAPSHOT_HISTORY`. The fault-injection `abort` mode (fails the request after the faulty tool returns) and `crash` mode (kills the process mid-turn) are new.

## Prerequisites

- Labs 0 and 5 completed in a Workshop Studio environment (Lab 6 is also required: Lab 7 keeps the credit-score MCP tool). **Do not run the Lab 6 cleanup script before Lab 7:** it deletes the `mortgage-assistant` namespace, including the `langfuse-otel-auth` Secret that Lab 7 reads.
- On your machine: `aws` (v2), `kubectl`, `uv`, `curl`, `python3`, `openssl`, and a `docker` CLI that can build and push (Docker, or Finch with a `docker` alias). The deploy script falls back to plain `docker build` when `buildx` is missing.
- AWS credentials for the workshop account. `app/inspect_audit.py` also needs `ssm:GetParameter`, `sts:GetCallerIdentity`, and DynamoDB `GetItem`/`Query` on the memory table; everything else in this lab goes through the API.

All commands below run from the `07-audit-resume` directory:

```bash
cd 07-audit-resume    # from the repository root
```

### Deploy flags added in Lab 7

| Flag | Effect |
| --- | --- |
| `--approval-required-tools LIST` | Tools that pause for human approval (default `create_loan_application`; empty disables). |
| `--lease-seconds N` | Per-session lease, at least 30 (default 180). Renewed by a heartbeat while a request runs; a crashed request can be retried after it expires. |
| `--enable-reasoning` | Capture Bedrock extended-thinking in the audit trail. |
| `--no-snapshot-history` | Keep only the latest snapshot per agent. |
| `--image-uri URI` | Reuse an image already in ECR and skip the build and push (about 2 minutes instead of about 12). |
| `--fault-injection-mode abort\|crash` | Fail the request after the faulty tool returns, or kill the process mid-turn. |

All Lab 6 flags (`--service-access-cidr`, `--telemetry-mask-content`, `--fault-injection-*`) still apply. Fault injection is wired into `get_mortgage_details` only; other tool names do nothing.

## Run the tests

```bash
uv sync --frozen
uv run python -m unittest discover tests
```

The first `uv sync` downloads the Strands and boto3 dependencies (a minute or two).

`tests/test_agent_flows.py` runs the real supervisor, `as_tool` specialists, hooks, snapshots, and orchestrator with a scripted model, covering audit and explanation, replay, rollback and resume, approval across a restart, and denial.

## Deploy

Complete Lab 5 and use the same Workshop Studio environment as Lab 6, then:

```bash
./scripts/deploy-audit-resume.sh --region us-west-2
```

The script reuses the Lab 5/6 API-key and Langfuse Secrets, builds `lab07-agent-*`, applies the manifest, and smoke-tests an invocation. The smoke test also fetches the audit trail, requires `chain_valid`, and replays the `request_id`.

## Exercises

Request IDs identify a request. `--prompt` creates one for you and remembers it in `.workshop/client-state.json` (per actor), and `last` means that most recent `--prompt`. The session ID is printed as `Session:` on every call.

### 1. Audit and explain a request

```bash
uv run app/invoke_eks.py --prompt "What is the balance on customer ID 123456's mortgage?"
uv run app/invoke_eks.py --trail last
```

Expect `Request ... COMPLETED (attempt 1), hash chain valid`, a `routed to existing_mortgage_specialist: Routing rationale: ...` line, tool calls under both `supervisor` and `existing`, and an ordered record list. Add `--json` for the full `explanation` block and `chain_valid`.

### 2. Resume a failed request

Switch the fault on without rebuilding (`abort` fails the whole request after `get_mortgage_details` returns):

```bash
kubectl set env deployment/mortgage-assistant -n mortgage-assistant \
  FAULT_INJECTION_ENABLED=true FAULT_INJECTION_TOOL=get_mortgage_details FAULT_INJECTION_MODE=abort
kubectl rollout status deployment/mortgage-assistant -n mortgage-assistant

RID=demo-$(date +%s)
# Use a customer ID you have not asked about yet: the specialist remembers earlier answers
# and would skip the tool (and the fault) for a customer it has already looked up.
uv run app/invoke_eks.py --request-id $RID --prompt "What is the balance on customer ID 654321's mortgage?"   # HTTP 500

kubectl set env deployment/mortgage-assistant -n mortgage-assistant FAULT_INJECTION_ENABLED=false
kubectl rollout status deployment/mortgage-assistant -n mortgage-assistant

uv run app/invoke_eks.py --resume $RID     # completes as attempt 2
uv run app/invoke_eks.py --trail $RID
```

The trail shows attempt 1 with a `fault_injection` record and `execution_failed`, then attempt 2 with a `rollback` record and `execution_completed`, all in one valid hash chain. The failed attempt's tool results remain in the specialist's own history, so the final answer may mention the earlier failure. (`kubectl set env` is the fast path; `./scripts/deploy-audit-resume.sh --fault-injection-enabled --fault-injection-mode abort --image-uri <image>` does the same through the script.)

### 3. Approve a side effect, across a restart

```bash
uv run app/invoke_eks.py --prompt "Start a new application: customer 123456, name Sam, age 30, annual income 90000, annual expenses 40000. Create the application now."
```

A good response says `Awaiting approval for: create_loan_application` (HTTP 202). If the model asks a follow-up question instead, answer it with another `--prompt` until you see the approval request; `--approve` reports "has no pending approvals" otherwise.

```bash
kubectl rollout restart deployment/mortgage-assistant -n mortgage-assistant
kubectl rollout status deployment/mortgage-assistant -n mortgage-assistant
uv run app/invoke_eks.py --approve last --reviewer pat
uv run app/invoke_eks.py --trail last      # shows the approval by pat and one create_loan_application call
```

Until the approval is answered, any other request in the same session returns `409`.

### 4. Optional: crash mid-turn

```bash
kubectl set env deployment/mortgage-assistant -n mortgage-assistant \
  FAULT_INJECTION_ENABLED=true FAULT_INJECTION_TOOL=get_mortgage_details FAULT_INJECTION_MODE=crash \
  LEASE_SECONDS=30
kubectl rollout status deployment/mortgage-assistant -n mortgage-assistant
RID=crash-$(date +%s)
uv run app/invoke_eks.py --request-id $RID --prompt "What is the balance on customer ID 777001's mortgage?"
```

The client reports that the connection was closed and the pod restarts (`kubectl get pods -n mortgage-assistant` shows `RESTARTS 1`). Turn the fault off (`FAULT_INJECTION_ENABLED=false`), wait for the rollout, then `--resume $RID`. Resuming before the 30 second lease expires returns `409`. (A running request keeps renewing its lease, so only a crashed one expires.)

Inspect the table directly with `uv run app/inspect_audit.py --session-id <Session value> [--request-id <id> --records]`.

## Cleanup

```bash
./scripts/cleanup-audit-resume.sh
```

This removes the `mortgage-assistant` application only. It does not touch `credit-services`, Langfuse, or other shared resources. Audit, execution, and ledger items stay in the table; delete the actor's items with the AWS console or CLI if you need to.
