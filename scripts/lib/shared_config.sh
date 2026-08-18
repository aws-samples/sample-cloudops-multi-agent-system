#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Shared project config helpers
#
# Sourced by scripts/lib/commands.sh. Do NOT execute directly.
#
# Terraform owns the actual storage (see terraform/modules/core/shared-config).
# This file provides the bash-side glue:
#   * read existing values from SSM (for prompt defaults on re-runs)
#   * write the user-typed values into terraform/config.auto.tfvars.json
#   * diff "what the user typed" vs. "what SSM has today" for reconfigure UX
#
# No `put` / `nuke` helpers — those go through `terraform apply` / `terraform
# destroy` on the shared-config module, which is itself invoked by cmd_configure.
# -----------------------------------------------------------------------------

# Terraform JSON file auto-loaded by `terraform apply`. Written by
# shared_config_write_tfvars; also read back by deploy.sh to populate shell
# env for scripts that still think in terms of env vars.
_SHARED_CONFIG_TFVARS_FILE="${TERRAFORM_DIR:-terraform}/config.auto.tfvars.json"

# macOS ships bash 3.2, which lacks `declare -A`. Emulate an associative
# array with a plain per-key variable naming convention: _SHARED_CFG__AWS_REGION
# holds the value for AWS_REGION. `shared_config_get` reads these; the
# importer writes them. No associative array needed.

# -----------------------------------------------------------------------------
# shared_config_prefix — build the SSM path prefix for this project+env.
# Callers expect $PROJECT_PREFIX + $ENVIRONMENT to be exported already.
# -----------------------------------------------------------------------------
shared_config_prefix() {
  echo "/${PROJECT_PREFIX}/${ENVIRONMENT}/config"
}

# -----------------------------------------------------------------------------
# shared_config_import_from_ssm — fill _SHARED_CFG from whatever's in SSM.
#
# Silent on first-run (no params exist yet) — leaves values as "" so prompts
# show blank defaults. Safe to call when the user hasn't yet deployed.
# -----------------------------------------------------------------------------
shared_config_import_from_ssm() {
  local prefix
  prefix="$(shared_config_prefix)"

  # SSM maps the space sentinel (" ") we write for empty values back to
  # " " here; strip that so the UI shows truly blank defaults.
  local json
  json=$(aws ssm get-parameters-by-path \
    --path "$prefix" \
    --recursive \
    --with-decryption \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION" \
    --output json 2>/dev/null || echo '{"Parameters":[]}')

  # Parse SSM output with python so we don't add jq as a new dep (already
  # required elsewhere, but keeps this module self-contained).
  while IFS=$'\t' read -r key value; do
    [ -z "$key" ] && continue
    [ "$value" = " " ] && value=""
    # bash 3.2-compatible emulation of an associative array: one dynamic
    # variable per key. The name sanitiser below mirrors the key
    # transformation used by the python heredoc above.
    local var="_SHARED_CFG__${key}"
    printf -v "$var" '%s' "$value"
  done < <(
    printf '%s' "$json" | .venv/bin/python -c "
import json, sys, os
prefix = '/' + os.environ['PROJECT_PREFIX'] + '/' + os.environ['ENVIRONMENT'] + '/config/'
data = json.load(sys.stdin)
for p in data.get('Parameters', []):
    name = p['Name']
    if not name.startswith(prefix):
        continue
    short = name[len(prefix):]
    # Translate 'idp/client_id' -> 'IDP_CLIENT_ID' etc.
    key = short.replace('/', '_').upper()
    print(f\"{key}\t{p.get('Value','')}\")
"
  )
}

# -----------------------------------------------------------------------------
# shared_config_get — read one value from _SHARED_CFG with a fallback.
#   $1  short key (e.g. AWS_REGION, IDP_TYPE, CROSS_ACCOUNT_DEFAULT_ROLE_ARN)
#   $2  fallback if the key isn't set or is empty
# -----------------------------------------------------------------------------
shared_config_get() {
  local key="$1" fallback="${2:-}"
  local var="_SHARED_CFG__${key}"
  local value="${!var:-}"
  [ -z "$value" ] && value="$fallback"
  echo "$value"
}

# -----------------------------------------------------------------------------
# shared_config_prompt — streamlined question helper.
#
# Usage:
#   shared_config_prompt VAR_NAME "Question to user" "default"
#
# Writes the answer into the variable VAR_NAME. An empty answer falls back
# to the default. The prompt shows the default in square brackets.
# -----------------------------------------------------------------------------
shared_config_prompt() {
  local __var="$1" __question="$2" __default="${3:-}"
  local __answer
  if [ -n "$__default" ]; then
    read -r -p "? $__question [$__default]: " __answer
  else
    read -r -p "? $__question: " __answer
  fi
  [ -z "$__answer" ] && __answer="$__default"
  printf -v "$__var" '%s' "$__answer"
}

# -----------------------------------------------------------------------------
# shared_config_prompt_yn — yes/no helper. Default is second arg (Y or N).
# -----------------------------------------------------------------------------
shared_config_prompt_yn() {
  local __var="$1" __question="$2" __default="${3:-N}"
  local __hint __answer
  if [ "$__default" = "Y" ] || [ "$__default" = "y" ]; then
    __hint="Y/n"
  else
    __hint="y/N"
  fi
  read -r -p "? $__question [$__hint]: " __answer
  [ -z "$__answer" ] && __answer="$__default"
  case "$__answer" in
    [Yy]*) printf -v "$__var" '%s' "true" ;;
    *)     printf -v "$__var" '%s' "false" ;;
  esac
}

