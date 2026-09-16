#!/usr/bin/env bash
# Verify sync_gateway_tools pages through list_gateway_targets.
#
# The bug this guards against: the AWS API returns a bounded page of gateway
# targets and boto3 does NOT follow nextToken on its own (the AWS CLI does,
# which is what made this invisible — `aws ... list-gateway-targets` showed all
# 11 targets while the same call through boto3 returned 10). With a single
# call, any target past the first page is missing from `existing`, and a missing
# target is indistinguishable from one that was never created: the sync prints
# "target not found in gateway, skipping" and fails. That is exactly how the
# lambda-runtime target ended up deployed with zero tool schemas, making its 8
# tools unreachable through the gateway.
#
# boto3 is replaced by a stub that serves a scripted list of pages and records
# every call, so the paging behaviour is asserted without touching AWS.
#
# Run from project root:
#   bash tests/scripts/test_sync_gateway_targets.sh

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

assert_contains() {
  local haystack="$1" needle="$2" name="$3"
  TESTS_RUN=$((TESTS_RUN + 1))
  if printf '%s' "$haystack" | grep -qF -- "$needle"; then
    printf "  PASS  %s\n" "$name"
  else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    printf "  FAIL  %s\n        expected to contain: %q\n        actual: %q\n" \
      "$name" "$needle" "$haystack"
  fi
}

assert_not_contains() {
  local haystack="$1" needle="$2" name="$3"
  TESTS_RUN=$((TESTS_RUN + 1))
  if printf '%s' "$haystack" | grep -qF -- "$needle"; then
    TESTS_FAILED=$((TESTS_FAILED + 1))
    printf "  FAIL  %s\n        expected NOT to contain: %q\n        actual: %q\n" \
      "$name" "$needle" "$haystack"
  else
    printf "  PASS  %s\n" "$name"
  fi
}

PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$PROJECT_ROOT"

SANDBOX=$(mktemp -d)
trap 'rm -rf "$SANDBOX"' EXIT

REPO="$SANDBOX/repo"
mkdir -p "$REPO/scripts/lib" "$REPO/.venv/bin" "$REPO/src/lambda/mcp" \
  "$REPO/.lambda-hashes" "$REPO/fakelib"
cp "$PROJECT_ROOT/scripts/lib"/{common,sync}.sh "$REPO/scripts/lib/"
ln -sf "$PROJECT_ROOT/.venv/bin/python" "$REPO/.venv/bin/python"

# --- boto3 stub -------------------------------------------------------------
# Pages come from $FAKE_TARGET_PAGES (JSON list of lists of target names); every
# call is appended to $FAKE_CALL_LOG as one JSON object per line.
cat > "$REPO/fakelib/boto3.py" <<'PYEOF'
"""boto3 stand-in for sync_gateway_tools tests.

Only the three AgentCore control-plane calls sync_gateway_tools makes are
implemented. list_gateway_targets serves the pages named by
$FAKE_TARGET_PAGES and issues a nextToken for every page but the last, so a
caller that ignores the token sees only the first page — the exact failure the
real bug had.
"""

import json
import os


def _log(call, payload):
    with open(os.environ["FAKE_CALL_LOG"], "a") as handle:
        handle.write(json.dumps({"call": call, **payload}) + "\n")


class _Client:
    def __init__(self, service, region):
        self.service = service
        self.region = region
        _log("client", {"service": service, "region": region})

    def list_gateway_targets(self, **kwargs):
        _log("list", {
            "nextToken": kwargs.get("nextToken"),
            "maxResults": kwargs.get("maxResults"),
            "gatewayIdentifier": kwargs.get("gatewayIdentifier"),
        })
        with open(os.environ["FAKE_TARGET_PAGES"]) as handle:
            pages = json.load(handle)
        token = kwargs.get("nextToken")
        index = 0 if token is None else int(token.split(":")[1])
        response = {
            "items": [
                {"name": name, "targetId": "tid-" + name} for name in pages[index]
            ]
        }
        if index + 1 < len(pages):
            response["nextToken"] = "page:%d" % (index + 1)
        return response

    def get_gateway_target(self, **kwargs):
        _log("get", {"targetId": kwargs.get("targetId")})
        return {
            "targetConfiguration": {
                "mcp": {"lambda": {"lambdaArn": "arn:aws:lambda:x:1:function:f"}}
            }
        }

    def update_gateway_target(self, **kwargs):
        schemas = kwargs["targetConfiguration"]["mcp"]["lambda"]["toolSchema"]
        _log("update", {
            "name": kwargs.get("name"),
            "targetId": kwargs.get("targetId"),
            "tool_count": len(schemas["inlinePayload"]),
            "tool_names": [t["name"] for t in schemas["inlinePayload"]],
        })
        return {}


