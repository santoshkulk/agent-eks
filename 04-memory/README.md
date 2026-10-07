# Lab 04: Durable memory, agents-as-tools, and an auditable, resumable mortgage assistant

In this lab, you add short-term and long-term memory to the mortgage assistant
running on Amazon EKS. You then rebuild its multi-agent design with Strands
agents-as-tools so that every agent keeps its own durable session, every
decision and tool call lands in a tamper-evident audit trail, and a request that
fails, is interrupted, or waits for a human can be resumed.

The memory design follows
[Introducing Strands DynamoDB Storage: Durable Agent Storage for the Strands Agents SDK](https://aws.amazon.com/blogs/database/introducing-strands-dynamodb-storage-durable-agent-storage-for-the-strands-agents-sdk/).
It does not use Amazon Bedrock AgentCore. This lab is a self-contained
checkpoint: it does not import code from other lab directories.

## Learning objectives

After completing this lab, you will be able to:

| Area | You will be able to |
| --- | --- |
| Memory | Explain short-term session state versus long-term memory, store and semantically retrieve durable preferences, scope them by actor and session, and prove that one actor cannot recall another's memories. |
| Orchestration | Describe the agent-as-tool pattern: a supervisor delegating to specialists that are real `Agent` objects, each with its own persistent session. |
| Persistence | Resume a conversation after an EKS pod is replaced, and inspect the DynamoDB items the agents use. |
| Audit | Read a hash-chained record of every invocation, model decision, tool call, memory read and write, and approval. |
| Explainability | See why a request was routed to a specialist and which evidence, memories, and approvals applied. |
| Resilience | Retry a failed request with `request_id`, roll back an unfinished turn, run a side effect at most once, approve a side effect across a pod restart, and cancel a stuck approval. |

## Estimated time

Allow about 70 minutes (about 60 without the optional Step 17), including
deployment, the memory exercises, and the audit and resume exercises. Roughly 30
minutes of that is reading, 25 is typing commands, and 12 is waiting for
deployments and rollouts. A first image build with a cold cache adds about 10
minutes.

## What changed from the previous Lab 04

| Topic | Before | Now |
| --- | --- | --- |
| Specialists | Throwaway agents built inside `@tool` functions; only `str(result)` was kept, so their tool calls and history were lost. | Real `Agent`s exposed with `Agent.as_tool(preserve_context=True)`, each with its own `agent_id` and persistent session. |
| Sessions | One snapshot for the supervisor, saved after each invocation. | One `SnapshotSessionManager` per agent (`supervisor`, `general`, `existing`, `new_application`) under the same `session_id`, saved after every message, plus an immutable snapshot per invocation (`SNAPSHOT_HISTORY`). |
| Memory writes | Random key per write. | Deterministic key per request and content, with provenance metadata (`request_id`, `session_id`, `actor_id`, `source_agent`, `created_at`), so a retried request overwrites instead of duplicating. |
| Hooks | None. | Supervisor: `ResumeHook`, `ApprovalHook`, `AuditHook`, `FailFastHook`. Specialists: the first three. |
| Requests | Fire and forget. | `request_id` idempotency key, execution record, per-session lease with heartbeat, tool ledger, rollback of unfinished turns, human approval, cancel. |
| API | `POST /invoke` returning `response`. | Adds `status`, `interrupts`, `attempt`, `explanation`, and routes to resume, approve, cancel, and read the audit trail. |
| Client | `--prompt` and session options. | Adds `--request-id`, `--trail`, `--resume`, `--approve`, `--deny`, `--cancel`, `--reviewer`, `--comment`, `--json`. |

This lab has no tracing: Lab 5 adds OpenTelemetry and Langfuse and attaches trace
IDs to the same audit records. Lab 6 adds an MCP credit-score tool whose calls
land in the same audit trail. Audit records and execution summaries already
carry a `trace_id` field; it stays `null` here.

## Prerequisites

Complete Labs 00 to 03 in a Workshop Studio environment and confirm:

```bash
aws sts get-caller-identity
kubectl get nodes
kubectl get deployment,pods,service --namespace mortgage-assistant
```

You also need `aws` (v2), `kubectl`, `uv`, `curl`, `python3`, `openssl`, `sed`,
`grep`, Python 3.12 or later, a `docker` CLI that can build and push (Docker, or
Finch with a `docker` alias; the deploy script falls back to plain `docker
build` when `buildx` is missing), and Bedrock access for the agent model and
Titan Text Embeddings V2. With a named AWS profile, pass `--profile` to the
deploy and inspection scripts (on `invoke_eks.py` it only selects the account
used to derive the default actor).

The deploy script needs the shared cluster, ECR repository, memory table and
vector index, Knowledge Base, and AWS Load Balancer Controller. It updates the
Lab 03 Deployment in place and reuses the `mortgage-assistant-api-key` Secret
when it exists, so do not delete shared resources first. If you removed the
`mortgage-assistant` namespace, the script recreates it with a new API key.

**No infrastructure or IAM change is needed:** the pod role already allows
`GetItem`, `PutItem`, and `Query` on the table. The local utilities use your own
credentials: `inspect_memory.py` and `hydrate_memory.py` need SSM, STS,
DynamoDB, and Bedrock (embeddings); `inspect_audit.py` needs
`ssm:GetParameter`, `sts:GetCallerIdentity`, and DynamoDB `GetItem`/`Query`.

All commands below run from the `04-memory` directory (`cd 04-memory` from the
repository root).

### Workshop Studio resource discovery

| Resource | Parameter Store path |
|---|---|
| EKS cluster name | `/workshop/mortgage-assistant/eks/cluster-name` |
| ECR repository URI | `/workshop/mortgage-assistant/ecr/repository-uri` |
| DynamoDB memory table name | `/workshop/mortgage-assistant/memory/table-name` |
| DynamoDB vector index name | `/workshop/mortgage-assistant/memory/vector-index-name` |
| Bedrock Knowledge Base ID | `/workshop/mortgage-assistant/bedrock/knowledge-base-id` |

The deploy and cleanup scripts read these directly. The agent model
(`us.anthropic.claude-sonnet-4-6`) and embedding model
(`amazon.titan-embed-text-v2:0`) are fixed defaults in the deploy script. The
Python utilities discover the table the same way; `--table-name` (and, for the
memory tools, `--vector-index-name` and `--embedding-model-id`) override it.

## Architecture

Lab 04 keeps the long-running FastAPI service, two-replica Deployment, and NLB
from Lab 03. For each request FastAPI authenticates the caller, validates
`prompt`, `actor_id`, `session_id`, and `request_id`, takes one of the pod's
agent slots, and hands the request to the `Orchestrator`, which owns the lease,
the audit trail, and the execution record.

```text
client ─► NLB (port 80, source-CIDR filter) ─► Service ─► pod (Uvicorn ─► FastAPI)
                                                              │
POST /invoke ──► Orchestrator (lease, audit trail, status)    │
                     │                                        │
                     ▼                                        │
              supervisor Agent  ── hooks: Resume · Approval · Audit · FailFast
                │  tools: calculator, search_memory, add_memory (MemoryManager)
                │
                ├─ general_mortgage_specialist   ┐  (Knowledge Base `retrieve`)
                ├─ existing_mortgage_specialist  ├─ Agent.as_tool(preserve_context=True)
                └─ new_application_specialist    ┘   own agent_id, own snapshot, own hooks,
                                                      structured SpecialistReport
All state ─► one DynamoDB table (snapshots, memories + vector index, audit, exec, lock, ledger)
Models   ─► Bedrock (agent model, Titan embeddings); Pod Identity supplies credentials
```

- Every `/invoke` creates a new supervisor and three specialist `Agent`
  objects and discards them afterwards, so pods stay stateless: a later prompt
  can run on either replica and restore everything from DynamoDB.
- Each agent has its own `SnapshotSessionManager` under the same `session_id`
  and a distinct `agent_id`. The new-application specialist now remembers the
  fields it has already collected.
- Long-term memory (`MemoryManager`) is attached to the supervisor only, so a
  durable fact is stored once, at the user-facing boundary.
- Swarm and Graph were not used: they cannot persist member agents, and
  `SnapshotSessionManager` rejects multi-agent orchestrators.

### Request flow

The API checks the bearer token and takes an agent slot. The orchestrator takes
the session lease (a completed `request_id` is replayed here) and opens the audit
trail. The supervisor restores its snapshot, writes a `Routing rationale:`
sentence, and may call `search_memory` or `add_memory`; each specialist it calls
restores its own snapshot and returns a report. Gated tools pause for approval,
other side-effecting tools run through the ledger, and snapshots save after every
message. The orchestrator then records `execution_completed`, releases the lease,
and the API returns the response with an `explanation`.

## Memory concepts

**Short-term memory** is one session's conversation. It lets the assistant
answer follow-ups ("What property value did I mention?"). Snapshots live in
DynamoDB with a seven-day TTL; the application filters expired records
immediately and DynamoDB removes them asynchronously. Each agent has its own
conversation, so the supervisor and each specialist remember different things.

**Long-term memory** holds durable facts that stay relevant across sessions: a
preferred term, fixed or variable rate preference, approximate property-price
range, deposit goal, payment priority, refinancing objective, or timeline. The
Strands `MemoryManager` gives the supervisor `search_memory` and `add_memory`
tools. Memories are embedded with Titan Text Embeddings V2 and stored in the same
table; prompts are embedded and compared through a DynamoDB vector index.
Long-term memories have no TTL.

**Actor and session IDs.** Every request includes `actor_id` and `session_id`.
The client derives the default actor as `participant-<AWS-account-id>` and keeps
a generated session UUID per actor in `04-memory/.workshop/client-state.json`
(excluded from source control; it also remembers the last request per actor for
`last`). `--new-session` starts a new conversation for the same actor and
`--show-context` prints the current values. Each participant has an isolated
account, but the actor ID still demonstrates application-level isolation. In
production, derive the actor from an authenticated identity.

## DynamoDB data model

One on-demand table with string keys `pk` and `sk`, encryption with
`alias/aws/dynamodb`, point-in-time recovery, TTL on `expireAt`, and a
1,024-dimension cosine vector index partitioned by `pk`. All of an actor's data
lives under `pk=user/<actor_id>`:

| Sort-key prefix | Contents |
| --- | --- |
| `session/<session>/scopes/agent/<agent_id>/snapshots/snapshot_latest.json` | Latest snapshot per agent, saved after every message. TTL `MEMORY_SESSION_TTL_SECONDS` (7 days). |
| `session/<session>/scopes/agent/<agent_id>/snapshots/immutable_history/...` | One immutable snapshot per invocation (rollback, forensics); `--no-snapshot-history` disables it. Same TTL. |
| `memories/<key>` | Durable memory text, vector, and metadata. No TTL. |
| `audit/<session>/<request>/<seq>` | Append-only audit records with `prev_hash` and `hash`. No TTL. |
| `exec/<session>/<request>` | Execution record: status, attempt, prompt hash, interrupts, response. |
| `lock/<session>` | Per-session lease (`LEASE_SECONDS`, default 180). |
| `ledger/<session>/<request>/<tool>/<input-hash>` | Recorded results of side-effecting tools. |

Session and memory operations are scoped to the actor prefix, and vector
searches pass the actor partition. The partition is not an authorization
boundary: the shared API key protects the endpoint and Pod Identity restricts
the application to the workshop table. Audit, execution, lock, and ledger items
use a small conditional-write helper (`app/store.py`) because `DynamoDBStorage`
has no conditional writes.

In code, `app/memory.py` builds the session managers with message-level saves and
an immutable snapshot per invocation:

```python
SnapshotSessionManager(session_id, storage=session_storage,   # TTL view of the table
                       save_latest_on="message",
                       snapshot_trigger=lambda **_: SNAPSHOT_HISTORY)
```

`DynamoDBMemoryStore.add` derives the key from the request and the text, so a
retry overwrites instead of duplicating, and writes provenance with the vector;
`search` queries only the actor's partition (`top_k=MEMORY_MAX_SEARCH_RESULTS`,
default 5). Both emit `memory_write` / `memory_read` audit records:

