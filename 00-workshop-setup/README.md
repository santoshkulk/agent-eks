# Lab 00: Workshop setup

This lab deploys the shared AWS resources used by all later labs:

- Amazon Bedrock Knowledge Base with an S3 data source.
- OpenSearch Serverless vector collection and index.
- Amazon EKS cluster and managed node group.
- Amazon ECR repository.
- EKS Pod Identity roles for the application and load balancer controller.
- AWS Load Balancer Controller.
- Customer-managed AWS KMS key for agent memory.
- On-demand DynamoDB table with TTL and point-in-time recovery.
- DynamoDB vector index for semantic long-term memory.

From the repository root:

```bash
./00-workshop-setup/scripts/deploy-infrastructure.sh \
  --region us-west-2
```

The script uses the default AWS CLI profile unless `--profile` is provided.
It provisions the DynamoDB memory table, uploads the mortgage documents, waits
for ingestion, configures `kubectl`, and installs the load balancer controller.
It also publishes the `/workshop/mortgage-assistant/...` Parameter Store values that Labs 3 and later read (`eks/cluster-name`, `ecr/repository-uri`, `memory/table-name`, `memory/vector-index-name`, and, from the stack, `bedrock/knowledge-base-id`); `cleanup.sh` removes them. It does not deploy the mortgage application.

## Lab 5 and Lab 6 limitations

This standalone setup does not provision the self-hosted Langfuse stack used
by Lab 5. Lab 6 uses a pre-provisioned credit-score MCP server managed by the
credit-services team. This setup does not build or deploy the
`credit-score-mcp` workload in the `credit-services` namespace or publish
`/workshop/mortgage-assistant/mcp/credit-score-url`. Labs 5 and 6 require a
Workshop Studio-provisioned environment. Do not substitute an arbitrary MCP
endpoint or modify that MCP server to work around this limitation.

## If `kubectl` starts timing out

The script restricts the EKS API endpoint to the public IP of the machine that ran it
(`--public-access-cidr` overrides this). If your IP changes (VPN, new network),
`kubectl` fails with `dial tcp ...:443: i/o timeout`. Rerun the same
`deploy-infrastructure.sh` command: it detects the new address and updates the stack in
about two minutes.

To remove all workshop resources:

```bash
./00-workshop-setup/scripts/cleanup.sh \
  --region us-west-2
```