def client(service, region_name=None, **_kwargs):
    return _Client(service, region_name)
PYEOF

# --- tools.json -------------------------------------------------------------
# Two tools on the target under test; a second entry with no `tools` array to
# confirm schema-less entries are skipped rather than reported as missing.
cat > "$REPO/src/lambda/mcp/tools.json" <<'EOF'
{
  "commitments": {
    "tools": [
      {
        "name": "get_commitment_recommendations",
        "description": "Size SP/RI purchases",
        "input_schema": {
          "type": "object",
          "properties": {"lookback_days": {"type": "integer"}}
        }
      },
      {
        "name": "get_commitment_expiry",
        "description": "Upcoming expiries",
        "input_schema": {"type": "object", "properties": {}}
      }
    ]
  },
  "no-schemas": {}
}
EOF

cd "$REPO"
export AWS_REGION=ap-northeast-1
export HASH_DIR=".lambda-hashes"
export PYTHONPATH="$REPO/fakelib"
export FAKE_CALL_LOG="$SANDBOX/calls.jsonl"
export FAKE_TARGET_PAGES="$SANDBOX/pages.json"

# shellcheck disable=SC1091
source scripts/lib/common.sh
# shellcheck disable=SC1091
source scripts/lib/sync.sh

# Stub the Terraform lookup — no state, no terraform binary needed.
tf_output() { echo "GW-TEST-123"; }

# Count list calls / read the nth logged call of a kind.
#
# `grep -c` prints 0 AND exits 1 when there is no match, so the usual
# `|| echo 0` fallback emits a second zero and every count comparison against
# "0" fails on a two-line value. Capture first, then default.
count_calls() {
  local count
  count=$(grep -c "\"call\": \"$1\"" "$FAKE_CALL_LOG" 2>/dev/null) || count=0
  echo "$count"
}
nth_call() {
  grep "\"call\": \"$1\"" "$FAKE_CALL_LOG" 2>/dev/null | sed -n "${2}p"
}

reset_run() {
  : > "$FAKE_CALL_LOG"
  rm -f "$HASH_DIR/gateway-tools.sha"
}

# ---------------------------------------------------------------------------
# Test 1: target on the LAST page is still found and updated
#
# This is the regression. Pre-fix, only page 1 was read, so `commitments`
# (page 3) was absent from `existing` and the sync failed.
# ---------------------------------------------------------------------------
echo "Test 1: target on final page is found"
cat > "$FAKE_TARGET_PAGES" <<'EOF'
[["cost-explorer", "health"], ["network-resilience", "lambda-runtime"], ["commitments"]]
EOF
reset_run
OUTPUT=$(sync_gateway_tools 2>&1)
STATUS=$?

assert_eq "$STATUS" "0" "sync_gateway_tools succeeds"
assert_contains "$OUTPUT" "commitments: updated with 2 tool schemas" "target updated with both schemas"
assert_not_contains "$OUTPUT" "target not found" "no spurious not-found"
assert_not_contains "$OUTPUT" "sync failed" "no failure warning"
assert_eq "$(count_calls list)" "3" "all 3 pages were requested"
assert_eq "$(count_calls update)" "1" "exactly one target updated"

# ---------------------------------------------------------------------------
# Test 2: paging arguments are correct
# ---------------------------------------------------------------------------
echo
echo "Test 2: paging arguments"
FIRST=$(nth_call list 1)
SECOND=$(nth_call list 2)
THIRD=$(nth_call list 3)

