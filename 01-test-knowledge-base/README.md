# Lab 01: Test the Bedrock Knowledge Base

This lab verifies document ingestion before an agent is introduced. The program
reads the Knowledge Base ID from SSM and calls the Bedrock `Retrieve` API. The
first `uv run` command creates the local environment and installs its locked
dependencies automatically.

```bash
uv run query_knowledge_base.py \
  --query "What are the benefits of a 15-year mortgage?"
```

Try another query:

```bash
uv run query_knowledge_base.py \
  --query "When does refinancing make sense?" \
  --number-of-results 5
```

Use `--json` to inspect complete scores, locations, and metadata.
