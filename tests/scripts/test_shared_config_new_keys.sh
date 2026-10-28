#!/usr/bin/env bash
# Verify that the five new shared-config keys round-trip cleanly through
# shared_config_set_value + shared_config_write_tfvars without clobbering
# existing tool_env_vars or tripping the json serializer.
#
# Run from project root:
#   bash tests/scripts/test_shared_config_new_keys.sh

set -u

TESTS_RUN=0
TESTS_FAILED=0

assert_eq() {
  local actual="$1" expected="$2" name="$3"
  TESTS_RUN=$((TESTS_RUN + 1))
  if [ "$actual" = "$expected" ]; then
    printf "  PASS  %s\n" "$name"
  else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    printf "  FAIL  %s\n        expected: %q\n        actual:   %q\n" \
      "$name" "$expected" "$actual"
  fi
}

PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$PROJECT_ROOT"

# Sandbox: copy minimal repo, run shared_config helpers, verify resulting JSON.
SANDBOX=$(mktemp -d)
trap 'rm -rf "$SANDBOX"' EXIT

mkdir -p "$SANDBOX/repo/scripts/lib" "$SANDBOX/repo/.venv/bin" "$SANDBOX/repo/terraform"
cp "$PROJECT_ROOT/scripts/lib"/{common,shared_config,auth,commands}.sh "$SANDBOX/repo/scripts/lib/"
cp "$PROJECT_ROOT/scripts/shared-keys.txt" "$SANDBOX/repo/scripts/"
ln -sf "$PROJECT_ROOT/.venv/bin/python" "$SANDBOX/repo/.venv/bin/python"

# Pre-existing tfvars file with tool_env_vars — we want to confirm the new
# top-level keys are added without clobbering this nested object.
cat > "$SANDBOX/repo/terraform/config.auto.tfvars.json" <<'EOF'
{
  "aws_region": "us-east-1",
  "idp_type": "cognito",
  "tool_env_vars": {
    "cost-explorer": {
      "CROSS_ACCOUNT_ROLE_ARN": "arn:aws:iam::222222222222:role/CEReader"
    }
  }
}
EOF

cd "$SANDBOX/repo"
export PROJECT_PREFIX=cloudops
export ENVIRONMENT=dev
export AWS_PROFILE=test
export AWS_REGION=us-east-1
export TERRAFORM_DIR=terraform

# shellcheck disable=SC1091
source scripts/lib/common.sh
# shellcheck disable=SC1091
source scripts/lib/shared_config.sh

# -----------------------------------------------------------------------------
# Test 1: shared_config_set_value writes top-level scalars without disturbing
# tool_env_vars
# -----------------------------------------------------------------------------
shared_config_set_value memory_id "MEMORY-12345"
shared_config_set_value bedrock_model_id "global.anthropic.claude-sonnet-4-6-v1"
shared_config_set_value health_enrichment_model_id "global.anthropic.claude-haiku-4-5-20251001-v1:0"
shared_config_set_value health_events_cross_account_role_arn "arn:aws:iam::333333333333:role/HealthBackfill"

# Verify with python.
RESULT=$(.venv/bin/python - <<'PY'
import json
data = json.load(open("terraform/config.auto.tfvars.json"))
print(data.get("memory_id", ""))
print(data.get("bedrock_model_id", ""))
print(data.get("health_enrichment_model_id", ""))
print(data.get("health_events_cross_account_role_arn", ""))
print(json.dumps(data.get("tool_env_vars", {}), sort_keys=True))
PY
)
LINE_MEM=$(echo "$RESULT" | sed -n '1p')
LINE_BED=$(echo "$RESULT" | sed -n '2p')
LINE_HEN=$(echo "$RESULT" | sed -n '3p')
LINE_HRO=$(echo "$RESULT" | sed -n '4p')
LINE_TEV=$(echo "$RESULT" | sed -n '5p')

assert_eq "$LINE_MEM" "MEMORY-12345" "shared_config_set_value writes memory_id"
assert_eq "$LINE_BED" "global.anthropic.claude-sonnet-4-6-v1" "shared_config_set_value writes bedrock_model_id"
assert_eq "$LINE_HEN" "global.anthropic.claude-haiku-4-5-20251001-v1:0" "shared_config_set_value writes health_enrichment_model_id"
assert_eq "$LINE_HRO" "arn:aws:iam::333333333333:role/HealthBackfill" "shared_config_set_value writes health_events_cross_account_role_arn"
assert_eq "$LINE_TEV" '{"cost-explorer": {"CROSS_ACCOUNT_ROLE_ARN": "arn:aws:iam::222222222222:role/CEReader"}}' "tool_env_vars survives intact"