# -----------------------------------------------------------------------------
# shared_config_get_tfvars — read one scalar key from config.auto.tfvars.json.
# Returns empty string if the file or key is absent. Cheaper than re-reading
# from SSM when the post-deploy hook just wants to know whether a value has
# already been written.
# -----------------------------------------------------------------------------
shared_config_get_tfvars() {
  local key="$1"
  local file="$_SHARED_CONFIG_TFVARS_FILE"
  [ -f "$file" ] || { echo ""; return 0; }
  .venv/bin/python - "$file" "$key" <<'PY'
import json, sys
path, key = sys.argv[1], sys.argv[2]
try:
    with open(path) as f:
        data = json.load(f)
except Exception:
    print("")
    sys.exit(0)
val = data.get(key, "")
# Emit strings and numbers (e.g. log_retention_days is an int); skip
# lists/dicts/bools which aren't scalar config values this helper serves.
if isinstance(val, str):
    print(val)
elif isinstance(val, (int, float)) and not isinstance(val, bool):
    print(val)
else:
    print("")
PY
}

# -----------------------------------------------------------------------------
# shared_config_set_value — update one scalar key in config.auto.tfvars.json.
#
# Used by post-deploy steps (APP_URL after CloudFront is up, MEMORY_ID after
# memory is created, etc.) to write a single derived value into the shared
# config without running the full interactive prompt flow. Caller is
# responsible for running `shared_config_apply` afterwards if the change
# needs to land in SSM before the next full apply.
#   $1  Terraform variable name (e.g. "app_url")
#   $2  value (string)
# -----------------------------------------------------------------------------
shared_config_set_value() {
  local key="$1" value="$2"
  local file="$_SHARED_CONFIG_TFVARS_FILE"
  .venv/bin/python - "$file" "$key" "$value" <<'PY'
import json, sys
path, key, value = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path) as f:
        data = json.load(f)
except FileNotFoundError:
    data = {}
data[key] = value
with open(path, "w") as f:
    json.dump(data, f, indent=2, sort_keys=True)
PY
}

