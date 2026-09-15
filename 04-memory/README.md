# Lab 04: Add durable memory with Strands and Amazon DynamoDB

In this lab, you add short-term and long-term memory to the mortgage
assistant running on Amazon EKS.

The implementation follows the architecture described in
[Introducing Strands DynamoDB Storage: Durable Agent Storage for the Strands Agents SDK](https://aws.amazon.com/blogs/database/introducing-strands-dynamodb-storage-durable-agent-storage-for-the-strands-agents-sdk/).
It does not use Amazon Bedrock AgentCore.

## Learning objectives

After completing this lab, you will be able to:

- Explain the difference between short-term session state and long-term memory.
- Persist Strands session snapshots in DynamoDB.
- Store and semantically retrieve durable user preferences.
- Scope memory by actor and session.
- Resume a conversation after an EKS pod is replaced.
- Recall a preference from a new conversation.
- Verify that one actor cannot retrieve another actor's memories.
- Inspect the DynamoDB records used by the agent.

## Estimated time

Allow approximately 45–60 minutes, including deployment and the memory
exercises.

## Architecture

```text
Participant laptop
        |
        | prompt + actor_id + session_id
        v
Internet-facing NLB
        |
        v
FastAPI service on EKS (two replicas)
        |
        v
Strands supervisor agent
   |                         |
   | SnapshotSessionManager  | MemoryManager
   v                         v
Short-term snapshots      Semantic long-term memories
   |                         |
   +-------------+-----------+
                 |
                 v
      One DynamoDB table and vector index
```

The existing Bedrock Knowledge Base and mortgage tools remain unchanged.
Lab 04 updates the same `mortgage-assistant` Kubernetes Deployment and
continues to use the same Network Load Balancer.

## Memory concepts

### Short-term memory

Short-term memory is the conversation associated with one session. It lets
the assistant understand follow-up questions such as:

```text
User: I am considering a property worth $600,000.
User: What property value did I mention?
```

`SnapshotSessionManager` writes a snapshot after every invocation. A new
agent process with the same actor and session IDs restores that snapshot.

Short-term records use a seven-day DynamoDB TTL by default. The application
filters expired records immediately, while DynamoDB removes them
asynchronously.

### Long-term memory

Long-term memory contains durable facts that remain relevant across
sessions. Examples include:

- Preferred mortgage term.
- Fixed or variable-rate preference.
- Approximate property-price range.
- Deposit goal.
- Monthly-payment priority.
- Refinancing objective.
- Application timeline.

The Strands `MemoryManager` gives the supervisor `search_memory` and
`add_memory` capabilities. Durable memories are embedded with Amazon Titan
Text Embeddings V2 and stored in the same DynamoDB table. Future prompts are
embedded and compared through a DynamoDB vector index.

Long-term memories do not use the short-term session TTL.

## Actor and session IDs

Every API request includes:

```json
{
  "prompt": "What did I tell you?",
  "actor_id": "participant-123456789012",
  "session_id": "session-6e18ca6d-..."
}
```

### Actor ID

The client derives the default actor from the current AWS account:

```text
participant-<AWS-account-id>
```

Each workshop participant has an isolated AWS account and DynamoDB table.
The actor ID still provides a useful application-level memory namespace and
allows this lab to demonstrate actor isolation.

For a production application, derive the actor from an authenticated user
identity. Do not trust an arbitrary actor ID supplied by an unauthenticated
caller.

### Session ID

The client generates a UUID on the first invocation and stores it in:

```text
04-memory/.workshop/client-state.json
```

Normal invocations reuse the current session. `--new-session` creates a new
conversation while retaining the same actor.

Display the current values:

```bash
python3 app/invoke_eks.py --show-context
```

The `.workshop` directory is excluded from source control.

## DynamoDB design

Lab 04 uses one on-demand DynamoDB table with:

- String partition key `pk`.
- String sort key `sk`.
- Customer-managed AWS KMS encryption key.
- Point-in-time recovery.
- TTL enabled on the `expireAt` attribute.
- A 1,024-dimension cosine vector index.
- `pk` as the vector search partition.

This infrastructure is owned and provisioned by Lab 00 before participants
receive their workshop accounts. Lab 04 reads the table and model settings
from the Lab 00 CloudFormation outputs and only deploys application code.

All data for an actor uses a prefix such as:

```text
user/participant-123456789012
```

Example logical keys:

```text
user/participant-123456789012/session/session-123/...
user/participant-123456789012/memories/8a1c3f...
```

Both session and memory operations are scoped to this prefix. Vector searches
also specify the actor partition, preventing accidental cross-actor recall in
application code.

The actor partition is not an authorization boundary by itself. The shared
workshop API key protects the endpoint, while EKS Pod Identity restricts the
application to the workshop table.

## How the implementation works

### DynamoDB storage

The application creates two views of the same table:

```python
durable_storage = DynamoDBStorage(
    table_name,
    prefix=f"user/{actor_id}",
)

session_storage = DynamoDBStorage(
    table_name,
    prefix=f"user/{actor_id}",
    ttl_seconds=604800,
)
```

The session view applies TTL. The durable view does not.

### Short-term session manager

```python
session_manager = SnapshotSessionManager(
    session_id,
    storage=session_storage,
)
```

The manager restores an existing snapshot when the supervisor is created and
saves a new snapshot after the invocation.

### Long-term memory manager

`app/memory.py` implements a small `DynamoDBMemoryStore` adapter. Its `add`
method embeds and writes a memory:

```python
await storage.write(
    memory_key,
    content.encode("utf-8"),
    vector=embed_text(content),
    metadata=metadata,
)
```

Its `search` method embeds a prompt and searches only the current actor's
partition:

```python
await storage.search(
    SearchQuery(
        vector=embed_text(query),
        top_k=5,
        pk=actor_partition,
        include_values=True,
    )
)
```

The store is connected to the supervisor:

```python
memory_manager = MemoryManager(
    stores=[memory_store],
    add_tool_config=True,
)
```

The specialized mortgage subagents remain stateless tools. Memory is applied
at the user-facing supervisor boundary so one conversation is stored once.

## Memory safety policy

The supervisor is instructed to save only durable mortgage goals and
preferences. It must not put these values into long-term memory:

- Customer IDs.
- Account numbers.
- Authentication data.
- Exact income.
- Uploaded documents.
- Other sensitive financial identifiers.

This workshop uses mock data. Do not enter real personal, financial, or
customer information.

## Prerequisites

Complete Labs 00–03 and confirm:

```bash
aws sts get-caller-identity

kubectl get nodes

kubectl get deployment,pods,service \
  --namespace mortgage-assistant
```

You also need:

- AWS CLI v2.
- `kubectl`.
- Docker with Buildx, or Finch's Docker-compatible CLI.
- `uv`.
- Python 3.12 or later.
- Access to the existing EKS cluster and ECR repository.
- Bedrock model access for the agent model and Titan Text Embeddings V2.

When using a named AWS profile, pass `--profile` to both deployment and
client commands.

## Step 1: Review the Lab 04 files

```text
04-memory/
├── app/
│   ├── inspect_memory.py
│   ├── invoke_eks.py
│   ├── memory.py
│   ├── mortgage_agent.py
│   └── mortgage_api.py
├── k8s/
├── scripts/
│   ├── cleanup-memory.sh
│   ├── deploy-memory.sh
│   └── hydrate_memory.py
├── tests/
├── Dockerfile
├── pyproject.toml
└── uv.lock
```

Lab 00 contains the CloudFormation resources and the provisioning helper that
create the KMS key, runtime IAM policy, DynamoDB table, and vector index.
`hydrate_memory.py` is participant-facing test-data tooling; it does not create
infrastructure.

## Step 2: Install and test the module locally

From the repository root:

```bash
cd 04-memory

uv sync --frozen

uv run python -m unittest discover \
  --start-directory tests \
  --verbose
```

These tests validate the API contract, identifier rules, TTL separation, and
client session-state behaviour. They do not invoke Bedrock or modify AWS.

## Step 3: Deploy the memory-enabled application

```bash
chmod +x \
  scripts/deploy-memory.sh \
  scripts/cleanup-memory.sh \
  scripts/hydrate_memory.py

./scripts/deploy-memory.sh \
  --region us-west-2
```

For a named profile:

```bash
./scripts/deploy-memory.sh \
  --region us-west-2 \
  --profile YOUR_AWS_PROFILE
```

The deployment script:

1. Reads the existing Lab 00 stack outputs.
2. Confirms the pre-provisioned memory table and vector index are active.
3. Builds and pushes the Lab 04 image.
4. Updates the existing EKS Deployment.
5. Reuses the existing Kubernetes API-key Secret when present.
6. Waits for the pods and API to become ready.
7. Sends one smoke-test prompt.

The script does not create or update AWS infrastructure. EKS, the Knowledge
Base, ECR, IAM, KMS, DynamoDB, and the vector index are owned by Lab 00.

## Step 4: Check the deployment

```bash
kubectl get deployment,pods,service \
  --namespace mortgage-assistant

kubectl logs \
  --namespace mortgage-assistant \
  deployment/mortgage-assistant \
  --tail=100
```

Confirm the memory configuration:

```bash
kubectl get deployment mortgage-assistant \
  --namespace mortgage-assistant \
  --output jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}' |
grep MEMORY
```

## Step 5: Display your actor and session

```bash
python3 app/invoke_eks.py \
  --region us-west-2 \
  --show-context
```

Example:

```text
Actor:   participant-123456789012
Session: session-6e18ca6d-70d1-4be5-ae3b-ff4a98b13c3a
```

Keep this session for the short-term memory tests.

## Step 6: Test short-term memory

Tell the assistant a fact that should remain within the active conversation:

```bash
python3 app/invoke_eks.py \
  --region us-west-2 \
  --prompt "I am considering a property worth 600,000 dollars."
```

Ask a follow-up without supplying IDs:

```bash
python3 app/invoke_eks.py \
  --region us-west-2 \
  --prompt "What property value did I mention in this conversation?"
```

Expected result:

```text
The assistant identifies the $600,000 property value.
```

The client reused the same actor and session, so the newly created supervisor
restored the DynamoDB snapshot.

## Step 7: Prove the session survives an EKS restart

Restart both EKS replicas:

```bash
kubectl rollout restart deployment/mortgage-assistant \
  --namespace mortgage-assistant

kubectl rollout status deployment/mortgage-assistant \
  --namespace mortgage-assistant
```

Ask again using the existing client session:

```bash
python3 app/invoke_eks.py \
  --region us-west-2 \
  --prompt "What property value did I tell you earlier?"
```

Expected result:

```text
The assistant still identifies $600,000.
```

The previous Python process and EKS pods are gone. The conversation was
restored from DynamoDB.

## Step 8: Hydrate deterministic test memories

For a predictable long-term-memory test, seed the sample mortgage profile:

```bash
uv run scripts/hydrate_memory.py seed \
  --region us-west-2 \
  --replace
```

The script derives the actor from the participant's AWS account and stores:

- An approximate $600,000 property goal.
- A preference for a 15-year fixed-rate mortgage.
- A priority to pay the mortgage off early.

It also waits briefly for the eventually consistent vector index to return the
hydrated records.

Inspect them:

```bash
uv run app/inspect_memory.py \
  --region us-west-2 \
  --memories
```

Clear only the hydrated long-term memories:

```bash
uv run scripts/hydrate_memory.py clear \
  --region us-west-2
```

Clear both sessions and long-term memories for the actor:

```bash
uv run scripts/hydrate_memory.py clear \
  --region us-west-2 \
  --all-data
```

## Step 9: Let the agent save a long-term preference

Explicitly ask the assistant to remember a durable preference:

```bash
python3 app/invoke_eks.py \
  --region us-west-2 \
  --prompt "Remember for future conversations that I prefer a 15-year fixed-rate mortgage and prioritize paying the loan off early."
```

Expected result:

```text
The assistant confirms that it saved the preference.
```

The supervisor should invoke `add_memory`. The preference is embedded and
written under the current actor's partition.

Inspect the stored memory:

```bash
uv run app/inspect_memory.py \
  --region us-west-2 \
  --memories
```

If no memory appears, review the pod logs to confirm whether `add_memory` was
called and repeat the prompt with the explicit phrase `Remember for future
conversations`.

## Step 10: Test long-term memory in a new session

Create a new conversation for the same actor:

```bash
python3 app/invoke_eks.py \
  --region us-west-2 \
  --new-session \
  --prompt "What kind of mortgage do I prefer?"
```

Expected result:

```text
The assistant recalls the 15-year fixed-rate preference and early-payoff
priority.
```

The new session has no prior transcript. The answer comes from semantic
long-term memory.

## Step 11: Test semantic retrieval

Use different wording from the stored memory:

```bash
python3 app/invoke_eks.py \
  --region us-west-2 \
  --new-session \
  --prompt "Do you remember how quickly I wanted to repay my home loan?"
```

Expected result:

```text
The assistant connects the question with the remembered preference to pay the
loan off early.
```

You can query the vector index directly:

```bash
uv run app/inspect_memory.py \
  --region us-west-2 \
  --search "preferred repayment period"
```

DynamoDB vector indexes are eventually consistent. The inspection utility
retries for up to 60 seconds by default.

## Step 12: Test actor isolation

Select another actor and start a new session:

```bash
python3 app/invoke_eks.py \
  --region us-west-2 \
  --actor-id alternate-user \
  --new-session \
  --prompt "What mortgage preferences do you remember about me?"
```

Expected result:

```text
The assistant does not return the default participant's 15-year mortgage
preference.
```

Return to the default actor by omitting `--actor-id`:

```bash
python3 app/invoke_eks.py \
  --region us-west-2 \
  --new-session \
  --prompt "What mortgage term do I prefer?"
```

## Step 13: Inspect all memory records

```bash
uv run app/inspect_memory.py \
  --region us-west-2
```

The output separates:

- Short-term session records under `session/`.
- Long-term records under `memories/`.

Inspect an alternate actor:

```bash
uv run app/inspect_memory.py \
  --region us-west-2 \
  --actor-id alternate-user
```

## API contract

The memory-enabled API requires both identifiers:

```bash
curl --request POST "$MORTGAGE_API_URL/invoke" \
  --header "Authorization: Bearer $MORTGAGE_API_KEY" \
  --header "Content-Type: application/json" \
  --data '{
    "prompt": "What do you remember about my mortgage preference?",
    "actor_id": "participant-123456789012",
    "session_id": "session-example"
  }'
```

Response:

```json
{
  "request_id": "7a6b...",
  "actor_id": "participant-123456789012",
  "session_id": "session-example",
  "response": "...",
  "duration_ms": 2450
}
```

## Troubleshooting

### The shared vector table is unavailable

Lab 00 creates the table before participants begin the workshop. Confirm that
the setup environment installed boto3 1.43.64 or later:

```bash
uv run --project ../00-workshop-setup python -c \
  'import boto3; print(boto3.__version__)'
```

The setup module pins a compatible version. A workshop administrator can rerun
`00-workshop-setup/scripts/deploy-infrastructure.sh` to validate or repair the
pre-provisioned table.

### The vector index remains in CREATING

Index creation and backfill can take several minutes. Check:

```bash
aws dynamodb describe-table \
  --region us-west-2 \
  --table-name mortgage-assistant-workshop-memory \
  --query 'Table.VectorIndexes'
```

The index is ready when `IndexStatus` is `ACTIVE` and `Backfilling` is false.

### Short-term memory is not recalled

Display the client context:

```bash
python3 app/invoke_eks.py --show-context
```

Confirm both prompts used the same actor and session. Supplying
`--new-session` intentionally clears short-term conversational context.

### Long-term memory is not recalled

Check whether a memory was written:

```bash
uv run app/inspect_memory.py --memories
```

If it exists, retry the query because vector-index updates are eventually
consistent. If it does not exist, explicitly ask the assistant to
`Remember for future conversations`.

### The request receives HTTP 401

The client normally reads the API key from the Kubernetes Secret. If using
curl, retrieve it:

```bash
export MORTGAGE_API_KEY="$(
  kubectl get secret mortgage-assistant-api-key \
    --namespace mortgage-assistant \
    --output jsonpath='{.data.api-key}' |
  base64 --decode
)"
```

### Pods receive AccessDenied from DynamoDB

Confirm the Lab 00 stack contains the memory outputs and pod-role permissions:

```bash
aws cloudformation describe-stacks \
  --region us-west-2 \
  --stack-name mortgage-assistant-workshop

kubectl logs \
  --namespace mortgage-assistant \
  deployment/mortgage-assistant \
  --tail=200
```

## Production considerations

This lab intentionally keeps identity and operations simple. For a production
application:

- Derive actor IDs from authenticated identities.
- Enforce authorization before accepting an actor namespace.
- Define retention and deletion workflows for both sessions and memories.
- Add user-visible memory review and deletion controls.
- Treat prompt and session records as potentially sensitive data.
- Add monitoring for throttling, failed writes, vector-search latency, and
  Bedrock embedding errors.
- Review table partition size and traffic distribution.
- Consider S3 offload for session snapshots approaching DynamoDB's item-size
  limit.
- Test concurrent requests against the same session.

## Cleanup

To remove only Lab 04 resources:

```bash
./scripts/cleanup-memory.sh \
  --region us-west-2
```

This removes only the EKS application namespace and load balancer. The
DynamoDB memory table, vector index, IAM policy, KMS key, EKS cluster, and
Knowledge Base remain shared Lab 00 infrastructure.

To run Lab 03 again afterward:

```bash
cd ../03-eks-service
./scripts/deploy-application.sh --region us-west-2
```

To remove the entire workshop, use the root cleanup instructions. The Lab 00
cleanup script deletes the memory table before deleting the shared stack.

## Completion checkpoint

You have completed Lab 04 when:

- The API returns actor and session IDs.
- A follow-up prompt recalls information from the current session.
- The same session survives an EKS rollout restart.
- A new session recalls a stored mortgage preference.
- A paraphrased question retrieves the same preference.
- An alternate actor does not retrieve the default actor's memory.
- You can inspect both session and memory records in DynamoDB.
