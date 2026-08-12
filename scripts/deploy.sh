#!/usr/bin/env bash
set -eu

# Suppress BrokenPipeError from Python-based CLI tools (e.g., aws CLI v2)
export PYTHONDONTWRITEBYTECODE=1
trap '' PIPE 2>/dev/null || true

# Ensure we run from the project root
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# ---------------------------------------------------------------------------
# Configuration defaults (override via .env or environment variables)
# ---------------------------------------------------------------------------
TERRAFORM_DIR="terraform"
LAMBDA_TOOLS_DIR="src/lambda/mcp"
DATA_COLLECTION_DIR="src/lambda/collectors"
FRONTEND_DIR="src/frontend"
HASH_DIR=".lambda-hashes"
FRONTEND_CHANGED=false

# ---------------------------------------------------------------------------
# Config precedence (highest wins):
#   1. CLI env override:   VAR=value make deploy-auto
#   2. SSM-backed JSON:    terraform/config.auto.tfvars.json
#   3. deploy.sh defaults  (applied only if all above are unset)
#
# `.env` is treated as PER-DEV IDENTITY ONLY: AWS_PROFILE, PROJECT_PREFIX,
# ENVIRONMENT, MEMORY_ID. Any SSM-owned keys that happen to be in a user's
# .env (from pre-SSM-migration days) are deliberately stripped below so they
# don't silently shadow the authoritative JSON values.
# ---------------------------------------------------------------------------
# Load the canonical shared-key list from scripts/shared-keys.txt. We can't
# use the `load_shared_keys` helper in common.sh yet — lib scripts get sourced
# at line ~89, and this block runs before that so CLI overrides can be
# captured before .env is loaded.
_SHARED_KEYS=$(grep -vE '^[[:space:]]*(#|$)' "$SCRIPT_DIR/shared-keys.txt" | tr '\n' ' ')

# Capture CLI-set values BEFORE sourcing .env. Bash 3.2 (macOS /bin/bash)
# has no associative arrays, so use one plain variable per key.
for _k in $_SHARED_KEYS; do
  eval "_CLI_${_k}=\"\${$_k:-}\""
done

# Source .env if present. This may overwrite the shared-key values captured
# above — that's handled in the restore-or-strip loop below.
if [ -f .env ]; then
  set -a; source .env; set +a
fi

# For each shared key: if the user supplied it as a CLI override, restore
# the captured value. Otherwise unset it so the JSON auto-loader further
# down can fill it from config.auto.tfvars.json. Stale .env values never
# win for SSM-owned keys.
for _k in $_SHARED_KEYS; do
  _cli_var="_CLI_${_k}"
  if [ -n "${!_cli_var:-}" ]; then
    eval "export $_k=\"\${$_cli_var}\""
  else
    unset "$_k"
  fi
done
unset _k _cli_var

# Per-dev identity defaults (from .env).
PROJECT_PREFIX="${PROJECT_PREFIX:-cloudops}"
ENVIRONMENT="${ENVIRONMENT:-dev}"

# State backend naming — derived from identity.
S3_BUCKET="${S3_BUCKET:-${PROJECT_PREFIX}-tf-state-${ENVIRONMENT}}"
DYNAMODB_TABLE="${DYNAMODB_TABLE:-${PROJECT_PREFIX}-tf-lock-${ENVIRONMENT}}"

# Post-deploy auto-populated.
MEMORY_ID="${MEMORY_ID:-}"

# Flags
DEPLOY_ALL=false
AUTO_APPROVE=false
PLAN_ONLY=false
DESTROY=false
DESTROY_ALL=false
BUILD_AGENTS_ONLY=false

# Selected modules
SELECTED_AGENTS=()
SELECTED_TOOLS=()
SELECTED_DATA=()

# ---------------------------------------------------------------------------
# Source sub-scripts
# ---------------------------------------------------------------------------
source "$SCRIPT_DIR/lib/common.sh"
source "$SCRIPT_DIR/lib/hierarchy.sh"
source "$SCRIPT_DIR/lib/config.sh"
source "$SCRIPT_DIR/lib/build.sh"
source "$SCRIPT_DIR/lib/terraform.sh"
source "$SCRIPT_DIR/lib/sync.sh"
source "$SCRIPT_DIR/lib/teardown.sh"
source "$SCRIPT_DIR/lib/shared_config.sh"
source "$SCRIPT_DIR/lib/auth.sh"
source "$SCRIPT_DIR/lib/commands.sh"