# -----------------------------------------------------------------------------
# Test 2: shared_config_write_tfvars consumes ANS_* env vars + writes the new
# top-level keys, including the list-typed network_resilience_cross_account_role_arns
# -----------------------------------------------------------------------------
ANS_AWS_REGION="us-west-2"
ANS_IDP_TYPE="cognito"
ANS_MODEL_DEFAULT_ID="global.anthropic.claude-opus-4-6-v1"
ANS_MODEL_HEALTH_ENRICHMENT_ID="global.anthropic.claude-haiku-4-5-20251001-v1:0"
ANS_CROSS_ACCOUNT_HEALTH_ROLE_ARN="arn:aws:iam::444444444444:role/Health2"
ANS_CROSS_ACCOUNT_NETWORK_RESILIENCE_ROLE_ARNS="arn:aws:iam::555555555555:role/NRA, arn:aws:iam::666666666666:role/NRB"
ANS_DEVOPS_AGENT_SPACE_ID="space-123"
ANS_DEVOPS_AGENT_SPACE_REGION="ap-southeast-1"
ANS_DEVOPS_AGENT_INTEGRATION_ENABLED="true"
ANS_DEVOPS_AGENT_HEALTH_AUTOMATIC_ENABLED="true"
ANS_DEVOPS_AGENT_MAX_CONCURRENCY="2"
ANS_DEVOPS_AGENT_AUTOMATIC_DAILY_BUDGET="10"
ANS_DEVOPS_AGENT_MAX_AGE_MINUTES="120"
ANS_DEVOPS_AGENT_SWEEP_INTERVAL_MINUTES="1"
ANS_DEVOPS_AGENT_COVERAGE_CACHE_TTL_SECONDS="300"

export ANS_AWS_REGION ANS_IDP_TYPE ANS_MODEL_DEFAULT_ID
export ANS_MODEL_HEALTH_ENRICHMENT_ID ANS_CROSS_ACCOUNT_HEALTH_ROLE_ARN
export ANS_CROSS_ACCOUNT_NETWORK_RESILIENCE_ROLE_ARNS
export ANS_DEVOPS_AGENT_SPACE_ID ANS_DEVOPS_AGENT_SPACE_REGION
export ANS_DEVOPS_AGENT_INTEGRATION_ENABLED
export ANS_DEVOPS_AGENT_HEALTH_AUTOMATIC_ENABLED
export ANS_DEVOPS_AGENT_MAX_CONCURRENCY
export ANS_DEVOPS_AGENT_AUTOMATIC_DAILY_BUDGET
export ANS_DEVOPS_AGENT_MAX_AGE_MINUTES
export ANS_DEVOPS_AGENT_SWEEP_INTERVAL_MINUTES
export ANS_DEVOPS_AGENT_COVERAGE_CACHE_TTL_SECONDS

shared_config_write_tfvars

