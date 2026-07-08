#!/bin/bash
# Run Next.js frontend locally with Cognito authentication.
#
# Self-healing Cognito whitelist: on start, adds http://localhost:3000/ to
# the user pool client's CallbackURLs/LogoutURLs if missing. On exit (clean
# or trap-triggered), removes it so the production allow-list stays lean.
# Terraform no longer carries localhost permanently — this script owns the
# transient whitelist while it's running.
#
# Usage: ./scripts/run-local.sh [--bypass-auth]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TERRAFORM_DIR="$PROJECT_ROOT/terraform"
FRONTEND_DIR="$PROJECT_ROOT/src/frontend"
LOCALHOST_URL="http://localhost:3000/"

# Absolute path to the venv Python. MUST be absolute: this script cd's into
# $FRONTEND_DIR before launching `next dev`, so the Cognito-cleanup trap runs
# with cwd=src/frontend. A relative ".venv/bin/python" would fail there with
# "No such file or directory", silently skipping the localhost allow-list
# removal and leaving a stale localhost callback in the app client on exit.
PYTHON="$PROJECT_ROOT/.venv/bin/python"

# Source .env for AWS_PROFILE, AWS_REGION, PROJECT_PREFIX, etc.
if [ -f "$PROJECT_ROOT/.env" ]; then
  set -a
  source "$PROJECT_ROOT/.env"
  set +a
fi

AWS_REGION="${AWS_REGION:-us-east-1}"
PROJECT_PREFIX="${PROJECT_PREFIX:-cloudops}"
ENVIRONMENT="${ENVIRONMENT:-dev}"
DEV_AUTH_BYPASS="false"

if [ "${1:-}" = "--bypass-auth" ]; then
  DEV_AUTH_BYPASS="true"
  echo "Auth bypass enabled — skipping Cognito login"
fi

# ---------------------------------------------------------------------------
# Terraform Output Helper (reuses common.sh logic for TF warning stripping)
# ---------------------------------------------------------------------------
source "$SCRIPT_DIR/lib/common.sh" 2>/dev/null || true
if ! type tf_output &>/dev/null; then
  tf_output() {
    local raw
    raw=$(terraform -chdir="$TERRAFORM_DIR" output -no-color -raw "$1" 2>/dev/null || echo "")
    echo "$raw" | sed '/^$/d; /^Warning:/d; /^│/d; /^╷/d; /^╵/d; /^The parameter/d; /instead\./d' | head -1
  }
fi
get_tf_output() { tf_output "$1"; }

echo "Fetching Terraform outputs..."
RUNTIME_ARN=$(get_tf_output "supervisor_runtime_arn")
RUNTIME_ID=$(get_tf_output "agentcore_runtime_id")
COGNITO_USER_POOL_ID=$(get_tf_output "cognito_user_pool_id")
COGNITO_APP_CLIENT_ID=$(get_tf_output "cognito_app_client_id")
COGNITO_DOMAIN=$(get_tf_output "cognito_domain")
CLOUDFRONT_URL=$(get_tf_output "cloudfront_url")

if [ -z "$RUNTIME_ARN" ]; then
  echo "Error: Could not fetch Terraform outputs. Run 'make deploy-auto' first."
  exit 1
fi

FRONTEND_API_URL=$(get_tf_output "frontend_api_url")

# ---------------------------------------------------------------------------
# Self-healing Cognito whitelist — add http://localhost:3000/ on start, trap
# EXIT/INT/TERM to remove it on quit. Idempotent: if localhost is already
# present (e.g. from a SIGKILL'd previous run), the add no-ops and the trap
# won't remove it, matching the "leave state as we found it" principle.
# ---------------------------------------------------------------------------
WE_ADDED_CALLBACK=0
WE_ADDED_LOGOUT=0

_get_cognito_client_json() {
  aws cognito-idp describe-user-pool-client \
    --user-pool-id "$COGNITO_USER_POOL_ID" \
    --client-id "$COGNITO_APP_CLIENT_ID" \
    --profile "${AWS_PROFILE:-default}" \
    --region "$AWS_REGION" \
    --output json 2>/dev/null || echo '{}'
}

