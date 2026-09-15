# Lab 00: Workshop setup

This lab deploys the shared AWS resources used by all later labs:

- Amazon Bedrock Knowledge Base with an S3 data source.
- OpenSearch Serverless vector collection and index.
- Amazon EKS cluster and managed node group.
- Amazon ECR repository.
- EKS Pod Identity roles for the application and load balancer controller.
- AWS Load Balancer Controller.

From the repository root:

```bash
./00-workshop-setup/scripts/deploy-infrastructure.sh \
  --region us-west-2
```

The script uses the default AWS CLI profile unless `--profile` is provided.
It uploads the mortgage documents, waits for ingestion, configures `kubectl`,
and installs the load balancer controller. It does not deploy the application.

To remove all workshop resources:

```bash
./00-workshop-setup/scripts/cleanup.sh \
  --region us-west-2
```