RESULT=$(.venv/bin/python - <<'PY'
import json
d = json.load(open("terraform/config.auto.tfvars.json"))
print(d.get("aws_region", ""))
print(d.get("bedrock_model_id", ""))
print(d.get("health_enrichment_model_id", ""))
print(d.get("health_events_cross_account_role_arn", ""))
print(json.dumps(d.get("network_resilience_cross_account_role_arns", []), sort_keys=True))
print(json.dumps(d.get("tool_env_vars", {}), sort_keys=True))
print(d.get("devops_agent_space_id", ""))
print(d.get("devops_agent_space_region", ""))
print(d.get("devops_agent_integration_enabled"))
print(d.get("devops_agent_health_automatic_enabled"))
print(d.get("devops_agent_max_concurrency"))
print(d.get("devops_agent_automatic_daily_budget"))
print(d.get("devops_agent_max_age_minutes"))
print(d.get("devops_agent_sweep_interval_minutes"))
print(d.get("devops_agent_coverage_cache_ttl_seconds"))
PY
)
assert_eq "$(echo "$RESULT" | sed -n '1p')" "us-west-2" "write_tfvars writes aws_region"
assert_eq "$(echo "$RESULT" | sed -n '2p')" "global.anthropic.claude-opus-4-6-v1" "write_tfvars writes bedrock_model_id"
assert_eq "$(echo "$RESULT" | sed -n '3p')" "global.anthropic.claude-haiku-4-5-20251001-v1:0" "write_tfvars writes health_enrichment_model_id"
assert_eq "$(echo "$RESULT" | sed -n '4p')" "arn:aws:iam::444444444444:role/Health2" "write_tfvars writes health_events_cross_account_role_arn"
assert_eq "$(echo "$RESULT" | sed -n '5p')" '["arn:aws:iam::555555555555:role/NRA", "arn:aws:iam::666666666666:role/NRB"]' "write_tfvars splits CSV into list"
assert_eq "$(echo "$RESULT" | sed -n '6p')" '{"cost-explorer": {"CROSS_ACCOUNT_ROLE_ARN": "arn:aws:iam::222222222222:role/CEReader"}}' "write_tfvars preserves tool_env_vars"
assert_eq "$(echo "$RESULT" | sed -n '7p')" "space-123" "write_tfvars writes Agent Space ID"
assert_eq "$(echo "$RESULT" | sed -n '8p')" "ap-southeast-1" "write_tfvars writes Agent Space Region"
assert_eq "$(echo "$RESULT" | sed -n '9p')" "True" "write_tfvars writes integration bool"
assert_eq "$(echo "$RESULT" | sed -n '10p')" "True" "write_tfvars writes automatic bool"
assert_eq "$(echo "$RESULT" | sed -n '11p')" "2" "write_tfvars writes concurrency number"
assert_eq "$(echo "$RESULT" | sed -n '12p')" "10" "write_tfvars writes budget number"
assert_eq "$(echo "$RESULT" | sed -n '13p')" "120" "write_tfvars writes max age number"
assert_eq "$(echo "$RESULT" | sed -n '14p')" "1" "write_tfvars writes sweep number"
assert_eq "$(echo "$RESULT" | sed -n '15p')" "300" "write_tfvars writes coverage TTL number"

# -----------------------------------------------------------------------------
# Test 3: empty network_resilience answer produces an empty list (not omitted)
# -----------------------------------------------------------------------------
unset ANS_CROSS_ACCOUNT_NETWORK_RESILIENCE_ROLE_ARNS
ANS_CROSS_ACCOUNT_NETWORK_RESILIENCE_ROLE_ARNS=""
export ANS_CROSS_ACCOUNT_NETWORK_RESILIENCE_ROLE_ARNS

shared_config_write_tfvars

RESULT=$(.venv/bin/python - <<'PY'
import json
d = json.load(open("terraform/config.auto.tfvars.json"))
print(json.dumps(d.get("network_resilience_cross_account_role_arns", "MISSING"), sort_keys=True))
PY
)
assert_eq "$(echo "$RESULT" | sed -n '1p')" "[]" "empty CSV produces empty list"

# -----------------------------------------------------------------------------
# Test 4: the payer roles reach the commitments Lambda, not only cost-explorer
# and cost-optimization-hub. Terraform reads Lambda env (and the scoped
# sts:AssumeRole grant) only from tool_env_vars, so without this the tool
# silently analyzes the ops account in Mode B.
# -----------------------------------------------------------------------------
ANS_CROSS_ACCOUNT_ROLE_ARN="arn:aws:iam::777777777777:role/PayerReader"
ANS_CROSS_ACCOUNT_ROLE_ARN_COH="arn:aws:iam::888888888888:role/COHReader"
export ANS_CROSS_ACCOUNT_ROLE_ARN ANS_CROSS_ACCOUNT_ROLE_ARN_COH

shared_config_write_tfvars

RESULT=$(.venv/bin/python - <<'PY'
import json
tev = json.load(open("terraform/config.auto.tfvars.json")).get("tool_env_vars", {})
print(json.dumps(tev.get("commitments", {}), sort_keys=True))
print(tev.get("cost-explorer", {}).get("CROSS_ACCOUNT_ROLE_ARN", ""))
PY
)
assert_eq "$(echo "$RESULT" | sed -n '1p')" '{"CROSS_ACCOUNT_ROLE_ARN": "arn:aws:iam::777777777777:role/PayerReader", "CROSS_ACCOUNT_ROLE_ARN_COH": "arn:aws:iam::888888888888:role/COHReader"}' "commitments receives both payer roles"
assert_eq "$(echo "$RESULT" | sed -n '2p')" "arn:aws:iam::777777777777:role/PayerReader" "cost-explorer still receives the payer role"

echo
echo "----------------------------------------"
echo "Ran $TESTS_RUN tests, $TESTS_FAILED failed."
echo "----------------------------------------"
exit "$TESTS_FAILED"