# Auto-load terraform/config.auto.tfvars.json into shell env so downstream
# scripts that read env vars (generate_tfvars, run-local.sh, etc.) pick up
# the user's configured values. Only loads if the file exists — fresh
# clones before `make configure` simply fall through.
if [ -f "$TERRAFORM_DIR/config.auto.tfvars.json" ] && command -v python3 >/dev/null 2>&1; then
  while IFS='=' read -r key value; do
    [ -z "$key" ] && continue
    # Only set if not already set (env-var override wins).
    if [ -z "${!key:-}" ]; then
      export "$key=$value"
    fi
  done < <(
    python3 - <<'PY'
import json, os, sys
try:
    with open(os.path.join("terraform", "config.auto.tfvars.json")) as f:
        data = json.load(f)
except Exception:
    sys.exit(0)
for tf_key, val in data.items():
    if isinstance(val, (dict, list)):
        continue  # tool_env_vars etc. are consumed by terraform directly
    env_key = tf_key.upper()
    print(f"{env_key}={val}")
PY
  )
fi

# Final safety-net defaults for shared keys that are still unset (i.e. no
# CLI override AND no JSON file — happens on fresh clones before `make
# configure`). The JSON auto-loader above is the preferred source.
AWS_REGION="${AWS_REGION:-us-east-1}"

# Warn if AWS_REGION conflicts with the shared config (common source of
# "state bucket not found" errors when Claude Code or shell env overrides region)
if [ -f "$TERRAFORM_DIR/config.auto.tfvars.json" ]; then
  _config_region=$(python3 -c "import json; print(json.load(open('$TERRAFORM_DIR/config.auto.tfvars.json')).get('aws_region',''))" 2>/dev/null || echo "")
  if [ -n "$_config_region" ] && [ "$_config_region" != "$AWS_REGION" ]; then
    warn "Region mismatch: environment has AWS_REGION=$AWS_REGION but shared config has aws_region=$_config_region"
    warn "Using $_config_region (from shared config). Override with: AWS_REGION=$_config_region make deploy-auto"
    export AWS_REGION="$_config_region"
    export AWS_DEFAULT_REGION="$_config_region"
  fi
  unset _config_region
fi

IDP_TYPE="${IDP_TYPE:-cognito}"
CUSTOM_IDP_ISSUER_URL="${CUSTOM_IDP_ISSUER_URL:-}"
CUSTOM_IDP_CLIENT_ID="${CUSTOM_IDP_CLIENT_ID:-}"
CUSTOM_IDP_CLIENT_SECRET="${CUSTOM_IDP_CLIENT_SECRET:-}"
APP_URL="${APP_URL:-}"
GATEWAY_AUTH="${GATEWAY_AUTH:-iam}"
DEPLOY_AGENTS="${DEPLOY_AGENTS:-}"
DEPLOY_MODE="${DEPLOY_MODE:-full}"
DEPLOY_TOOLS="${DEPLOY_TOOLS:-}"

# Translate CLI overrides into `-var` flags so terraform sees them.
#
# Precedence gotcha: Terraform's own precedence is `-var` > `*.auto.tfvars.json`
# > `TF_VAR_*` env vars, so `TF_VAR_*` does NOT beat config.auto.tfvars.json.
# We need `-var key=value` on the PLAN call to actually override the JSON.
# (`terraform apply tfplan` doesn't accept -var — the overrides are baked
# into the saved plan.) We only emit args for keys that came from CLI
# (captured in _CLI_<KEY> before .env was sourced); emitting for every key
# would override user edits to the JSON on normal deploys. DEPLOY_* are
# shell-side flags, not terraform variables.
#
# TF_VAR_CLI_OVERRIDES is consumed by run_terraform / shared_config_apply.
TF_CLI_OVERRIDES=()
for _k in $_SHARED_KEYS; do
  case "$_k" in DEPLOY_*) continue ;; esac
  _cli_var="_CLI_${_k}"
  _cli_value="${!_cli_var:-}"
  [ -z "$_cli_value" ] && continue
  _tf_name=$(echo "$_k" | tr '[:upper:]' '[:lower:]')
  TF_CLI_OVERRIDES+=("-var=${_tf_name}=${_cli_value}")