# -----------------------------------------------------------------------------
# shared_config_write_tfvars — serialize answers into config.auto.tfvars.json.
#
# Takes the ANSWERS associative array (populated by commands.sh during the
# prompt loop) and writes a Terraform-auto-loaded JSON file. Only keys that
# were explicitly prompted for get written; unmentioned keys are left as
# whatever they already were (so gated-out tools don't clobber their stored
# config).
# -----------------------------------------------------------------------------
shared_config_write_tfvars() {
  # Callers must have populated the prompt-answer variables via
  # _answers_set and exported them as ANS_* (see commands.sh).
  local existing_tfvars="{}"
  if [ -f "$_SHARED_CONFIG_TFVARS_FILE" ]; then
    existing_tfvars=$(cat "$_SHARED_CONFIG_TFVARS_FILE")
  fi

  # Merge strategy: existing JSON is the baseline; ANSWERS overlays it.
  # tool_env_vars is a nested object that needs merging at the tool level.
  .venv/bin/python - "$_SHARED_CONFIG_TFVARS_FILE" <<PY
import json, os, sys

existing = json.loads('''$existing_tfvars''')
answers = {}
# Pass ANSWERS through the environment so we don't have to escape JSON
# through the heredoc twice.
for k, v in os.environ.items():
    if k.startswith("ANS_"):
        answers[k[4:]] = v

# Top-level scalars where the answer-key name matches the tf-var name (lower case).
for tf_key in (
    "aws_region", "idp_type", "custom_idp_issuer_url", "custom_idp_client_id",
    "custom_idp_client_secret", "app_url", "gateway_auth",
):
    if tf_key.upper() in answers:
        existing[tf_key] = answers[tf_key.upper()]

# Top-level scalars where the answer-key matches the imported SSM key shape
# rather than the tf-var name. We map explicitly so the diff helper can find
# them but the JSON still uses the tf-var key Terraform expects.
_ANSWER_TO_TFVAR = {
    "MODEL_DEFAULT_ID":                      "bedrock_model_id",
    "MODEL_HEALTH_ENRICHMENT_ID":            "health_enrichment_model_id",
    "CROSS_ACCOUNT_HEALTH_ROLE_ARN":         "health_events_cross_account_role_arn",
    # Imported from SSM as GATEWAY_JWT_VALIDATION_CLAIM (path gateway/jwt_validation_claim);
    # Terraform expects the root var name jwt_validation_claim.
    "GATEWAY_JWT_VALIDATION_CLAIM":          "jwt_validation_claim",
}
for ans_key, tf_key in _ANSWER_TO_TFVAR.items():
    if ans_key in answers:
        existing[tf_key] = answers[ans_key]

# log_retention_days is numeric — write it into the tfvars as an int so the
# tfvar (used by the Terraform-managed log groups) and SSM (read by the
# deploy.sh runtime-group sweep) always carry the SAME value. Without this,
# changing retention via SSM would only move the auto-created runtime groups
# while the Terraform-managed groups stayed at the var default — silent drift.
if "OBSERVABILITY_LOG_RETENTION_DAYS" in answers:
    try:
        existing["log_retention_days"] = int(answers["OBSERVABILITY_LOG_RETENTION_DAYS"])
    except (ValueError, TypeError):
        pass

# network_resilience_cross_account_role_arns is a list at the root; we serialize
# it as CSV in the prompt answer and split here so the JSON has the right shape.
if "CROSS_ACCOUNT_NETWORK_RESILIENCE_ROLE_ARNS" in answers:
    val = answers["CROSS_ACCOUNT_NETWORK_RESILIENCE_ROLE_ARNS"].strip()
    existing["network_resilience_cross_account_role_arns"] = (
        [a.strip() for a in val.split(",") if a.strip()] if val else []
    )

# selected_agents / selected_tools — CSV when user picked "custom", omitted
# entirely when they picked "all". Empty list IS NOT the same as "all" in
# main.tf (contains(var.selected_agents, k) is false for []), so we must
# drop the key from JSON so generate_tfvars' full-list default wins via
# terraform.tfvars. If the key already existed in the JSON from a prior
# custom run, remove it on switch to "all".
for tf_key, ans_key in (("selected_tools", "SELECTED_TOOLS"),
                        ("selected_agents", "SELECTED_AGENTS")):
    if ans_key not in answers:
        continue
    val = answers[ans_key].strip()
    if val:
        existing[tf_key] = [t.strip() for t in val.split(",") if t.strip()]
    else:
        existing.pop(tf_key, None)

# tool_env_vars — nested: merge per-tool
tev = existing.get("tool_env_vars", {}) or {}
def set_tool_var(tool, env_key, answer_key):
    if answer_key in answers and answers[answer_key]:
        tev.setdefault(tool, {})[env_key] = answers[answer_key]
set_tool_var("cost-explorer", "CROSS_ACCOUNT_ROLE_ARN", "CROSS_ACCOUNT_ROLE_ARN")
set_tool_var("cost-optimization-hub", "CROSS_ACCOUNT_ROLE_ARN_COH", "CROSS_ACCOUNT_ROLE_ARN_COH")
set_tool_var("tag-governance", "CROSS_ACCOUNT_ROLE_ARN_TAG_GOVERNANCE", "CROSS_ACCOUNT_ROLE_ARN_TAG_GOVERNANCE")
set_tool_var("cloudwatch", "CROSS_ACCOUNT_ROLE_ARN_CLOUDWATCH", "CROSS_ACCOUNT_ROLE_ARN_CLOUDWATCH")
set_tool_var("cur-athena", "CUR_DATABASE_NAME", "CUR_DATABASE_NAME")
set_tool_var("cur-athena", "CUR_TABLE_NAME", "CUR_TABLE_NAME")
set_tool_var("cur-athena", "ATHENA_WORKGROUP", "ATHENA_WORKGROUP")
set_tool_var("cur-athena", "ATHENA_OUTPUT_LOCATION", "ATHENA_OUTPUT_LOCATION")
if tev:
    existing["tool_env_vars"] = tev

with open(sys.argv[1], "w") as f:
    json.dump(existing, f, indent=2, sort_keys=True)
PY
}

