# Lab 02: Run the Strands mortgage assistant locally

This lab introduces the multi-agent mortgage assistant. It uses your laptop's
AWS credentials and the Knowledge Base deployed in Lab 00.

```bash
uv sync --frozen

uv run mortgage_agent.py \
  --prompt "What are the benefits of a 15-year mortgage?"
```

The default AWS CLI profile is used automatically. To use another profile:

```bash
export AWS_PROFILE=my-profile
export AWS_REGION=us-west-2

uv run mortgage_agent.py \
  --prompt "When does refinancing make sense?"
```