```python
key = sha256(f"{trail.request_id}:{cleaned}")[:32]
await storage.write(f"memories/{key}", content, vector=embed_text(cleaned),
                    metadata={**metadata, "request_id": ..., "session_id": ...,
                              "actor_id": ..., "source_agent": "supervisor", "created_at": ...})
```

`create_specialist_tool` builds each specialist with its own `agent_id`,
`structured_output_model=SpecialistReport`, session manager, and hooks, and
exposes it with `agent.as_tool(preserve_context=True)`.

## Memory safety policy

The supervisor may save only durable mortgage goals and preferences. It must not
store customer IDs, account numbers, authentication data, exact income, uploaded
documents, or other sensitive financial identifiers in long-term memory. This is
a prompt instruction, not a security boundary, and the audit trail and agent
snapshots do record prompts and tool inputs. Use mock data only: do not enter
real personal, financial, or customer information.

## Audit trail

`app/audit.py` attaches an `AuditHook` to the supervisor and each specialist. It
records, tagged with `agent_id`, `request_id`, `attempt`, and `trace_id`:

- `invocation_start` / `invocation_end` (stop reason, token usage, pending interrupts)
- `decision` (the model's text and chosen tool calls), `model_response`, `model_error`
- `tool_start` / `tool_call` (inputs, results, status, duration; specialists appear
  as tools of the supervisor and their own calls appear under their own `agent_id`)
- `memory_read` (keys and scores retrieved) and `memory_write` (with provenance)
- `approval`, `rollback`, `ledger_replay`, `ledger_pending`, `lease_lost`,
  `fault_injection`, and the `execution_*` lifecycle

Records are hash-chained. `GET /executions/{request_id}` returns `chain_valid`,
false if a record is altered, removed, or reordered, and `anchor_valid`, which compares the
record count and last hash stored on the execution item when the request stopped, so deleting the
last records is also detected (`null` while a request is running). Sensitive keys
(`AUDIT_REDACT_KEYS`) are masked and long values truncated.

If an audit record for a side-effecting tool (`AUDIT_CRITICAL_TOOLS`: by default
`create_customer_id`, `create_loan_application`, and `add_memory`) cannot be
written, the tool call is **blocked** (fail closed); these tools are also blocked
once the session lease is lost. Other records degrade to a logged, dropped
record, and `execution_completed` carries `dropped_records` so a gap is visible.
Records over 32 KB keep only a digest and preview (`truncated`). Hash chaining
makes tampering evident, not impossible: someone with write access to the table can
rewrite the chain and the anchor together. For production also stream the table to
S3 with Object Lock and enable CloudTrail data events (neither is provisioned).

## Explainability

- The supervisor writes a `Routing rationale:` sentence before delegating; the
  audit trail links it to the specialist call it caused. The sentence is model
  output, so it is occasionally missing: each `explanation.route` entry then has an
  empty `reason` and `rationale_recorded: false`.
- Specialists return `answer`, `rationale`, `evidence`, and `assumptions`; the
  evidence survives in the audit trail.
- Every `/invoke` response carries `explanation`: `route`, `tools_used`,
  `evidence`, `memories_used`, `approvals`, `records`, and `attempts`.
- `ENABLE_REASONING=true` (`--enable-reasoning`) also captures Bedrock
  extended-thinking blocks for the supervisor. It is off by default, the budget
  is fixed at 2048 tokens, and models without thinking support reject the request.

## Resilience and resume

1. **Message-level snapshots** for every agent, plus an immutable snapshot per invocation.
2. **Unfinished-turn rollback.** `ResumeHook` marks a turn in `agent.state` when
   it starts and clears it when the invocation ends. If a pod dies or the
   invocation raises, the next invocation rolls the agent back to the start of
   that turn so the message list stays valid. The hook also reapplies the
   current system prompt, which a restored snapshot would otherwise overwrite.
3. **Idempotent requests.** `request_id` is the idempotency key. Re-sending it
   replays a completed request and resumes a failed one (`attempt` increments).
   It returns `409` while the request is running, the session is busy, another
   request in the session waits for approval, or the ID was used with a
   different prompt.
4. **Tool ledger.** `create_customer_id` and `create_loan_application` run at most
   once per session, request, and input; a retry gets the recorded result. The
   claim is a conditional write, so two identical parallel calls run the effect
   once. Memory writes get the same protection from their deterministic key.
5. **Fail fast.** Strands turns a specialist's exception into an error tool
   result that a model can paper over with an apology. `FailFastHook`
   (supervisor only) fails the request instead, so it is resumable.
6. **Human approval.** `create_loan_application` (configurable with
   `APPROVAL_REQUIRED_TOOLS`) pauses with a Strands interrupt and `/invoke`
   returns `202` with the pending approvals. The interrupt is stored in the agent
   snapshots, so any pod can finish the request after the reviewer answers. If a
   pod dies while continuing an approval, retrying `/invoke` or `/approvals`
   returns the pending approval again. If an approval can no longer be completed
   (for example its snapshots expired), `POST /executions/{id}/cancel` abandons
   it and clears the paused agents so the session accepts new prompts.

## Step 1: Review the Lab 04 files

`app/` holds the API (`mortgage_api.py`), orchestration (`service.py`), agents
and tools (`mortgage_agent.py`), memory (`memory.py`), the resilience modules
(`audit.py`, `execution.py`, `ledger.py`, `approvals.py`, `resilience.py`,
`store.py`), the client (`invoke_eks.py`), and `inspect_memory.py` and
`inspect_audit.py`. `scripts/` holds `deploy-memory.sh`, `cleanup-memory.sh`,
`check_memory_ready.py` (used by the deploy script), and `hydrate_memory.py`
(test-data tooling; it creates no infrastructure).

## Step 2: Test the module locally

```bash
uv sync --frozen
uv run python -m unittest discover --start-directory tests --verbose
```

The first run installs the locked dependencies (a minute or two). The 109 tests
do not invoke Bedrock or modify AWS. They cover the API contract, identifier
rules, TTL separation, client state, audit chaining, leases, the ledger, and
approvals. `tests/test_agent_flows.py` runs the real supervisor, `as_tool`
specialists, hooks, snapshots, and orchestrator with a scripted model, covering
audit and explanation, replay, rollback and resume, approval after the agents
are rebuilt, and denial.

## Step 3: Deploy

```bash
./scripts/deploy-memory.sh --region us-west-2
./scripts/deploy-memory.sh --region us-west-2 --profile YOUR_AWS_PROFILE   # named profile
```

The script reads the Parameter Store values, confirms the table and vector index
are `ACTIVE`, builds and pushes a `lab04-agent-*` image, applies the manifest,
reuses the API-key Secret, waits for the pods and NLB, and smoke-tests an
invocation. The smoke test also fetches the audit trail, requires `chain_valid`,
and replays the `request_id`. It creates no AWS infrastructure.

| Flag | Effect |
| --- | --- |
| `--service-access-cidr CIDR` | Source CIDR for the NLB (default: your public IP `/32`). |
| `--session-ttl-seconds N` | Short-term retention, at least 3600 (default 604800). |
| `--approval-required-tools LIST` | Tools that pause for approval (default `create_loan_application`; empty disables). |
| `--lease-seconds N` | Per-session lease, at least 30 (default 180). |
| `--enable-reasoning` | Capture Bedrock extended-thinking in the audit trail. |
| `--no-snapshot-history` | Keep only the latest snapshot per agent. |
| `--image-uri URI` | Reuse an image already in ECR and skip build and push (about 2 minutes instead of about 12). |
| `--fault-injection-enabled`, `--fault-injection-tool`, `--fault-injection-mode delay\|error\|abort\|crash`, `--fault-injection-delay-seconds` | Fault-injection exercises. Wired into `get_mortgage_details` only. |
| `--prompt TEXT` | Smoke-test prompt. |

## Step 4: Check the deployment

```bash
kubectl get deployment,pods,service --namespace mortgage-assistant
kubectl logs --namespace mortgage-assistant deployment/mortgage-assistant --tail=100
```

## Step 5: Display your actor and session

```bash
uv run app/invoke_eks.py --region us-west-2 --show-context
```

```text
Actor:   participant-123456789012
Session: session-6e18ca6d-70d1-4be5-ae3b-ff4a98b13c3a
```

Keep this session for the short-term memory tests. Every later client call
prints these two lines first. Request IDs identify a request: `--prompt` creates
one for you and remembers it per actor, and `last` means the most recent
`--prompt`.

## Step 6: Test short-term memory

```bash
uv run app/invoke_eks.py --region us-west-2 \
  --prompt "I am considering a property worth 600,000 dollars."
uv run app/invoke_eks.py --region us-west-2 \
  --prompt "What property value did I mention in this conversation?"
```

The assistant identifies the $600,000 property value: the client reused the
actor and session, so a newly created supervisor restored its DynamoDB snapshot.

## Step 7: Prove the session survives an EKS restart

```bash
kubectl rollout restart deployment/mortgage-assistant --namespace mortgage-assistant
kubectl rollout status deployment/mortgage-assistant --namespace mortgage-assistant
uv run app/invoke_eks.py --region us-west-2 \
  --prompt "What property value did I tell you earlier?"
```

The assistant still identifies $600,000. The previous pods are gone; the
conversation was restored from DynamoDB.

## Step 8: Hydrate deterministic test memories

```bash
uv run scripts/hydrate_memory.py seed --region us-west-2 --replace
uv run app/inspect_memory.py --region us-west-2 --memories
```

The script derives the actor from your AWS account, stores a $600,000 property
goal, a 15-year fixed-rate preference, and an early-payoff priority, and waits
briefly for the eventually consistent vector index. Hydrated memories carry no
provenance metadata; only memories the agent saves do. To clear them:

```bash
uv run scripts/hydrate_memory.py clear --region us-west-2              # memories only
uv run scripts/hydrate_memory.py clear --region us-west-2 --all-data   # sessions and memories
```

`--all-data` removes the actor's snapshots and memories (including the conversation you built in
Steps 5 to 7) but not its audit, execution, lock, or ledger items. If the agent saved a
preference earlier on its own, `seed --replace` reports removing it.

