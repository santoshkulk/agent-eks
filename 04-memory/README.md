# Lab 04: Memory on Amazon EKS operator reference

This README is an operator reference for the Lab 4 checkpoint. For the guided deployment and the two required behavioral proofs, see the [Workshop Lab 4 page](../../build-and-deploy-agents-to-amazon-eks-with-amazon-bedrock/content/lab4-memory/).

Use only synthetic workshop data. This checkpoint is not a production mortgage, lending, identity, credit, or compliance system.

## Architecture

Each authenticated `POST /invoke` creates a fresh `mortgage_supervisor` and fresh specialists. The supervisor has stable `agent_id="supervisor"` and is the only agent with persistence:

- a latest-only, message-level session snapshot scoped to actor and session, with `MEMORY_SESSION_TTL_SECONDS` set to seven days by default;
- actor-scoped durable semantic memory for selected mortgage goals and preferences, with no TTL.

The three specialists are `Agent.as_tool` instances with their existing prompts, tools, stable IDs, and structured reports. They have no session or memory managers. Neither EKS replica owns conversation state.

A single DynamoDB table is partitioned by actor. The session snapshot is replaced as conversation advances; durable memory is separate from the transcript. The prompt-refresh hook reapplies the currently deployed supervisor prompt after snapshot restoration, and the result-cap hook bounds tool text retained in the snapshot.

Workshop Studio owns the shared EKS cluster, ECR repository, Knowledge Base, DynamoDB memory table, vector index, and Pod Identity permissions. This module updates only `mortgage-assistant`.

## Local validation

Run from `agent-eks/04-memory`:

```bash
uv run python -m unittest discover --start-directory tests --verbose
```

These tests use scripted models and test stores. They do not call AWS, Bedrock, EKS, or a deployed service.

## Deploy

Deployment changes charge-incurring participant resources. Confirm the account, Region, credentials, and intended workshop environment before running it.

```bash
./scripts/deploy-memory.sh --region us-west-2
```

The script reads canonical Parameter Store values, validates the shared memory table and vector index, builds and pushes a `linux/amd64` image unless an image is supplied, renders and applies participant manifests, waits for two ready replicas and the NLB, then sends an authenticated smoke invocation. It does not create or replace shared infrastructure.

| Option | Purpose |
| --- | --- |
| `--region REGION` | Select the AWS Region. |
| `--profile PROFILE` | Use a named local AWS profile. |
| `--service-access-cidr CIDR` | Restrict NLB access. The default is the caller's public IPv4 `/32`. |
| `--session-ttl-seconds N` | Set session retention. The minimum is 3600 seconds. |
| `--prompt TEXT` | Set the smoke-test prompt. |
| `--image-uri URI` | Reuse an existing image and skip the image build and push. |

The deployed workload preserves two replicas, rolling updates, probes, a PodDisruptionBudget, resource bounds, Pod Identity, disabled service-account token mounting, non-root execution, a read-only root filesystem, dropped capabilities, RuntimeDefault seccomp, bearer authentication, and source-restricted NLB access.

## Service and client contract

`GET /health` and `GET /health/ready` are unauthenticated probe endpoints. `POST /invoke` requires `Authorization: Bearer <key>` and accepts only:

```json
{
  "prompt": "What did I tell you?",
  "actor_id": "participant-123456789012",
  "session_id": "session-example"
}
```

`prompt` is 1 to 4,000 characters. Actor and session IDs are 1 to 128 characters and can contain letters, numbers, `.`, `_`, `:`, or `-`. A successful response contains a generated `request_id`, actor ID, session ID, response, and duration. The request ID is ephemeral correlation, not an idempotency key.

`app/invoke_eks.py` discovers the Service endpoint and API-key Secret unless `--url` and `--api-key` are supplied. It derives the default actor from the caller's AWS account and stores only the current session selection per actor in `.workshop/client-state.json`.

```bash
uv run app/invoke_eks.py --region us-west-2 --show-context
uv run app/invoke_eks.py --region us-west-2 \
  --prompt "I am considering a property worth 600,000 dollars."
```

## Facilitator utilities

`app/inspect_memory.py` lists session snapshots or durable memories for an actor and can run an actor-scoped semantic search. It is a facilitator diagnostic utility, not part of the guided participant proof.

```bash
uv run app/inspect_memory.py --region us-west-2 --sessions
uv run app/inspect_memory.py --region us-west-2 --memories
uv run app/inspect_memory.py --region us-west-2 \
  --search "preferred repayment period"
```

`scripts/hydrate_memory.py` seeds or clears synthetic durable preference records. `seed --replace` removes only `memories/` for the selected actor before writing the fixed synthetic examples. `clear --all-data` also removes that actor's session records and is intended only for explicit facilitator cleanup.

```bash
uv run scripts/hydrate_memory.py seed --region us-west-2 \
  --actor-id participant-123 --replace
```

Vector index updates are eventually consistent. The hydration and inspection utilities can wait up to 60 seconds for a search result.

## Security, ownership, and trade-offs

- Actor IDs are caller supplied and are not authorization. The shared bearer key also does not prove actor identity. A production system should derive identity from a verified token and authorize it.
- Send one request at a time for an actor and session. There is no cross-pod lease; concurrent same-session writes are last-writer-wins. A failed connection has an unknown outcome, and a retry is a new invocation.
- Durable-memory restrictions are prompt policy. Add deterministic validation, redaction, authorization, and retention controls before using non-synthetic data.
- The source-restricted NLB uses plaintext HTTP. Production systems should use TLS and an appropriate authenticated private or edge endpoint.
- Older operational records from earlier Lab 4 revisions are ignored. This checkpoint does not read, migrate, replay, or delete them.

Never add static AWS credentials to pods or create replacement shared resources.

## Cleanup boundary

`scripts/cleanup-memory.sh` removes only the `mortgage-assistant` Kubernetes namespace and load balancer. It does not remove the shared DynamoDB table, Knowledge Base, EKS cluster, ECR repository, Pod Identity resources, snapshots, or durable memories. Workshop Studio remains the owner of shared infrastructure.
