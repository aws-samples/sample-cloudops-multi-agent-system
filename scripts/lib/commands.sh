#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Interactive commands — setup / configure / reconfigure-shared
#
# Sourced by scripts/deploy.sh when the Makefile dispatches to them.
# DO NOT execute directly.
#
# Design principles:
#   * Ask as few questions as possible. Group prompts into logical sections.
#   * Use contextual phrasing ("Does this agent analyse costs in another
#     account?") rather than raw env var names ("CROSS_ACCOUNT_ROLE_ARN").
#   * Re-running is idempotent. Existing SSM values become the defaults;
#     pressing enter leaves them unchanged.
#   * Tool-specific prompts are skipped entirely if the gating tool isn't
#     in the active DEPLOY_TOOLS selection.
# -----------------------------------------------------------------------------

# macOS ships bash 3.2 (no `declare -A`). We emulate an associative array
# for the prompt answers using:
#   * _ANSWER_KEYS — plain indexed array of keys set during the session.
#   * Per-key variables named _ANSWER__<KEY> holding the value.
# Helpers below wrap this so callers don't see the bookkeeping.
_ANSWER_KEYS=()

_answers_set() {
  local key="$1" value="$2" var="_ANSWER__${1}"
  # Track in _ANSWER_KEYS on first set. Must avoid `${!var+set}` under
  # `set -u` because the indirect expansion triggers an unbound-var error
  # on unset names. Instead, check the keys array directly.
  local already_tracked=0 k
  for k in "${_ANSWER_KEYS[@]+"${_ANSWER_KEYS[@]}"}"; do
    if [ "$k" = "$key" ]; then already_tracked=1; break; fi
  done
  printf -v "$var" '%s' "$value"
  if [ "$already_tracked" = 0 ]; then
    _ANSWER_KEYS+=("$key")
  fi
}

_answers_get() {
  local var="_ANSWER__${1}"
  echo "${!var:-}"
}

_answers_reset() {
  local key var
  for key in "${_ANSWER_KEYS[@]+"${_ANSWER_KEYS[@]}"}"; do
    var="_ANSWER__${key}"
    unset "$var"
  done
  _ANSWER_KEYS=()
}