## Step 9: Let the agent save a long-term preference, with provenance

```bash
uv run app/invoke_eks.py --region us-west-2 \
  --prompt "Remember for future conversations that I prefer a 15-year fixed-rate mortgage and prioritize paying the loan off early."
uv run app/inspect_memory.py --region us-west-2 --memories
```

The supervisor should call `add_memory` and confirm the save. If nothing appears,
repeat the prompt with the phrase `Remember for future conversations` and check
the pod logs. The memory text shows in `inspect_memory.py`; its provenance is in
the audit record of the request that wrote it:

```bash
uv run app/invoke_eks.py --trail last --json | tail -n +3 | python3 -c \
  'import json,sys; [print(json.dumps(r["data"], indent=2)) for r in json.load(sys.stdin)["records"] if r["type"]=="memory_write"]'
```

Expect `key` (`memories/<hash>`), `content`, `request_id`, `session_id`,
`actor_id`, `source_agent: supervisor`, and `created_at`. Memory writes are
idempotent per request: the key derives from the `request_id` and the text, so
retrying the same request overwrites the memory instead of adding a duplicate.
Re-sending a completed `request_id` replays the stored response without calling
the agents.

## Step 10: Test long-term memory in a new session

```bash
uv run app/invoke_eks.py --region us-west-2 --new-session \
  --prompt "What kind of mortgage do I prefer?"
```