assert_contains "$FIRST" '"nextToken": null' "first call sends no token"
assert_contains "$SECOND" '"nextToken": "page:1"' "second call forwards page-1 token"
assert_contains "$THIRD" '"nextToken": "page:2"' "third call forwards page-2 token"
assert_contains "$FIRST" '"maxResults": 100' "maxResults asks for a full page"
assert_contains "$SECOND" '"maxResults": 100' "maxResults persists across pages"
assert_contains "$FIRST" '"gatewayIdentifier": "GW-TEST-123"' "gateway id forwarded"

# ---------------------------------------------------------------------------
# Test 3: a single unpaginated page still works (no token → no second call)
# ---------------------------------------------------------------------------
echo
echo "Test 3: single page, no token"
cat > "$FAKE_TARGET_PAGES" <<'EOF'
[["commitments", "cost-explorer"]]
EOF
reset_run
OUTPUT=$(sync_gateway_tools 2>&1)
STATUS=$?

assert_eq "$STATUS" "0" "single-page sync succeeds"
assert_eq "$(count_calls list)" "1" "no extra call when no token returned"
assert_contains "$OUTPUT" "commitments: updated with 2 tool schemas" "target updated"

# ---------------------------------------------------------------------------
# Test 4: a genuinely absent target still fails loudly
#
# Paging must not paper over the real not-found case: an undeployed target has
# to keep failing the sync, otherwise a target with zero schemas ships silently.
# ---------------------------------------------------------------------------
echo
echo "Test 4: genuinely missing target fails"
cat > "$FAKE_TARGET_PAGES" <<'EOF'
[["cost-explorer"], ["health"]]
EOF
reset_run
OUTPUT=$(sync_gateway_tools 2>&1)
STATUS=$?

assert_eq "$STATUS" "0" "sync_gateway_tools returns 0 (failure is non-fatal by design)"
assert_contains "$OUTPUT" "commitments: target not found in gateway, skipping" "missing target reported"
assert_contains "$OUTPUT" "Gateway tool schema sync failed" "non-fatal warning emitted"
assert_eq "$(count_calls list)" "2" "both pages searched before giving up"
assert_eq "$(count_calls update)" "0" "nothing updated"
TESTS_RUN=$((TESTS_RUN + 1))
if [ ! -f "$HASH_DIR/gateway-tools.sha" ]; then
  printf "  PASS  %s\n" "hash file removed so the next deploy retries"
else
  TESTS_FAILED=$((TESTS_FAILED + 1))
  printf "  FAIL  %s\n" "hash file removed so the next deploy retries"
fi

# ---------------------------------------------------------------------------
# Test 5: entries without a `tools` array are skipped, not reported missing
# ---------------------------------------------------------------------------
echo
echo "Test 5: schema-less entries skipped"
assert_not_contains "$OUTPUT" "no-schemas" "tools.json entry with no schemas ignored"

# ---------------------------------------------------------------------------
# Test 6: success writes the hash file so an unchanged tools.json skips
# ---------------------------------------------------------------------------
echo
echo "Test 6: hash-based skip after success"
cat > "$FAKE_TARGET_PAGES" <<'EOF'
[["cost-explorer"], ["commitments"]]
EOF
reset_run
sync_gateway_tools >/dev/null 2>&1

TESTS_RUN=$((TESTS_RUN + 1))
if [ -f "$HASH_DIR/gateway-tools.sha" ]; then
  printf "  PASS  %s\n" "hash file written on success"
else
  TESTS_FAILED=$((TESTS_FAILED + 1))
  printf "  FAIL  %s\n" "hash file written on success"
fi

: > "$FAKE_CALL_LOG"
OUTPUT=$(sync_gateway_tools 2>&1)
assert_contains "$OUTPUT" "unchanged, skipping sync" "unchanged tools.json skips the sync"
assert_eq "$(count_calls list)" "0" "no API calls on the skipped run"

echo
echo "----------------------------------------"
echo "Ran $TESTS_RUN tests, $TESTS_FAILED failed."
echo "----------------------------------------"
exit "$TESTS_FAILED"