# -----------------------------------------------------------------------------
# shared_config_apply — run the targeted terraform apply for shared_config.
#
# Fast (~5 seconds) because it's scoped to a single module. The main deploy
# later re-applies everything including shared_config, but this targeted run
# lets `make configure` show the user their changes landed without waiting
# for a full deploy.
# -----------------------------------------------------------------------------
shared_config_apply() {
  # First invocation after a fresh clone: the state backend (S3 + DynamoDB)
  # doesn't exist yet, and `.terraform/` hasn't been initialised. Both helpers
  # are idempotent no-ops on subsequent runs.
  bootstrap_state_backend
  ensure_terraform_init

  # Per-invocation CLI overrides (e.g. `AWS_REGION=... make configure`)
  # reach this targeted apply via `-var` flags. See load_tf_overrides in
  # common.sh for the precedence rationale.
  local _TF_OVERRIDES
  load_tf_overrides

  # `make configure` runs BEFORE generate_tfvars() (full deploy is what writes
  # terraform/terraform.tfvars). On a cold-start clone the file doesn't exist
  # yet, so the required `s3_bucket` / `dynamodb_table` / `project_tag` /
  # `environment_tag` variables have nothing to read from. Pass them through
  # as `-var=` flags from the shell env (which deploy.sh has already populated
  # from .env + identity defaults).
  local _CFG_BOOTSTRAP_VARS=(
    "-var=s3_bucket=${S3_BUCKET}"
    "-var=dynamodb_table=${DYNAMODB_TABLE}"
    "-var=project_tag=${PROJECT_PREFIX}"
    "-var=environment_tag=${ENVIRONMENT}"
  )

  info "Applying shared-config module (writes to SSM)..."
  (
    cd "$TERRAFORM_DIR" && \
    terraform apply -target=module.shared_config -auto-approve -compact-warnings \
      "${_CFG_BOOTSTRAP_VARS[@]}" \
      "${_TF_OVERRIDES[@]+"${_TF_OVERRIDES[@]}"}"
  ) || die "terraform apply -target=module.shared_config failed"
}

# -----------------------------------------------------------------------------
# shared_config_print_summary — one-line provenance banner.
#
# Counts populated vs total parameters under /$PROJECT/$ENV/config and prints
# the SSM prefix so devs can quickly see which deployment they're operating
# on. Designed to print once per `make` entrypoint (run_terraform / apply
# wrappers). Silent (and zero-exit) when SSM is unreachable — first-run,
# missing creds, etc. shouldn't fail the deploy.
# -----------------------------------------------------------------------------
shared_config_print_summary() {
  local prefix
  prefix="$(shared_config_prefix)"

  local json
  json=$(aws ssm get-parameters-by-path \
    --path "$prefix" \
    --recursive \
    --profile "${AWS_PROFILE:-default}" \
    --region "${AWS_REGION:-us-east-1}" \
    --output json 2>/dev/null) || return 0

  local stats
  stats=$(printf '%s' "$json" | .venv/bin/python -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
params = data.get("Parameters", []) or []
total = len(params)
populated = sum(1 for p in params if (p.get("Value") or " ").strip() != "")
print(f"{populated} {total}")
' 2>/dev/null)
  [ -z "$stats" ] && return 0
  local populated total
  populated=$(echo "$stats" | awk '{print $1}')
  total=$(echo "$stats" | awk '{print $2}')
  [ "$total" = "0" ] && return 0
  info "Shared config: ${populated}/${total} values active (source: SSM ${prefix})"
}

# -----------------------------------------------------------------------------
# shared_config_diff — prints "changed / unchanged / new" summary.
#
# Compares what the user just typed (ANSWERS) to what SSM has right now
# (_SHARED_CFG, populated earlier by shared_config_import_from_ssm). Used by
# reconfigure-shared to show the user exactly what's about to change before
# they type APPLY CHANGES.
# -----------------------------------------------------------------------------
shared_config_diff() {
  local key value existing changed=0 var
  echo
  echo "Changes to apply:"
  echo "-----------------"
  for key in "${_ANSWER_KEYS[@]+"${_ANSWER_KEYS[@]}"}"; do
    value="$(_answers_get "$key")"
    var="_SHARED_CFG__${key}"
    existing="${!var:-}"
    if [ "$existing" != "$value" ]; then
      # Mask secrets in the diff output. Use a case statement because bash
      # 3.2 (macOS /bin/bash) chokes on `[[ =~ A|B ]]` alternation under
      # `set -eu` and spews a syntax-error warning.
      local display_existing="$existing" display_new="$value"
      case "$key" in
        *SECRET*|*PASSWORD*)
          display_existing=$([ -n "$existing" ] && echo "(set)" || echo "(unset)")
          display_new=$([ -n "$value" ] && echo "(set)" || echo "(unset)")
          ;;
      esac
      printf "  %-40s %s -> %s\n" "$key" "${display_existing:-(unset)}" "${display_new:-(unset)}"
      changed=$((changed + 1))
    fi
  done
  if [ "$changed" -eq 0 ]; then
    echo "  (no changes)"
  fi
  echo
  return "$changed"
}