The assistant recalls the 15-year fixed-rate preference and early-payoff
priority. The new session has no transcript, so the answer comes from semantic
long-term memory. To see what was retrieved:

```bash
uv run app/invoke_eks.py --trail last --json | tail -n +3 | python3 -c \
  'import json,sys; print(json.dumps(json.load(sys.stdin)["explanation"]["memories_used"], indent=2))'
```

`memories_used` lists the retrieved keys, scores (a distance: lower is closer, results are sorted ascending), and content (empty if the model
did not call `search_memory`).

## Step 11: Test semantic retrieval

```bash
uv run app/invoke_eks.py --region us-west-2 --new-session \
  --prompt "Do you remember how quickly I wanted to repay my home loan?"
uv run app/inspect_memory.py --region us-west-2 --search "preferred repayment period"
```

The assistant connects the question with the early-payoff preference although
the wording differs. DynamoDB vector indexes are eventually consistent, so the
inspection utility retries for up to 60 seconds (`--wait-seconds`).

## Step 12: Test actor isolation

```bash
uv run app/invoke_eks.py --region us-west-2 --actor-id alternate-user --new-session \
  --prompt "What mortgage preferences do you remember about me?"
```

The assistant does not return the default participant's preferences. Omit
`--actor-id` to return to the default actor.