_list_contains() {
  # Pass the JSON haystack as argv[2] — two stdin redirects collide (the
  # here-string silently replaces the heredoc script) and Python would try
  # to run the JSON as source, failing on `true`/`false` literals.
  local needle="$1" haystack_json="$2"
  "$PYTHON" - "$needle" "$haystack_json" <<'PY'
import json, sys
needle = sys.argv[1]
try:
    items = json.loads(sys.argv[2])
except Exception:
    items = []
sys.exit(0 if needle in (items or []) else 1)
PY
}

_update_cognito_urls() {
  local callbacks_json="$1" logouts_json="$2"
  local current
  current=$(_get_cognito_client_json)

  # Carry forward every other field so we don't accidentally narrow the
  # client's config by posting a minimal update. Pass the Cognito
  # describe-client response as argv[7] — two stdin redirects collide and
  # Python would try to run the JSON as source.
  "$PYTHON" - "$COGNITO_USER_POOL_ID" "$COGNITO_APP_CLIENT_ID" \
    "$AWS_REGION" "${AWS_PROFILE:-default}" \
    "$callbacks_json" "$logouts_json" "$current" <<'PY'
import json, subprocess, sys
pool_id, client_id, region, profile, callbacks_json, logouts_json, current_json = sys.argv[1:]
client = (json.loads(current_json) or {}).get("UserPoolClient", {})
callbacks = json.loads(callbacks_json)
logouts = json.loads(logouts_json)
# Full update-user-pool-client call. Only pass fields the API accepts here
# and that are set today — drop internal-only fields like ClientSecret and
# LastModifiedDate.
kept = {}
for k in (
    "AllowedOAuthFlows", "AllowedOAuthFlowsUserPoolClient", "AllowedOAuthScopes",
    "ClientName", "DefaultRedirectURI", "ExplicitAuthFlows", "IdTokenValidity",
    "AccessTokenValidity", "RefreshTokenValidity", "TokenValidityUnits",
    "ReadAttributes", "WriteAttributes", "SupportedIdentityProviders",
    "PreventUserExistenceErrors", "EnableTokenRevocation",
    "AuthSessionValidity", "EnablePropagateAdditionalUserContextData",
):
    if k in client:
        kept[k] = client[k]
kept["UserPoolId"] = pool_id
kept["ClientId"] = client_id
kept["CallbackURLs"] = callbacks
kept["LogoutURLs"] = logouts
cmd = ["aws", "cognito-idp", "update-user-pool-client",
       "--profile", profile, "--region", region,
       "--cli-input-json", json.dumps(kept)]
subprocess.run(cmd, check=True, capture_output=True)
PY
}

_cognito_whitelist_add() {
  if [ -z "$COGNITO_USER_POOL_ID" ] || [ -z "$COGNITO_APP_CLIENT_ID" ]; then
    return 0
  fi
  local json
  json=$(_get_cognito_client_json)
  local current_cb current_lo
  current_cb=$("$PYTHON" -c "import json,sys; d=json.load(sys.stdin).get('UserPoolClient',{}); print(json.dumps(d.get('CallbackURLs',[])))" <<<"$json")
  current_lo=$("$PYTHON" -c "import json,sys; d=json.load(sys.stdin).get('UserPoolClient',{}); print(json.dumps(d.get('LogoutURLs',[])))" <<<"$json")

  local new_cb new_lo
  new_cb=$("$PYTHON" -c "import json,sys; u='$LOCALHOST_URL'; lst=json.loads(sys.stdin.read()); print(json.dumps(lst if u in lst else lst+[u]))" <<<"$current_cb")
  new_lo=$("$PYTHON" -c "import json,sys; u='$LOCALHOST_URL'; lst=json.loads(sys.stdin.read()); print(json.dumps(lst if u in lst else lst+[u]))" <<<"$current_lo")

  if [ "$new_cb" != "$current_cb" ]; then WE_ADDED_CALLBACK=1; fi
  if [ "$new_lo" != "$current_lo" ]; then WE_ADDED_LOGOUT=1; fi

  if [ "$WE_ADDED_CALLBACK" = 1 ] || [ "$WE_ADDED_LOGOUT" = 1 ]; then
    echo "Adding $LOCALHOST_URL to Cognito allow-list (removed on exit)..."
    _update_cognito_urls "$new_cb" "$new_lo"
  else
    echo "Cognito already allows $LOCALHOST_URL — leaving as-is."
  fi
}

