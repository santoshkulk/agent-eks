#!/usr/bin/env bash
set -euo pipefail

MODULE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}"
PROFILE=""
URL=""

usage() {
  cat <<'EOT'
Usage: 05-observability/05b-cloudwatch-omni/scripts/test-agent.sh [options]

Sends a short series of prompts to the deployed mortgage assistant and prints
the trace ID of each request. Look the trace IDs up in CloudWatch Omni about
five minutes later.

Requests:
  1. A general mortgage question (Knowledge Base specialist).
  2. An existing-mortgage lookup (account tool).
  3. A two-turn conversation in one session (session memory).

Options:
  --region REGION     AWS Region (default: us-west-2).
  --profile PROFILE   AWS CLI profile; omit to use the default profile.
  --url URL           API base URL; defaults to the load balancer of
                      mortgage-assistant-omni.
  -h, --help          Show this help.
EOT
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region) REGION="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --url) URL="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

for command_name in uv python3; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Required command not found: $command_name" >&2
    exit 1
  fi
done

CLIENT_OPTIONS=(--region "$REGION" --state-file "$MODULE_DIR/.workshop/omni-test-state.json")
if [[ -n "$PROFILE" ]]; then
  CLIENT_OPTIONS+=(--profile "$PROFILE")
fi
if [[ -n "$URL" ]]; then
  CLIENT_OPTIONS+=(--url "$URL")
fi

RUN_ID="$(date -u +%Y%m%d%H%M%S)"
RESULTS=()

# send LABEL SESSION_ID PROMPT
send() {
  local label="$1" session="$2" prompt="$3" output trace_id
  echo
  echo "== $label"
  echo "   Prompt:  $prompt"
  if ! output="$(cd "$MODULE_DIR" && uv run --quiet app/invoke_eks.py \
    "${CLIENT_OPTIONS[@]}" --session-id "$session" --prompt "$prompt" --json)"; then
    echo "   Request failed. Check the pod logs:" >&2
    echo "   kubectl logs -n mortgage-assistant deployment/mortgage-assistant-omni" >&2
    exit 1
  fi
  # The client prints Actor/Session lines before the JSON body.
  trace_id="$(python3 -c '
import json, sys
text = sys.stdin.read()
body = json.loads(text[text.index("{"):])
print("Response: " + str(body.get("response", ""))[:300].replace("\n", " "), file=sys.stderr)
print(body.get("trace_id") or "")
' <<<"$output")"
  RESULTS+=("$label|$session|${trace_id:-none}")
}

send "General question" "omni-test-$RUN_ID-general" \
  "What are the benefits of a 15-year mortgage?"
send "Existing mortgage lookup" "omni-test-$RUN_ID-account" \
  "What is the outstanding principal on account 555000111?"
send "Conversation, turn 1" "omni-test-$RUN_ID-memory" \
  "I am considering a property worth 600,000 dollars."
send "Conversation, turn 2" "omni-test-$RUN_ID-memory" \
  "What property value did I mention in this conversation?"

echo
echo "Trace IDs"
printf '  %-26s %-40s %s\n' "Request" "Session" "Trace ID"
for row in "${RESULTS[@]}"; do
  IFS='|' read -r label session trace_id <<<"$row"
  printf '  %-26s %-40s %s\n' "$label" "$session" "$trace_id"
done

OMNI_DOMAIN_URL="$(aws ${PROFILE:+--profile "$PROFILE"} --region "$REGION" ssm get-parameter \
  --name /workshop/mortgage-assistant/cloudwatch/omni-domain-url \
  --query Parameter.Value --output text 2>/dev/null || true)"

cat <<EOT

Open CloudWatch Omni in about five minutes and search for these trace IDs:
  ${OMNI_DOMAIN_URL:-<see /workshop/mortgage-assistant/cloudwatch/omni-domain-url>}
The two "Conversation" requests share one session ID.
EOT