## Step 13: Inspect all memory records

```bash
uv run app/inspect_memory.py --region us-west-2
uv run app/inspect_memory.py --region us-west-2 --sessions | grep snapshot_latest
uv run app/inspect_memory.py --region us-west-2 --actor-id alternate-user
```

The output separates short-term records under `session/` from long-term records
under `memories/`. Each session holds one scope per agent that has run:
`scopes/agent/supervisor/` plus `general`, `existing`, or `new_application` for
the specialists the supervisor called. Each scope has a `snapshot_latest` and,
with `SNAPSHOT_HISTORY`, one immutable snapshot per invocation under
`immutable_history/`. Audit, execution, lease, and ledger items are not shown
here; use `inspect_audit.py` (Step 14).

## Step 14: Audit and explain a request

```bash
uv run app/invoke_eks.py --prompt "What is the balance on customer ID 123456's mortgage?"
uv run app/invoke_eks.py --trail last
```

Expect `Request ... COMPLETED (attempt 1), hash chain valid`, a
`routed to existing_mortgage_specialist: Routing rationale: ...` line, tool calls
under both `supervisor` and `existing`, and an ordered record list. Add `--json`
for the full `explanation` block and `chain_valid`. To read the table directly
with your AWS identity (no API key):

```bash
uv run app/inspect_audit.py --region us-west-2 --session-id <Session value>
uv run app/inspect_audit.py --region us-west-2 --session-id <Session value> --request-id <id> --records
```

## Step 15: Resume a failed request

Switch the fault on without rebuilding (`abort` fails the whole request after
`get_mortgage_details` returns):

```bash
kubectl set env deployment/mortgage-assistant -n mortgage-assistant \
  FAULT_INJECTION_ENABLED=true FAULT_INJECTION_TOOL=get_mortgage_details FAULT_INJECTION_MODE=abort
kubectl rollout status deployment/mortgage-assistant -n mortgage-assistant

RID=demo-$(date +%s)
# Use a customer ID you have not asked about yet: the specialist remembers earlier answers
# and would skip the tool (and the fault) for a customer it has already looked up.
uv run app/invoke_eks.py --request-id $RID --prompt "What is the balance on customer ID 654321's mortgage?"   # HTTP 500

kubectl set env deployment/mortgage-assistant -n mortgage-assistant \
  FAULT_INJECTION_ENABLED=false FAULT_INJECTION_MODE=delay
kubectl rollout status deployment/mortgage-assistant -n mortgage-assistant

uv run app/invoke_eks.py --resume $RID     # completes as attempt 2
uv run app/invoke_eks.py --trail $RID
```