done
unset _k _cli_var _cli_value _tf_name

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
  local SUBCOMMAND=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --all)     DEPLOY_ALL=true; shift ;;
      --auto)    AUTO_APPROVE=true; shift ;;
      --plan)    PLAN_ONLY=true; shift ;;
      --destroy) DESTROY=true; shift ;;
      --destroy-all) DESTROY_ALL=true; shift ;;
      --build-agents-only) BUILD_AGENTS_ONLY=true; shift ;;
      --setup)             SUBCOMMAND="setup"; shift ;;
      --configure)         SUBCOMMAND="configure"; shift ;;
      --reconfigure-shared) SUBCOMMAND="reconfigure-shared"; shift ;;
      --nuke-shared-config) SUBCOMMAND="nuke-shared-config"; shift ;;
      --help)    show_help ;;
      *) die "Unknown option: $1. Use --help for usage." ;;
    esac
  done

  # Interactive subcommands short-circuit before the deploy flow.
  case "$SUBCOMMAND" in
    setup)
      cmd_setup
      exit 0
      ;;
    configure)
      validate_credentials
      cmd_configure
      exit 0
      ;;
    reconfigure-shared)
      validate_credentials
      cmd_reconfigure_shared
      exit 0
      ;;
    nuke-shared-config)
      validate_credentials
      cmd_nuke_shared_config
      exit 0
      ;;
  esac

  check_prerequisites
  validate_credentials

  # Load agent hierarchy from hierarchy.json
  _load_hierarchy

  # Initialize temp-file-based agent tracking
  _init_agent_tracking

  # --build-agents-only: resolve agents, build images, exit
  if [ "$BUILD_AGENTS_ONLY" = true ]; then
    _resolve_selected_agents
    build_all_agent_images
    info "Agent image builds complete."
    exit 0
  fi

  if [ "$DESTROY_ALL" = true ]; then
    if [ "$AUTO_APPROVE" != true ]; then
      warn "This will DESTROY all infrastructure, ECR repos, memory, and the Terraform state backend."
      read -rp "Type 'destroy-all' to confirm: " confirm
      if [ "$confirm" != "destroy-all" ]; then
        info "Cancelled."; exit 0
      fi
      AUTO_APPROVE=true
    fi
    run_full_destroy
    exit 0
  fi

  if [ "$DESTROY" = true ]; then
    run_terraform destroy
    exit 0
  fi

  # Auto-detect modules
  local tools data_modules
  tools=$(detect_lambda_tools)
  data_modules=$(detect_data_collection_modules)

  review_and_configure
  bootstrap_state_backend

  if [ "$PLAN_ONLY" = false ]; then
    # Build frontend in background while agent images build (if both needed)
    local _frontend_build_pid=""

    if [ "$DEPLOY_FLAG_FRONTEND" = true ] && [ "$DEPLOY_FLAG_AGENTS" = true ]; then
      # Start frontend build in background — it's independent of agent builds
      build_frontend &
      _frontend_build_pid=$!
    fi

    if [ "$DEPLOY_FLAG_AGENTS" = true ]; then
      build_all_agent_images
    fi

    if [ -n "$_frontend_build_pid" ]; then
      wait "$_frontend_build_pid" || die "Frontend build failed"
    elif [ "$DEPLOY_FLAG_FRONTEND" = true ]; then
      build_frontend
    fi
  else
    local placeholder_image="public.ecr.aws/docker/library/python:3.12-slim"
    # SELECTED_AGENTS is empty in gateway-only/tools-only modes; guard the
    # expansion so it doesn't trip `set -u` on bash 3.2 (macOS).
    for agent in ${SELECTED_AGENTS[@]+"${SELECTED_AGENTS[@]}"}; do
      local existing
      existing=$(get_agent_image "$agent")
      if [ -z "$existing" ]; then
        set_agent_image "$agent" "$placeholder_image"
      fi
    done
  fi

  generate_tfvars

  if [ "$PLAN_ONLY" = true ]; then
    run_terraform plan
  else
    run_terraform apply

    # Terraform apply resets gateway targets to single-tool placeholder schemas.
    # Invalidate the gateway-tools hash so sync_gateway_tools always re-uploads
    # the full multi-tool schemas from tools.json after every apply.
    rm -f "${HASH_DIR}/gateway-tools.sha"

    # After first deploy, capture CloudFront URL and update Cognito callback URLs.
    # APP_URL is owned by shared-config now (SSM-backed), so we write to the
    # config.auto.tfvars.json file and re-apply rather than to .env.
    local cf_url tf_memory_id
    cf_url=$(tf_output cloudfront_url)
    tf_memory_id=$(tf_output agentcore_memory_id)

    # Mirror MEMORY_ID into shared-config so subsequent fresh clones (or
    # `make reconfigure-shared` runs) see the active memory binding from SSM.
    # Set BEFORE the cf_url re-apply so both land in the same terraform run.
    if [ -n "$tf_memory_id" ] && [ "$DEPLOY_FLAG_AGENTS" = true ]; then
      local current_mem_ssm
      current_mem_ssm=$(shared_config_get_tfvars memory_id 2>/dev/null || echo "")
      if [ "$current_mem_ssm" != "$tf_memory_id" ]; then
        info "Mirroring memory_id to shared-config: $tf_memory_id"
        shared_config_set_value memory_id "$tf_memory_id"
      fi
    fi

    if [ -n "$cf_url" ] && [ "$APP_URL" != "$cf_url" ] && [ "$DEPLOY_FLAG_FRONTEND" = true ]; then
      info "Updating app_url to $cf_url (shared-config)"
      APP_URL="$cf_url"
      shared_config_set_value app_url "$APP_URL"
      info "Re-applying to update Cognito callback URLs..."
      terraform -chdir="$TERRAFORM_DIR" apply -auto-approve || warn "Re-apply for callback URLs failed (non-fatal)"
    elif [ -n "$tf_memory_id" ] && [ "$DEPLOY_FLAG_AGENTS" = true ]; then
      # No cf_url re-apply needed, but memory_id might still need to land in SSM.
      # Targeted apply on shared_config keeps it cheap (~5s).
      terraform -chdir="$TERRAFORM_DIR" apply -target=module.shared_config -auto-approve >/dev/null 2>&1 \
        || warn "Targeted re-apply for memory_id mirror failed (non-fatal)"
    fi

    if [ "$DEPLOY_FLAG_AGENTS" = true ]; then
      post_deploy_sync
    fi
    if [ "$DEPLOY_FLAG_GATEWAY" = true ] && [ "$DEPLOY_FLAG_TOOLS" = true ]; then
      sync_gateway_tools
    fi

    # Update .env.local from Terraform outputs and rebuild if env vars changed
    if [ "$DEPLOY_FLAG_FRONTEND" = true ]; then
      update_frontend_env_and_rebuild
      deploy_frontend_to_s3
    fi

    # If health-events collection is deployed but the table is empty, hint at
    # backfill. This typically happens on first deploy — the EventBridge rule
    # only catches events from the moment it was created onward.
    _maybe_hint_health_backfill
  fi

  info "Deployment complete!"
}

