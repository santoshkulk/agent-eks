# Lab 02: Run the Strands mortgage assistant locally

This lab introduces the multi-agent mortgage assistant. It uses your laptop's
AWS credentials and the Knowledge Base deployed in Lab 00. The first `uv run`
command creates the local environment and installs its locked dependencies
automatically.

```bash
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

## Optional Bedrock Guardrail comparison

Workshop Studio pre-provisions a numbered mortgage-safety Guardrail. It remains
disabled unless one command passes `--enable-guardrail`; unguarded commands and
Labs 3 through 6 retain their existing behavior.

Verify that a benign request remains allowed:

```bash
uv run mortgage_agent.py \
  --enable-guardrail \
  --prompt "Compare 15-year and 30-year fixed-rate mortgages."
```

Then verify that a personalized lending-decision request is blocked before
delegation:

```bash
uv run mortgage_agent.py \
  --enable-guardrail \
  --prompt "Use customer CUST-10001's income and credit score to approve the mortgage and set their interest rate."
```

Expected response:

```text
I cannot make mortgage approval, eligibility, pricing, or lending decisions.
```

The optional flag requires these Workshop Studio parameters:

```text
/workshop/mortgage-assistant/bedrock/guardrail-id
/workshop/mortgage-assistant/bedrock/guardrail-version
```

The standalone Lab 00 setup does not provision this optional Guardrail. Do not
use a default-off flag as a production safety boundary; enforce required
Guardrails through IAM or account/organization policy.