# -----------------------------------------------------------------------------
# cmd_setup — identity-only, no SSM writes. Writes .env + installs Python deps.
#
# Called as `make setup`. This is the first thing a new developer runs after
# cloning. It prompts only for things that are PER-USER (AWS profile, their
# PROJECT_PREFIX / ENVIRONMENT choice — which determines the SSM path they
# read from). Shared config comes later via `make configure`.
# -----------------------------------------------------------------------------
cmd_setup() {
  info "CloudOps local setup"
  echo
  echo "  Writes .env with your local identity (AWS profile, project name)."
  echo "  Shared project config (regions, cross-account roles, etc.) is not"
  echo "  prompted here — that lives in SSM and is set via 'make configure'."
  echo

  local project_prefix environment
  shared_config_prompt project_prefix "Project prefix (resources named \${prefix}-\${env}-*)" "${PROJECT_PREFIX:-cloudops}"
  shared_config_prompt environment    "Environment (dev/staging/prod)"                       "${ENVIRONMENT:-dev}"
  save_env_var PROJECT_PREFIX "$project_prefix"
  save_env_var ENVIRONMENT    "$environment"

  # Make project prefix / environment visible to the rest of cmd_setup so the
  # auth + xacct paths below can read them via the live shell.
  export PROJECT_PREFIX="$project_prefix"
  export ENVIRONMENT="$environment"

  # ---------------------------------------------------------------------------
  # Auth method — sso (default, recommended) or credentials.
  # ---------------------------------------------------------------------------
  local auth_method
  while true; do
    shared_config_prompt auth_method "Auth method (sso/credentials)" "${AUTH_METHOD:-sso}"
    case "$auth_method" in
      sso|credentials) break ;;
      *) echo "  Error: must be 'sso' or 'credentials'." ;;
    esac
  done
  save_env_var AUTH_METHOD "$auth_method"
  export AUTH_METHOD="$auth_method"

  # AWS profile name is derived deterministically from project + env so SSO
  # config stays self-consistent. The user can still customise their profile
  # by changing PROJECT_PREFIX / ENVIRONMENT.
  local aws_profile="${PROJECT_PREFIX}-${ENVIRONMENT}"
  save_env_var AWS_PROFILE "$aws_profile"
  export AWS_PROFILE="$aws_profile"
  info "AWS profile: $aws_profile (derived from \${PROJECT_PREFIX}-\${ENVIRONMENT})"

  if [ "$auth_method" = "sso" ]; then
    _cmd_setup_sso_branch "$aws_profile"
  else
    _cmd_setup_credentials_branch "$aws_profile"
  fi

  info "Wrote .env"

  # Migration: older .env files pre-date the SSM/JSON split and often carry
  # shared-config keys. Those keys must not live in .env — they'd silently
  # shadow the authoritative config.auto.tfvars.json. Offer to strip them.
  _cmd_setup_migrate_stale_env

  # Optional cross-account sub-profile setup (only meaningful for SSO + after
  # `make configure` has populated the role ARNs in SSM). Silent no-op
  # otherwise.
  if [ "$auth_method" = "sso" ]; then
    _cmd_setup_cross_account_sso
  fi

  # Virtualenv + deps (preserves the prior make setup behaviour).
  # Prefer pyenv Python 3.12 over system python3 (macOS ships 3.9 whose pip
  # is too old for PEP 660 editable installs from pyproject.toml).
  if [ ! -d .venv ]; then
    info "Creating Python virtualenv..."
    local _py3=""
    for _candidate in "$HOME/.pyenv/versions"/3.12.*/bin/python3; do
      [ -x "$_candidate" ] && _py3="$_candidate" && break
    done
    if [ -z "$_py3" ]; then
      _py3="$(command -v python3)"
    fi
    "$_py3" -m venv .venv
    .venv/bin/pip install --upgrade pip setuptools wheel --quiet
  fi
  info "Installing Python dev dependencies..."
  .venv/bin/pip install -e ".[dev]" --quiet

  echo
  info "Setup complete."
  echo
  echo "  Next steps:"
  echo "    1. make configure      # set shared project config (stored in SSM)"
  echo "    2. make deploy-auto    # deploy the stack"
  if [ "$auth_method" = "sso" ]; then
    echo
    echo "  Tip: cross-account sub-profiles for tools like cost-explorer / COH"
    echo "  are auto-generated AFTER you've run 'make configure' (they read the"
    echo "  role ARNs from SSM). Re-run 'make setup' once configure is done to"
    echo "  generate them."
  fi
}

# -----------------------------------------------------------------------------
# _cmd_setup_sso_branch — interactive IAM Identity Center setup.
#
# Prompts for the per-developer SSO identity values (start URL, region,
# account, permission set), writes ~/.aws/config blocks, then runs `aws sso
# login` and validates with sts:GetCallerIdentity. SSO_SESSION_NAME is derived
# from PROJECT_PREFIX so all developers on a deployment share a session name.
# -----------------------------------------------------------------------------
_cmd_setup_sso_branch() {
  local aws_profile="$1"

  echo
  echo "=== SSO setup ==="
  echo

  local sso_session_name="${PROJECT_PREFIX}-sso"
  local sso_start_url sso_region sso_account_id sso_role_name aws_region

  shared_config_prompt sso_start_url   "SSO start URL"                 "${SSO_START_URL:-}"
  shared_config_prompt sso_region      "SSO region (Identity Center)"  "${SSO_REGION:-${AWS_REGION:-us-east-1}}"
  shared_config_prompt sso_account_id  "AWS account ID for deployment" "${SSO_ACCOUNT_ID:-}"
  shared_config_prompt sso_role_name   "Permission set / role name"    "${SSO_ROLE_NAME:-}"
  shared_config_prompt aws_region      "Default AWS region"            "${AWS_REGION:-us-east-1}"

  save_env_var SSO_SESSION_NAME "$sso_session_name"
  save_env_var SSO_START_URL    "$sso_start_url"
  save_env_var SSO_REGION       "$sso_region"
  save_env_var SSO_ACCOUNT_ID   "$sso_account_id"
  save_env_var SSO_ROLE_NAME    "$sso_role_name"

  export SSO_SESSION_NAME="$sso_session_name"
  export AWS_REGION="$aws_region"

  setup_sso_profile "$sso_session_name" "$aws_profile" "$sso_start_url" \
    "$sso_region" "$sso_account_id" "$sso_role_name" "$aws_region"

  info "Launching browser to log into SSO session '$sso_session_name'..."
  if aws sso login --sso-session "$sso_session_name"; then
    if aws sts get-caller-identity --profile "$aws_profile" >/dev/null 2>&1; then
      local identity
      identity=$(aws sts get-caller-identity --profile "$aws_profile" --output text --query 'Arn' 2>/dev/null || echo "unknown")
      info "Validated — authenticated as: $identity"
    else
      warn "Login completed but sts:GetCallerIdentity still failing. Re-check the values above."
    fi
  else
    warn "aws sso login failed. You can re-run 'aws sso login --sso-session $sso_session_name' later."
  fi
}