_maybe_hint_health_backfill() {
  local table="${PROJECT_PREFIX:-cloudops}-health-events"
  # Silent if table doesn't exist (module not deployed / different prefix).
  aws dynamodb describe-table --table-name "$table" --region "$AWS_REGION" >/dev/null 2>&1 || return 0
  local count
  count=$(aws dynamodb scan --table-name "$table" --select COUNT --region "$AWS_REGION" --query 'Count' --output text 2>/dev/null || echo "0")
  if [ "$count" = "0" ]; then
    echo ""
    warn "Health events table is empty — the EventBridge collector only captures events"
    warn "from this moment forward. Without backfill, the Health Events agent has no data to report."
    echo ""
    if [ -t 0 ] && [ "$AUTO_APPROVE" != true ]; then
      # Interactive — prompt the user
      echo "  Would you like to backfill the last 30 days of AWS Health events?"
      echo "  (Requires Business+ AWS Support plan. Skippable — you can run 'make backfill-health' later.)"
      echo ""
      read -rp "  Run backfill now? [y/N] " answer
      if [[ "$answer" =~ ^[Yy] ]]; then
        info "Running backfill (30 days, single-account)..."
        make -C "$PROJECT_ROOT" backfill-health DAYS=30 || warn "Backfill failed — you can retry with: make backfill-health DAYS=30"
      else
        info "Skipped. Run later with:"
        info "    make backfill-health DAYS=30             # single-account"
        info "    make backfill-health DAYS=30 ORG=1       # org-wide"
      fi
    else
      # Non-interactive (CI/auto) — just hint
      info "Run 'make backfill-health DAYS=30' to populate historical events."
    fi
  fi
}

main "$@"