The trail shows attempt 1 with a `fault_injection` record and `execution_failed`,
then attempt 2 with a `rollback` record and `execution_completed`, all in one
valid hash chain. The failed attempt's tool results remain in the specialist's
history, so the final answer may mention the earlier failure. (Rerunning
`./scripts/deploy-memory.sh --fault-injection-enabled --fault-injection-mode abort --image-uri <image>`
does the same through the script and also resets any `kubectl set env` changes.)

## Step 16: Approve a side effect, across a restart

```bash
uv run app/invoke_eks.py --prompt "Start a new application: customer 123456, name Sam, age 30, annual income 90000, annual expenses 40000. Create the application now."
```

A good response prints `Awaiting approval for:` followed by the gated tool
(`approve_create_loan_application: {...}`) and `status: awaiting_approval`; the API
returns HTTP 202 for it.
If the model asks a follow-up question instead, answer it with another
`--prompt` until you see the approval request; `--approve` reports "has no
pending approvals" otherwise.

```bash
kubectl rollout restart deployment/mortgage-assistant -n mortgage-assistant
kubectl rollout status deployment/mortgage-assistant -n mortgage-assistant
uv run app/invoke_eks.py --approve last --reviewer pat
uv run app/invoke_eks.py --trail last      # shows the approval by pat and one create_loan_application call
```

Until the approval is answered, any other request in the same session returns
`409`. Use `--deny last --reviewer pat --comment "reason"` to refuse: the tool
does not run and the model is told it was denied.

**Cancel instead.** Trigger another approval request (use a new customer ID),
then abandon it and confirm the session accepts prompts again:

```bash
uv run app/invoke_eks.py --cancel last
uv run app/invoke_eks.py --prompt "What is a fixed-rate mortgage?"
```

The request becomes `CANCELLED`, an `execution_cancelled` record is added, and
the paused agents are cleared.

## Step 17: Optional, crash mid-turn

```bash
kubectl set env deployment/mortgage-assistant -n mortgage-assistant \
  FAULT_INJECTION_ENABLED=true FAULT_INJECTION_TOOL=get_mortgage_details FAULT_INJECTION_MODE=crash \
  LEASE_SECONDS=30
kubectl rollout status deployment/mortgage-assistant -n mortgage-assistant
RID=crash-$(date +%s)
uv run app/invoke_eks.py --request-id $RID --prompt "What is the balance on customer ID 777001's mortgage?"
```

The client reports that the connection was closed and the pod restarts
(`kubectl get pods -n mortgage-assistant` shows `RESTARTS 1`). Turn the fault off
before resuming, otherwise the retry runs the same tool and crashes the pod again:

```bash
kubectl set env deployment/mortgage-assistant -n mortgage-assistant \
  FAULT_INJECTION_ENABLED=false FAULT_INJECTION_MODE=delay LEASE_SECONDS=180
kubectl rollout status deployment/mortgage-assistant -n mortgage-assistant
uv run app/invoke_eks.py --resume $RID
uv run app/invoke_eks.py --trail $RID      # attempt 1 stops mid-turn; the next attempt starts with a rollback record
```

A resume inside the lease window returns `409` (the dead request still holds the
session); wait for the lease to expire and retry. A running request keeps
renewing its lease, so only a crashed one expires.

## API reference

All routes except the health probes require `Authorization: Bearer <key>`.
`actor_id`, `session_id`, and `request_id` match `^[A-Za-z0-9][A-Za-z0-9._:-]*$`
(up to 128 characters); `prompt` is 1 to 4000 characters.

| Route | Purpose |
| --- | --- |
| `POST /invoke` | `prompt`, `actor_id`, `session_id`, optional `request_id`. `200` completed or replayed, `202` awaiting approval, `409` conflict, `429` all agent slots busy (`Retry-After: 5`), `500` failed (retry with the same `request_id`). |
| `POST /executions/{request_id}/resume` | `actor_id`, `session_id`. Retry a failed request with its stored prompt (`404` if unknown). |
| `POST /executions/{request_id}/approvals` | `actor_id`, `session_id`, `decisions: [{interrupt_id, approved, comment, reviewer}]` answering every pending approval exactly once. |
| `POST /executions/{request_id}/cancel` | `actor_id`, `session_id`. Abandon a stuck or unwanted request (`CANCELLED`); returns the execution summary. `409` if already completed or cancelled. |
| `GET /executions/{request_id}?actor_id=&session_id=` | `execution` summary, `chain_valid`, `explanation`, and ordered `records` (`404` if unknown). |
| `GET /sessions/{session_id}/executions?actor_id=` | Execution summaries for a session. |
| `GET /health`, `GET /health/ready` | Liveness; readiness also reports the Knowledge Base ID, model, memory table, and embedding model. |

To call the API directly, export the endpoint and key first (the client does this
for you):

```bash
export MORTGAGE_API_URL="http://$(kubectl get service mortgage-assistant -n mortgage-assistant \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')"
export MORTGAGE_API_KEY="$(kubectl get secret mortgage-assistant-api-key -n mortgage-assistant \
  -o jsonpath='{.data.api-key}' | base64 --decode)"
curl --request POST "$MORTGAGE_API_URL/invoke" \
  --header "Authorization: Bearer $MORTGAGE_API_KEY" \
  --header "Content-Type: application/json" \
  --data '{"prompt": "What do you remember about my mortgage preference?",
           "actor_id": "participant-123456789012", "session_id": "session-example",
           "request_id": "example-1"}'
```