# -----------------------------------------------------------------------------
# _cmd_setup_credentials_branch — long-lived access-key fallback.
#
# Preserves the pre-SSO behaviour: assume the user has already configured
# their profile out of band (or wants to enter keys interactively now).
# -----------------------------------------------------------------------------
_cmd_setup_credentials_branch() {
  local aws_profile="$1"
  local aws_region

  echo
  echo "=== Credentials setup ==="
  echo
  shared_config_prompt aws_region "Default AWS region" "${AWS_REGION:-us-east-1}"
  export AWS_REGION="$aws_region"

  if aws sts get-caller-identity --profile "$aws_profile" >/dev/null 2>&1; then
    info "Profile '$aws_profile' already has working credentials — leaving ~/.aws/credentials untouched."
    return 0
  fi

  local configure_yn
  shared_config_prompt_yn configure_yn "Profile '$aws_profile' is not configured. Enter access keys now?" "Y"
  if [ "$configure_yn" = "true" ]; then
    setup_access_key_profile "$aws_profile" "$aws_region"
  else
    warn "Skipped — you'll need to run 'aws configure --profile $aws_profile' before deploying."
  fi
}

# -----------------------------------------------------------------------------
# _cmd_setup_cross_account_sso — generate per-target SSO sub-profiles.
#
# Iterates the auth.sh target catalogue (ce/coh/tag-gov/health) and configures
# a [profile $PROJECT_PREFIX-$suffix] entry for each one whose role ARN is
# already populated in SSM. Silent no-op when SSM is empty (first run before
# `make configure`).
# -----------------------------------------------------------------------------
_cmd_setup_cross_account_sso() {
  # Need SSM populated to know which accounts to derive profiles for.
  shared_config_import_from_ssm 2>/dev/null || return 0

  local has_any=0
  local line suffix ssm_subkey env_field label
  while IFS=':' read -r suffix ssm_subkey env_field label; do
    [ -z "$suffix" ] && continue
    local key
    key="$(echo "$ssm_subkey" | tr '[:lower:]/' '[:upper:]_')"
    local arn
    arn="$(shared_config_get "$key" "")"
    [ -z "$arn" ] && continue
    if [ "$has_any" = 0 ]; then
      echo
      echo "=== Cross-account SSO sub-profiles ==="
      echo "  Generating one [profile] per cross-account target whose role ARN is in SSM."
      echo "  Sub-profiles reuse session '$SSO_SESSION_NAME' — single browser login covers all."
      echo
      has_any=1
    fi
    configure_cross_account_target "$suffix" "$ssm_subkey" "$env_field" "$label"
  done < <(auth_sso_cross_account_targets)

  if [ "$has_any" = 0 ]; then
    info "No cross-account role ARNs in SSM yet — sub-profile setup deferred until after 'make configure'."
  fi
}

