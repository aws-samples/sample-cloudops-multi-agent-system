#!/usr/bin/env bash
# Common helpers — logging, env persistence, prerequisites, credentials

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
info()  { echo -e "\033[0;34m[INFO]\033[0m  $*"; }
warn()  { echo -e "\033[0;33m[WARN]\033[0m  $*"; }
error() { echo -e "\033[0;31m[ERROR]\033[0m $*" >&2; }
die()   { error "$@"; exit 1; }

# ---------------------------------------------------------------------------
# .env Persistence
# ---------------------------------------------------------------------------
ENV_FILE=".env"

save_env_var() {
  local key="$1" value="$2"
  if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
    sed -i.bak "s|^${key}=.*|${key}=${value}|" "$ENV_FILE" && rm -f "${ENV_FILE}.bak"
  else
    echo "${key}=${value}" >> "$ENV_FILE"
  fi
}

# Read the canonical list of SSM-owned keys from scripts/shared-keys.txt.
# Echoes a space-separated list. Comments and blank lines are stripped.
# Callers: deploy.sh (precedence logic), commands.sh (stale-env migration).
load_shared_keys() {
  local manifest="${SCRIPT_DIR:-$(dirname "${BASH_SOURCE[0]}")}/shared-keys.txt"
  # SCRIPT_DIR is set by deploy.sh; fall back to scripts/ if we're being
  # sourced from somewhere else.
  [ -f "$manifest" ] || manifest="scripts/shared-keys.txt"
  grep -vE '^\s*(#|$)' "$manifest" 2>/dev/null | tr '\n' ' '
}

# Populate _TF_OVERRIDES (global array) from TF_CLI_OVERRIDES.
#
# Both `run_terraform` (full apply/plan/destroy) and `shared_config_apply`
# (targeted module apply) need to forward CLI overrides as `-var=...` flags.
# `declare -p` is the reliable existence test; `${#ARR[@]+x}` silently
# swallows populated arrays (hit that regression during the SSM reland).
#
# Callers use `"${_TF_OVERRIDES[@]+"${_TF_OVERRIDES[@]}"}"` in their
# terraform invocation to splat the flags safely when empty.
load_tf_overrides() {
  _TF_OVERRIDES=()
  if declare -p TF_CLI_OVERRIDES >/dev/null 2>&1 && [ "${#TF_CLI_OVERRIDES[@]}" -gt 0 ]; then
    _TF_OVERRIDES=("${TF_CLI_OVERRIDES[@]}")
  fi
}

# Delete a KEY= line (and any blank line immediately above it) from .env.
# Used by the cmd_setup migration path to strip SSM-owned keys that pre-date
# the move to Terraform-managed shared config.
remove_env_var() {
  local key="$1"
  [ -f "$ENV_FILE" ] || return 0
  if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
    sed -i.bak "/^${key}=/d" "$ENV_FILE" && rm -f "${ENV_FILE}.bak"
  fi
}

# ---------------------------------------------------------------------------
# Terraform Output Helper — strips deprecation warnings from stdout
# Terraform v1.10+ emits "Deprecated Parameter" warnings to stdout when
# backend config uses `dynamodb_table` (now `use_lockfile`). These corrupt
# `terraform output -raw` values. This helper filters them out.
# ---------------------------------------------------------------------------
tf_output() {
  local raw
  raw=$(terraform -chdir="$TERRAFORM_DIR" output -no-color -raw "$1" 2>/dev/null || echo "")
  # Strip Terraform warning/error blocks and blank lines, keep only the value
  echo "$raw" | sed '/^$/d; /^Warning:/d; /^│/d; /^╷/d; /^╵/d; /^The parameter/d; /instead\./d' | head -1
}

# ---------------------------------------------------------------------------
# Prerequisites Check
# ---------------------------------------------------------------------------
CONTAINER_CMD=""