The response has `request_id`, `actor_id`, `session_id`, `status` (`completed` or
`awaiting_approval`), `response`, `interrupts`, `attempt`, `duration_ms`, and
`explanation`. Execution records use `RUNNING`, `COMPLETED`, `FAILED`,
`INTERRUPTED`, and `CANCELLED`. Execution summaries expose only the error class,
but the trail endpoint returns full tool results and prompts.

## Configuration reference

| Variable | Default | Effect |
| --- | --- | --- |
| `MODEL_ID` | `us.anthropic.claude-sonnet-4-6` | Agent model. |
| `KB_PARAMETER_NAME` | canonical SSM path | Knowledge Base ID lookup. |
| `MEMORY_TABLE_NAME`, `MEMORY_VECTOR_INDEX_NAME`, `MEMORY_EMBEDDING_MODEL_ID` | from SSM; `amazon.titan-embed-text-v2:0` | Storage and embeddings. |
| `MEMORY_SESSION_TTL_SECONDS` | `604800` | Snapshot TTL (`--session-ttl-seconds`). |
| `SNAPSHOT_HISTORY` | `true` | Immutable snapshot per invocation (`--no-snapshot-history`). |
| `MAX_TOOL_RESULT_CHARS` | `16000` | Longest text a single tool result may add to a conversation; longer results are truncated (a `tool_result_truncated` audit record is written). Each agent's whole conversation is one DynamoDB item (limit 400 KB), so this keeps long sessions saveable. |
| `APPROVAL_REQUIRED_TOOLS` | `create_loan_application` | Tools that pause for approval. |
| `LEASE_SECONDS` | `180` | Session lease; the heartbeat renews every third of it. |
| `ENABLE_REASONING` | `false` | Capture extended thinking. |
| `FAULT_INJECTION_ENABLED`, `_TOOL`, `_MODE`, `_DELAY_SECONDS` | `false`, `get_mortgage_details`, `delay`, `5` | Fault injection. |
| `MORTGAGE_API_KEY` | `mortgage-assistant-api-key` Secret | Bearer key. |
| `AGENT_CONCURRENCY` | `4` | Agent turns per pod. Not in the manifest; set with `kubectl set env`. |
| `MEMORY_MAX_SEARCH_RESULTS` | `5` | Vector search `top_k`. Not in the manifest. |
| `AUDIT_CRITICAL_TOOLS` | `create_customer_id,create_loan_application,add_memory` | Tools blocked when their audit record cannot be written. Not in the manifest. |
| `AUDIT_REDACT_KEYS` | `password,token,authorization,api_key,ssn` | Keys masked in audit records. Not in the manifest. |

## Known limitations

- **The bearer key is an administrator key.** `actor_id`, `session_id`, and the
  approval `reviewer` are caller-supplied, so anyone holding the key can read any
  actor's audit trail, approve their own requests, and cancel others'. Audit
  records are attributable to those claims, not to verified identities.
  Production needs per-user authentication (for example a JWT subject mapped to
  `actor_id`) and a four-eyes rule on approvals.
- **Plain HTTP** through the Network Load Balancer. Put TLS (an ACM certificate
  on the NLB listener) in front of it before using real data.
- **Lease and heartbeat.** The heartbeat renews the lease every third of
  `LEASE_SECONDS` while a request runs; if a renewal is refused, the trail
  records `lease_lost` and side-effecting tools are blocked. After a crash the
  lease simply expires, so a retry can wait up to `LEASE_SECONDS`.
- **Crash window.** A crash between a side effect and its ledger write can repeat
  the effect once (the trail marks it `ledger_pending` / `possibly_executed`).
  Real downstream systems should also take an idempotency key.
- **SDK-private state.** `Agent.as_tool` resumes a paused specialist from the same
  `Interrupt` object the parent registered. After a restart, parent and
  specialist each restore their own copy, so `share_interrupts()` in
  `app/resilience.py` re-links them, and `reset_interrupt_state()` clears paused
  agents on cancel. Both read the SDK-private `_interrupt_state`; the approval
  tests in `tests/test_agent_flows.py` and `tests/test_hardening.py` guard them,
  and `tests/test_integrity.py` checks that the private attributes still exist.
  Strands is therefore pinned to `>=1.56.0,<1.57`; after bumping the pin, run the
  tests and repeat the approval exercise before releasing.
- **Throttling.** At most `AGENT_CONCURRENCY` agent turns run per pod; more return
  `429`. Uvicorn's limit of 32 connections keeps health probes from starving, and
  pods get 330 seconds to finish in-flight turns on shutdown.
- **Sensitive data persists.** Audit, execution, and ledger items have no TTL and
  contain prompts and tool inputs; cleanup does not delete them.

## Troubleshooting

### The shared table or vector index is unavailable

```bash
aws ssm get-parameters --region us-west-2 --names \
  /workshop/mortgage-assistant/memory/table-name /workshop/mortgage-assistant/memory/vector-index-name
```

If either parameter is absent or empty, use the Workshop Studio support path.
To check the status, run
`uv run python scripts/check_memory_ready.py --region us-west-2 --table-name <table> --vector-index-name <index>`;
it prints `<table status> <index status>` and both must be `ACTIVE` (index
creation and backfill can take several minutes). The utilities need boto3
1.43.94 or later (pinned in `uv.lock`) for DynamoDB vector operations.