# -----------------------------------------------------------------------------
# _cmd_setup_migrate_stale_env — strip SSM-owned keys from legacy .env files.
#
# Pre-SSM versions of the project put AWS_REGION, IDP_TYPE, APP_URL, etc.
# directly in .env. Now those live in terraform/config.auto.tfvars.json (and
# SSM). If they linger in .env, they'll be sourced into the shell and beat
# the JSON auto-loader — which is exactly the Bug 1 scenario this fix is for.
# -----------------------------------------------------------------------------
_cmd_setup_migrate_stale_env() {
  [ -f "$ENV_FILE" ] || return 0

  local stale_keys
  stale_keys=$(load_shared_keys)
  local found=()
  local k
  for k in $stale_keys; do
    if grep -q "^${k}=" "$ENV_FILE" 2>/dev/null; then
      found+=("$k")
    fi
  done

  [ "${#found[@]}" -eq 0 ] && return 0

  # Special-case: MEMORY_ID. If .env has it but SSM does not, push it into the
  # tfvars file before the strip prompt so the user doesn't lose the binding.
  # This stops a fresh `make deploy-auto` from creating a NEW memory and orphaning
  # the existing one. SSM is the new source of truth for memory_id; .env was
  # the old one.
  for k in "${found[@]}"; do
    if [ "$k" = "MEMORY_ID" ]; then
      local existing_mem
      existing_mem=$(grep "^MEMORY_ID=" "$ENV_FILE" | head -1 | cut -d= -f2-)
      if [ -n "$existing_mem" ]; then
        local current_ssm; current_ssm="$(shared_config_get MEMORY_ID "")"
        if [ -z "$current_ssm" ]; then
          info "Migrating MEMORY_ID from .env into shared config (tfvars/SSM)..."
          shared_config_set_value memory_id "$existing_mem"
          info "  -> shared-config will be applied during 'make configure' / next deploy."
        fi
      fi
      break
    fi
  done

  echo
  warn "Your .env contains shared-config keys that now live in SSM:"
  for k in "${found[@]}"; do
    printf "    %s=%s\n" "$k" "$(grep "^${k}=" "$ENV_FILE" | head -1 | cut -d= -f2-)"
  done
  echo
  echo "  Keeping these in .env will silently override the SSM/JSON config."
  echo "  Recommended: remove them from .env so 'make configure' / 'make reconfigure-shared'"
  echo "  is the single source of truth. You can always set one as a PER-INVOCATION"
  echo "  override on the command line, e.g. 'AWS_REGION=eu-west-1 make deploy-auto'."
  echo
  local resp
  shared_config_prompt resp "Strip these keys from .env now?" "Y"
  case "$resp" in
    [Yy]*|"")
      for k in "${found[@]}"; do
        remove_env_var "$k"
      done
      info "Stripped ${#found[@]} stale key(s) from .env"
      ;;
    *)
      warn "Leaving .env untouched. Expect silent override until you remove them manually."
      ;;
  esac
}

# -----------------------------------------------------------------------------
# cmd_configure — first-run interactive config of SHARED settings.
#
# Called as `make configure`. Writes to terraform/config.auto.tfvars.json and
# runs a targeted terraform apply on the shared-config module so values land
# in SSM for other devs. Subsequent runs pre-fill prompts from the existing
# SSM values (idempotent: pressing enter through everything does nothing).
# -----------------------------------------------------------------------------
cmd_configure() {
  info "CloudOps shared configuration"
  echo
  echo "  Writing to SSM at /$PROJECT_PREFIX/$ENVIRONMENT/config/*"
  echo "  Per-invocation env vars (e.g. DEPLOY_TOOLS=x make deploy-auto)"
  echo "  still override these values for a single deploy."
  echo

  # Pre-load existing SSM values so prompts show the current team defaults.
  info "Reading current config from SSM..."
  shared_config_import_from_ssm

  _configure_prompts "first-run"
  _configure_write_and_apply
}

# -----------------------------------------------------------------------------
# cmd_reconfigure_shared — change flow with diff + APPLY CHANGES gate.
#
# Same prompt flow as cmd_configure, but after the prompts we show a diff of
# what's about to change and require the literal phrase "APPLY CHANGES" to
# proceed. On confirm, writes SSM + triggers a redeploy of affected stacks.
# -----------------------------------------------------------------------------
cmd_reconfigure_shared() {
  info "CloudOps shared config — reconfigure"
  echo

  info "Reading current config from SSM..."
  shared_config_import_from_ssm

  _configure_prompts "reconfigure"

  if shared_config_diff; then
    info "No changes — nothing to do."
    return 0
  fi

  echo
  warn "Type 'APPLY CHANGES' (exact match, case-sensitive) to confirm, or anything else to cancel."
  local confirm
  read -r -p "> " confirm
  if [ "$confirm" != "APPLY CHANGES" ]; then
    info "Cancelled — no writes."
    return 0
  fi

  _configure_write_and_apply

  echo
  info "Config applied. Affected stacks will redeploy on the next 'make deploy-auto'."
  info "Tip: run 'make deploy-auto' now to pick up the changes."
}