check_prerequisites() {
  local missing=()
  local version_errors=()

  # --- Presence checks ---
  command -v aws &>/dev/null || missing+=("aws (AWS CLI v2)")
  command -v terraform &>/dev/null || missing+=("terraform (>=1.5)")
  command -v python3 &>/dev/null || missing+=("python3 (>=3.10)")

  if [ "$DESTROY" != true ] && [ "$DESTROY_ALL" != true ]; then
    # Container runtime only needed when building agent images
    if [ "$DEPLOY_MODE" != "tools-only" ] && [ "$DEPLOY_MODE" != "gateway-only" ] || [ -z "$DEPLOY_MODE" ]; then
      if command -v finch &>/dev/null; then
        CONTAINER_CMD="finch"
        if ! finch vm status 2>/dev/null | grep -q "Running"; then
          die "Finch VM is not running. Start it with: finch vm start"
        fi
      elif command -v docker &>/dev/null; then
        CONTAINER_CMD="docker"
      else
        missing+=("finch or docker (container runtime)")
      fi
    fi
    # Node.js only needed when building frontend
    if [ "$DEPLOY_MODE" != "tools-only" ] && [ "$DEPLOY_MODE" != "gateway-only" ] && [ "$DEPLOY_MODE" != "agents-only" ] || [ -z "$DEPLOY_MODE" ]; then
      command -v node &>/dev/null || missing+=("node (Node.js >=18)")
      command -v npm &>/dev/null || missing+=("npm")
    fi
    command -v zip &>/dev/null || missing+=("zip")
  fi

  if [ ${#missing[@]} -gt 0 ]; then
    error "Missing required tools:"
    for tool in "${missing[@]}"; do echo "  - $tool"; done
    die "Install the missing tools and try again."
  fi

  # --- Version checks (only for tools confirmed present) ---
  # Check the venv Python first (that's what actually runs), fall back to PATH python3.
  local _py3_check=""
  if [ -x ".venv/bin/python" ]; then
    _py3_check=".venv/bin/python"
  elif command -v python3 &>/dev/null; then
    _py3_check="python3"
  fi
  if [ -n "$_py3_check" ]; then
    if ! "$_py3_check" -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" 2>/dev/null; then
      local pyver; pyver=$("$_py3_check" --version 2>&1 | awk '{print $2}')
      version_errors+=("python3: found $pyver, requires >=3.10")
    fi
  fi

  if command -v node &>/dev/null && [ "$DEPLOY_MODE" != "tools-only" ] && [ "$DEPLOY_MODE" != "gateway-only" ] && [ "$DEPLOY_MODE" != "agents-only" ]; then
    local nodever; nodever=$(node -v 2>/dev/null | sed 's/v//')
    local nodemajor; nodemajor=$(echo "$nodever" | cut -d. -f1)
    if [ "${nodemajor:-0}" -lt 18 ]; then
      version_errors+=("node: found v$nodever, requires >=18")
    fi
  fi

  if command -v terraform &>/dev/null; then
    local tfver; tfver=$(terraform version -json 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin)['terraform_version'])" 2>/dev/null || terraform version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+')
    local tfmajor; tfmajor=$(echo "$tfver" | cut -d. -f1)
    local tfminor; tfminor=$(echo "$tfver" | cut -d. -f2)
    if [ "${tfmajor:-0}" -lt 1 ] || { [ "${tfmajor:-0}" -eq 1 ] && [ "${tfminor:-0}" -lt 5 ]; }; then
      version_errors+=("terraform: found v$tfver, requires >=1.5")
    fi
  fi

  if command -v aws &>/dev/null; then
    if ! aws --version 2>&1 | grep -q "aws-cli/2"; then
      local awsver; awsver=$(aws --version 2>&1 | awk '{print $1}')
      version_errors+=("aws: found $awsver, requires aws-cli/2.x")
    fi
  fi

  if [ ${#version_errors[@]} -gt 0 ]; then
    error "Tool version requirements not met:"
    for ve in "${version_errors[@]}"; do echo "  - $ve"; done
    die "Upgrade the tools above and try again."
  fi

  info "Prerequisites OK"
}

# ---------------------------------------------------------------------------
# AWS Credential Validation
#
# When AUTH_METHOD=sso, defer to preflight_auth (auth.sh) so an expired SSO
# token auto-launches `aws sso login` instead of dying. For credentials mode
# (or when AUTH_METHOD is unset, i.e. legacy setups) keep the simple sts probe.
# ---------------------------------------------------------------------------
validate_credentials() {
  info "Validating AWS credentials..."
  if [ "${AUTH_METHOD:-}" = "sso" ] && declare -f preflight_auth >/dev/null 2>&1; then
    preflight_auth
    info "Region: $AWS_REGION"
    return 0
  fi
  if ! aws sts get-caller-identity > /dev/null 2>&1; then
    die "AWS credentials not configured. Run 'aws configure' or set AWS_PROFILE."
  fi
  local identity
  identity=$(aws sts get-caller-identity --output text --query 'Arn' 2>/dev/null || echo "unknown")
  info "Authenticated as: $identity"
  info "Region: $AWS_REGION"
}

show_help() {
  cat <<'EOF'
CloudOps Multi-Agent System — Deployment Script

USAGE:
  make deploy-auto     Deploy everything non-interactively
  make deploy          Interactive deployment
  make plan            Terraform plan only
  make destroy         Destroy Terraform-managed infrastructure
  make destroy-all     Full teardown (infra + ECR + memory + state backend)

ENVIRONMENT VARIABLES:
  PROJECT_PREFIX          Resource naming prefix (default: cloudops)
  ENVIRONMENT             Deployment environment: dev|staging|prod (default: dev)
  AWS_REGION              AWS region (default: us-east-1)
  IDP_TYPE                Identity provider: cognito|custom (default: cognito)
  DEPLOY_AGENTS           Comma-separated agent names for selective deploy
EOF
  exit 0
}