### Memory is not recalled

Run `uv run app/invoke_eks.py --show-context` and confirm both prompts used the
same actor and session (`--new-session` intentionally clears short-term context).
For long-term memory, check that a memory exists with `inspect_memory.py
--memories`; if it does, retry because vector updates are eventually consistent,
otherwise ask with `Remember for future conversations`.

### HTTP 401, 409, 429, and 500

- `401`: the client reads the key from the Kubernetes Secret. For curl, export
  `MORTGAGE_API_KEY="$(kubectl get secret mortgage-assistant-api-key -n mortgage-assistant -o jsonpath='{.data.api-key}' | base64 --decode)"`.
- `409`: read the message. `session ... is busy`: another request holds the lease
  (after a crash, wait for `LEASE_SECONDS`). `request ... is awaiting approval`:
  approve, deny, or `--cancel` it. `request_id ... used with a different prompt`:
  pick a new `--request-id`.
- `429`: all agent slots in the pod are busy; retry with the same `request_id`.
- `500`: the request failed and is resumable. Run `--resume <request_id>` (the
  client prints it) and inspect `--trail`.

### Pods receive AccessDenied from DynamoDB

Confirm Parameter Store discovery works and read the pod logs (`kubectl logs
--namespace mortgage-assistant deployment/mortgage-assistant --tail=200`). If
discovery works but access is denied, use the Workshop Studio support path to
verify the EKS Pod Identity permissions.

### `kubectl` or the deploy script times out

`Unable to connect to the server: dial tcp ...:443: i/o timeout` usually means your public IP
changed since Lab 0 and the EKS API only allows the old one. Rerun
`./00-workshop-setup/scripts/deploy-infrastructure.sh --region us-west-2` (about two minutes).
The deploy script also re-detects your IP for the load balancer's source range.

### The audit chain is invalid or records are missing

`chain_valid: false` means a record was altered, removed, or reordered; `anchor_valid: false` with a
valid chain means records were removed from the end. A
non-zero `dropped_records` on `execution_completed` means non-critical records
could not be written; look for `Dropped non-critical audit record` in the pod logs.

## What is still missing for production

This workshop is not a production deployment. It demonstrates several patterns
that are sound foundations: a new agent per request with state outside the pods,
idempotent and resumable execution, managed data protection (on-demand DynamoDB,
AWS-managed KMS encryption, point-in-time recovery, TTL), Pod Identity instead
of static keys, and hardened containers (two replicas, probes, a Pod Disruption
Budget, non-root, read-only root filesystem, restricted Pod Security labels).
These controls remain outside its scope:

| Workshop implementation | Recommended solution |
|---|---|
| One shared bearer API key; caller-supplied `actor_id` and `reviewer`. | Use Amazon Cognito or another OIDC provider, derive the actor from the token's immutable `sub` claim, and require a different approver than the requester. |
| Internet-facing HTTP NLB. | Terminate TLS with ACM; use an ALB or API Gateway with AWS WAF when needed, or a private endpoint. |
| Hash-chained audit in the same table with no external anchor. | Stream to S3 with Object Lock, enable CloudTrail data events, and define audit retention and access controls. |
| The model decides when to save memory; safety policy is prompt-only. | Add a deterministic policy layer (validate, redact, deduplicate, authorize), Bedrock Guardrails, strict tool schemas, and adversarial tests. |
| `strands-dynamodb-storage==0.1.2` is an early version. | Review its support posture, test recovery and scale, and keep an application-owned storage interface. |
| No tracing or alarms (Lab 5 adds tracing). | Export traces and metrics; alarm on latency, errors, throttling, failed writes, token usage, and vector-search failures. |
| Fixed two replicas, no HPA; laptop deployment; unit tests only; no tested disaster recovery. | Load test and autoscale across AZs; use CI/CD with scanning and signed images; add integration, failure-injection, and evaluation tests; define RTO and RPO and test restores. Minimize and classify the sensitive data in prompts, memories, and audit records, and define retention and deletion workflows. |

Before using this design for real mortgage information, complete formal
security, privacy, reliability, model-risk, and operational-readiness reviews.

## Cleanup

```bash
./scripts/cleanup-memory.sh --region us-west-2
```

This removes the `mortgage-assistant` namespace (the application, its API-key
Secret, and the load balancer) only. Workshop Studio continues to manage the
DynamoDB table, vector index, IAM resources, EKS cluster, ECR repository, and
Knowledge Base. Items stay in the table: snapshots expire by TTL, memories can be
removed with `hydrate_memory.py clear`, and audit, execution, and ledger items
have no TTL, so delete them with the AWS console or CLI if you need to. To run
Lab 03 again afterward:

```bash
cd ../03-eks-service
./scripts/deploy-application.sh --region us-west-2
```

To remove the entire workshop, use the Workshop Studio cleanup instructions.

## Completion checkpoint

You have completed Lab 04 when:

- The API returns `request_id`, `status`, `attempt`, and an `explanation`.
- A follow-up prompt recalls the current session, and the session survives an
  EKS rollout restart.
- A new session recalls a stored preference, a paraphrased question retrieves it,
  and an alternate actor does not.
- You can show a saved memory's provenance and the memories used in an answer
  from the audit trail.
- `--trail` shows the routing rationale, per-agent tool calls, and a valid hash chain.
- A failed request resumes as attempt 2 with the same `request_id`.
- A loan application waits for approval, survives a pod restart, and runs once
  after approval; an abandoned approval can be cancelled.