# -----------------------------------------------------------------------------
# cmd_nuke_shared_config — wipe every SSM parameter under
# /$PROJECT_PREFIX/$ENVIRONMENT/config and tear down the shared-config Terraform
# module. Typed PROJECT_PREFIX confirmation required. Useful when state has
# drifted from SSM and you want a clean slate to re-run `make configure` against.
#
# Pairs SSM nuke with `terraform destroy -target=module.shared_config` so the
# next `terraform apply` doesn't immediately recreate the parameters from state.
# -----------------------------------------------------------------------------
cmd_nuke_shared_config() {
  warn "About to delete EVERY SSM parameter under /$PROJECT_PREFIX/$ENVIRONMENT/config"
  warn "AND destroy the shared-config Terraform module. This is not recoverable."
  echo
  warn "Type the project prefix '$PROJECT_PREFIX' (exact match) to confirm, or anything else to cancel."
  local confirm
  read -r -p "> " confirm
  if [ "$confirm" != "$PROJECT_PREFIX" ]; then
    info "Cancelled — no writes."
    return 0
  fi

  local prefix
  prefix="$(shared_config_prefix)"

  info "Listing parameters under $prefix..."
  local names_csv
  names_csv=$(aws ssm get-parameters-by-path \
    --path "$prefix" \
    --recursive \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION" \
    --query 'Parameters[].Name' \
    --output text 2>/dev/null || echo "")

  if [ -n "$names_csv" ]; then
    # delete-parameters caps at 10 per call — feed in groups of 10 via xargs.
    echo "$names_csv" | tr '\t' '\n' | xargs -n 10 aws ssm delete-parameters \
      --profile "$AWS_PROFILE" \
      --region "$AWS_REGION" \
      --names >/dev/null
    info "Deleted SSM parameters under $prefix"
  else
    info "No SSM parameters under $prefix to delete."
  fi

  info "Destroying Terraform shared-config module..."
  bootstrap_state_backend
  ensure_terraform_init
  local _TF_OVERRIDES
  load_tf_overrides
  # Same bootstrap-vars rationale as shared_config_apply — terraform.tfvars may
  # be absent if the user is nuking before having run a full deploy.
  local _CFG_BOOTSTRAP_VARS=(
    "-var=s3_bucket=${S3_BUCKET}"
    "-var=dynamodb_table=${DYNAMODB_TABLE}"
    "-var=project_tag=${PROJECT_PREFIX}"
    "-var=environment_tag=${ENVIRONMENT}"
  )
  (
    cd "$TERRAFORM_DIR" && \
    terraform destroy -target=module.shared_config -auto-approve -compact-warnings \
      "${_CFG_BOOTSTRAP_VARS[@]}" \
      "${_TF_OVERRIDES[@]+"${_TF_OVERRIDES[@]}"}"
  ) || warn "terraform destroy -target=module.shared_config returned non-zero (may be acceptable if module already absent)"

  # Drop the local tfvars JSON too — otherwise the next `make configure` will
  # think the values are still there and skip re-prompting.
  if [ -f "$_SHARED_CONFIG_TFVARS_FILE" ]; then
    rm -f "$_SHARED_CONFIG_TFVARS_FILE"
    info "Removed local $_SHARED_CONFIG_TFVARS_FILE"
  fi

  info "Done. Re-run 'make configure' to seed shared config from scratch."
}

# -----------------------------------------------------------------------------
# _configure_prompts — the shared prompt flow used by both configure and
# reconfigure. Populates the ANSWERS associative array based on the user's
# selections. Skips tool-specific sections that aren't in play.
# -----------------------------------------------------------------------------
_configure_prompts() {
  local mode="$1"  # "first-run" or "reconfigure"

  # -------------------------------------------------------------------------
  # DEPLOYMENT SHAPE — which agents and tools are in scope.
  # -------------------------------------------------------------------------
  echo
  echo "=== Deployment shape ==="
  echo

  local agents_mode tools_mode custom_agents custom_tools
  # Default agents_mode based on existing SSM: if deploy/agents is empty → "all".
  local existing_agents; existing_agents="$(shared_config_get DEPLOY_AGENTS "")"
  local existing_tools;  existing_tools="$(shared_config_get DEPLOY_TOOLS "")"

  if [ -n "$existing_agents" ]; then
    shared_config_prompt agents_mode "Deploy [all] agents or [custom] selection?" "custom"
  else
    shared_config_prompt agents_mode "Deploy [all] agents or [custom] selection?" "all"
  fi
  if [ "$agents_mode" = "custom" ]; then
    echo "   Available: supervisor, finops-agent, governance-agent, ops-excellence-agent,"
    echo "              cost-operations-agent, pricing-agent, health-events-agent,"
    echo "              network-resiliency-agent, tag-governance-agent"
    shared_config_prompt custom_agents "Agents (comma-separated)" "$existing_agents"
    _answers_set SELECTED_AGENTS "$custom_agents"
  else
    _answers_set SELECTED_AGENTS ""
  fi

  if [ -n "$existing_tools" ]; then
    shared_config_prompt tools_mode "Deploy [all] tools or [custom] selection?" "custom"
  else
    shared_config_prompt tools_mode "Deploy [all] tools or [custom] selection?" "all"
  fi
  if [ "$tools_mode" = "custom" ]; then
    echo "   Available: cost-explorer, cur-athena, cost-optimization-hub,"
    echo "              health-events, billing, pricing, network-resilience, tag-governance"
    shared_config_prompt custom_tools "Tools (comma-separated)" "$existing_tools"
    _answers_set SELECTED_TOOLS "$custom_tools"
  else
    _answers_set SELECTED_TOOLS ""
  fi

  # Normalise for gating checks below. An empty SELECTED_TOOLS means "all
  # tools selected" — mirror that by setting the gate variable to the full
  # list.
  local active_tools; active_tools="$(_answers_get SELECTED_TOOLS)"
  if [ -z "$active_tools" ]; then
    active_tools="cost-explorer,cur-athena,cost-optimization-hub,health-events,billing,pricing,network-resilience,tag-governance"
  fi

  # -------------------------------------------------------------------------
  # IDENTITY PROVIDER — cognito is the default.
  # -------------------------------------------------------------------------
  echo
  echo "=== Identity provider ==="
  echo

  local idp_type
  shared_config_prompt idp_type "Identity provider (cognito/custom)" "$(shared_config_get IDP_TYPE cognito)"
  _answers_set IDP_TYPE "$idp_type"

  if [ "$idp_type" = "custom" ]; then
    local issuer client_id client_secret
    shared_config_prompt issuer        "OIDC issuer URL"    "$(shared_config_get IDP_ISSUER_URL "")"
    shared_config_prompt client_id     "OIDC client id"     "$(shared_config_get IDP_CLIENT_ID "")"
    shared_config_prompt client_secret "OIDC client secret (stored as SecureString)" "$(shared_config_get IDP_CLIENT_SECRET "")"
    _answers_set CUSTOM_IDP_ISSUER_URL "$issuer"
    _answers_set CUSTOM_IDP_CLIENT_ID "$client_id"
    _answers_set CUSTOM_IDP_CLIENT_SECRET "$client_secret"
  fi

  # -------------------------------------------------------------------------
  # REGIONS & OBSERVABILITY — one prompt each.
  # -------------------------------------------------------------------------
  echo
  echo "=== Regions & observability ==="
  echo

  local aws_region log_retention gateway_auth
  shared_config_prompt aws_region    "AWS region"        "$(shared_config_get AWS_REGION us-east-1)"
  shared_config_prompt log_retention "Log retention days" "$(shared_config_get OBSERVABILITY_LOG_RETENTION_DAYS 30)"
  shared_config_prompt gateway_auth  "Gateway auth (iam/oauth)" "$(shared_config_get GATEWAY_AUTH iam)"
  _answers_set AWS_REGION "$aws_region"
  _answers_set OBSERVABILITY_LOG_RETENTION_DAYS "$log_retention"
  _answers_set GATEWAY_AUTH "$gateway_auth"

  # JWT claim is only meaningful for the oauth authorizer. Ask only when oauth
  # is selected; otherwise leave it untouched so iam/full deployments never see
  # the prompt and the stored value (if any) is preserved. Default "client" is
  # the Quick-friendly choice (access tokens carry client_id, not aud); AG-UI/
  # ID-token callers should pick "audience".
  if [ "$gateway_auth" = "oauth" ]; then
    local jwt_validation_claim
    shared_config_prompt jwt_validation_claim \
      "JWT validation claim (client=Quick access tokens / audience=AG-UI ID tokens)" \
      "$(shared_config_get GATEWAY_JWT_VALIDATION_CLAIM client)"
    _answers_set GATEWAY_JWT_VALIDATION_CLAIM "$jwt_validation_claim"
  fi

  # -------------------------------------------------------------------------
  # MODELS — shared deployment policy. Per-agent overrides in hierarchy.json
  # still win at runtime; this just sets the default.
  # -------------------------------------------------------------------------
  echo
  echo "=== Models ==="
  echo

  # Answer keys match the imported SSM key names so shared_config_diff can
  # find them (it indexes _SHARED_CFG__<KEY>). SSM stores model/default_id
  # and model/health_enrichment_id, which import as MODEL_DEFAULT_ID and
  # MODEL_HEALTH_ENRICHMENT_ID. The downstream tfvars writer maps these to
  # the Terraform variable names (bedrock_model_id, health_enrichment_model_id).
  local bedrock_model health_enrichment_model
  shared_config_prompt bedrock_model           "Default Bedrock model ID for sub-agents (empty = code default)" \
    "$(shared_config_get MODEL_DEFAULT_ID "")"
  shared_config_prompt health_enrichment_model "Bedrock model ID for health-events enrichment (empty = disable)" \
    "$(shared_config_get MODEL_HEALTH_ENRICHMENT_ID "global.anthropic.claude-haiku-4-5-20251001-v1:0")"
  _answers_set MODEL_DEFAULT_ID "$bedrock_model"
  _answers_set MODEL_HEALTH_ENRICHMENT_ID "$health_enrichment_model"

  # -------------------------------------------------------------------------
  # TOOL-SPECIFIC CONFIG — conditional on selections above.
  # -------------------------------------------------------------------------
  local need_ce_xacct need_coh_xacct need_cur need_health_xacct need_netres_xacct need_cw_xacct
  case ",$active_tools," in
    *,cost-explorer,*)          need_ce_xacct=1 ;;
    *)                          need_ce_xacct=0 ;;
  esac
  case ",$active_tools," in
    *,cost-optimization-hub,*)  need_coh_xacct=1 ;;
    *)                          need_coh_xacct=0 ;;
  esac
  case ",$active_tools," in
    *,cur-athena,*)             need_cur=1 ;;
    *)                          need_cur=0 ;;
  esac
  case ",$active_tools," in
    *,health-events,*)          need_health_xacct=1 ;;
    *)                          need_health_xacct=0 ;;
  esac
  case ",$active_tools," in
    *,network-resilience,*)     need_netres_xacct=1 ;;
    *)                          need_netres_xacct=0 ;;
  esac
  case ",$active_tools," in
    *,cloudwatch,*)             need_cw_xacct=1 ;;
    *)                          need_cw_xacct=0 ;;
  esac

  if [ "$need_ce_xacct" = 1 ] || [ "$need_coh_xacct" = 1 ] || [ "$need_cur" = 1 ] \
     || [ "$need_health_xacct" = 1 ] || [ "$need_netres_xacct" = 1 ] \
     || [ "$need_cw_xacct" = 1 ]; then
    echo
    echo "=== Tool-specific config ==="
    echo
  fi

  if [ "$need_ce_xacct" = 1 ]; then
    local ce_yn ce_arn
    local current_ce; current_ce="$(shared_config_get CROSS_ACCOUNT_DEFAULT_ROLE_ARN "")"
    local default_yn; [ -n "$current_ce" ] && default_yn="Y" || default_yn="N"
    shared_config_prompt_yn ce_yn "cost-explorer: analyse costs in a different account (e.g. payer)?" "$default_yn"
    if [ "$ce_yn" = "true" ]; then
      shared_config_prompt ce_arn "  Cross-account role ARN for cost-explorer" "$current_ce"
      _answers_set CROSS_ACCOUNT_ROLE_ARN "$ce_arn"
    else
      _answers_set CROSS_ACCOUNT_ROLE_ARN ""
    fi
  fi

  if [ "$need_coh_xacct" = 1 ]; then
    local coh_yn coh_arn
    local current_coh; current_coh="$(shared_config_get CROSS_ACCOUNT_COH_ROLE_ARN "")"
    local default_yn; [ -n "$current_coh" ] && default_yn="Y" || default_yn="N"
    shared_config_prompt_yn coh_yn "cost-optimization-hub: delegated-admin account for COH recommendations?" "$default_yn"
    if [ "$coh_yn" = "true" ]; then
      shared_config_prompt coh_arn "  Cross-account role ARN for COH" "$current_coh"
      _answers_set CROSS_ACCOUNT_ROLE_ARN_COH "$coh_arn"
    else
      _answers_set CROSS_ACCOUNT_ROLE_ARN_COH ""
    fi
  fi

  if [ "$need_cur" = 1 ]; then
    echo "  cur-athena needs a Glue database + table and an Athena workgroup."
    local cur_db cur_table athena_wg athena_out
    shared_config_prompt cur_db      "  Glue database"          "$(shared_config_get CUR_DATABASE_NAME "${PROJECT_PREFIX}_cur_db")"
    shared_config_prompt cur_table   "  Glue table name"        "$(shared_config_get CUR_TABLE_NAME cur)"
    shared_config_prompt athena_wg   "  Athena workgroup"       "$(shared_config_get CUR_ATHENA_WORKGROUP "${PROJECT_PREFIX}-cur")"
    shared_config_prompt athena_out  "  Athena S3 output URI"   "$(shared_config_get CUR_ATHENA_OUTPUT_LOCATION "")"
    _answers_set CUR_DATABASE_NAME "$cur_db"
    _answers_set CUR_TABLE_NAME "$cur_table"
    _answers_set ATHENA_WORKGROUP "$athena_wg"
    _answers_set ATHENA_OUTPUT_LOCATION "$athena_out"
  fi

  if [ "$need_health_xacct" = 1 ]; then
    local health_yn health_arn
    local current_health; current_health="$(shared_config_get CROSS_ACCOUNT_HEALTH_ROLE_ARN "")"
    local default_yn; [ -n "$current_health" ] && default_yn="Y" || default_yn="N"
    shared_config_prompt_yn health_yn "health-events: assume a cross-account role for AWS Health org-view APIs?" "$default_yn"
    if [ "$health_yn" = "true" ]; then
      shared_config_prompt health_arn "  Cross-account role ARN for health-events" "$current_health"
      _answers_set CROSS_ACCOUNT_HEALTH_ROLE_ARN "$health_arn"
    else
      _answers_set CROSS_ACCOUNT_HEALTH_ROLE_ARN ""
    fi
  fi

  if [ "$need_netres_xacct" = 1 ]; then
    local netres_yn netres_arns
    local current_netres; current_netres="$(shared_config_get CROSS_ACCOUNT_NETWORK_RESILIENCE_ROLE_ARNS "")"
    local default_yn; [ -n "$current_netres" ] && default_yn="Y" || default_yn="N"
    shared_config_prompt_yn netres_yn "network-resilience: enrichment via spoke-account roles?" "$default_yn"
    if [ "$netres_yn" = "true" ]; then
      shared_config_prompt netres_arns "  Spoke-account role ARNs (comma-separated)" "$current_netres"
      _answers_set CROSS_ACCOUNT_NETWORK_RESILIENCE_ROLE_ARNS "$netres_arns"
    else
      _answers_set CROSS_ACCOUNT_NETWORK_RESILIENCE_ROLE_ARNS ""
    fi
  fi

  if [ "$need_cw_xacct" = 1 ]; then
    local cw_yn cw_arn
    local current_cw; current_cw="$(shared_config_get CROSS_ACCOUNT_CLOUDWATCH_ROLE_ARN "")"
    local default_yn; [ -n "$current_cw" ] && default_yn="Y" || default_yn="N"
    shared_config_prompt_yn cw_yn "cloudwatch: read metrics/alarms from a different account (e.g. spoke)?" "$default_yn"
    if [ "$cw_yn" = "true" ]; then
      shared_config_prompt cw_arn "  Cross-account role ARN for cloudwatch" "$current_cw"
      _answers_set CROSS_ACCOUNT_ROLE_ARN_CLOUDWATCH "$cw_arn"
    else
      _answers_set CROSS_ACCOUNT_ROLE_ARN_CLOUDWATCH ""
    fi
  fi
}

# -----------------------------------------------------------------------------
# _configure_write_and_apply — serialize ANSWERS into config.auto.tfvars.json
# then run a targeted terraform apply so SSM is updated immediately.
# -----------------------------------------------------------------------------
_configure_write_and_apply() {
  info "Writing $_SHARED_CONFIG_TFVARS_FILE"

  # Export answers into environment as ANS_* so the python heredoc in
  # shared_config_write_tfvars can see them.
  local k v
  for k in "${_ANSWER_KEYS[@]+"${_ANSWER_KEYS[@]}"}"; do
    v="$(_answers_get "$k")"
    export "ANS_${k}=${v}"
  done

  shared_config_write_tfvars

  # Unset them so we don't leak into subsequent shell state.
  for k in "${_ANSWER_KEYS[@]+"${_ANSWER_KEYS[@]}"}"; do
    unset "ANS_${k}"
  done
  _answers_reset

  shared_config_apply
  info "Done."
}