_cognito_whitelist_remove() {
  # Only revert what WE added. SIGKILL'd prior runs that left localhost in
  # the list won't be cleaned up here — but the next run-local invocation
  # detects "already present" and won't re-add (and won't trap either).
  if [ "$WE_ADDED_CALLBACK" = 0 ] && [ "$WE_ADDED_LOGOUT" = 0 ]; then
    return 0
  fi
  if [ -z "$COGNITO_USER_POOL_ID" ] || [ -z "$COGNITO_APP_CLIENT_ID" ]; then
    return 0
  fi
  local json
  json=$(_get_cognito_client_json)
  local current_cb current_lo new_cb new_lo
  current_cb=$("$PYTHON" -c "import json,sys; d=json.load(sys.stdin).get('UserPoolClient',{}); print(json.dumps(d.get('CallbackURLs',[])))" <<<"$json")
  current_lo=$("$PYTHON" -c "import json,sys; d=json.load(sys.stdin).get('UserPoolClient',{}); print(json.dumps(d.get('LogoutURLs',[])))" <<<"$json")
  if [ "$WE_ADDED_CALLBACK" = 1 ]; then
    new_cb=$("$PYTHON" -c "import json,sys; u='$LOCALHOST_URL'; print(json.dumps([x for x in json.loads(sys.stdin.read()) if x!=u]))" <<<"$current_cb")
  else
    new_cb="$current_cb"
  fi
  if [ "$WE_ADDED_LOGOUT" = 1 ]; then
    new_lo=$("$PYTHON" -c "import json,sys; u='$LOCALHOST_URL'; print(json.dumps([x for x in json.loads(sys.stdin.read()) if x!=u]))" <<<"$current_lo")
  else
    new_lo="$current_lo"
  fi
  echo "Removing $LOCALHOST_URL from Cognito allow-list..."
  _update_cognito_urls "$new_cb" "$new_lo" || echo "  (remove failed, not fatal — manual cleanup may be needed)"
}

if [ "$DEV_AUTH_BYPASS" != "true" ]; then
  _cognito_whitelist_add
  trap _cognito_whitelist_remove EXIT INT TERM
fi

# ---------------------------------------------------------------------------
# Frontend env vars for Next.js dev server
# ---------------------------------------------------------------------------
export NEXT_PUBLIC_RUNTIME_ARN="$RUNTIME_ARN"
export NEXT_PUBLIC_AWS_REGION="$AWS_REGION"
export NEXT_PUBLIC_DEV_AUTH_BYPASS="$DEV_AUTH_BYPASS"
export NEXT_PUBLIC_OIDC_DISCOVERY_URL="https://cognito-idp.${AWS_REGION}.amazonaws.com/${COGNITO_USER_POOL_ID}/.well-known/openid-configuration"
export NEXT_PUBLIC_OIDC_CLIENT_ID="$COGNITO_APP_CLIENT_ID"
export NEXT_PUBLIC_OIDC_CALLBACK_URL="$LOCALHOST_URL"
export NEXT_PUBLIC_COGNITO_DOMAIN="$COGNITO_DOMAIN"
export NEXT_PUBLIC_FRONTEND_API_URL="$FRONTEND_API_URL"

echo "  Runtime: ${RUNTIME_ID}"
echo "  API: ${FRONTEND_API_URL}"
echo "  Auth: ${DEV_AUTH_BYPASS:+BYPASSED}${DEV_AUTH_BYPASS:+}${DEV_AUTH_BYPASS:-Cognito ($COGNITO_APP_CLIENT_ID)}"
echo "  Callback: $LOCALHOST_URL"

echo ""

cd "$FRONTEND_DIR"

# Install dependencies if needed
if [ ! -d node_modules ]; then
  echo "Installing dependencies..."
  npm install
fi

echo "Starting Next.js dev server on http://localhost:3000"
if [ "$DEV_AUTH_BYPASS" = "true" ]; then
  echo "Auth bypassed — no login required"
else
  echo "Login with your Cognito credentials"
fi
echo ""
npx next dev --port 3000
