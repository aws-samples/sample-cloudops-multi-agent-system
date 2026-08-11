#!/usr/bin/env bash
# Build functions — ECR login, agent images, frontend

# -----------------------------------------------------------------------------
# _write_agent_hierarchy_slice — write a single-entry hierarchy.json for
# <agent> to src/agents/.hierarchy-<agent>.json. Writes atomically; the file
# is gitignored and consumed by the Dockerfile's AGENT_HIERARCHY_PATH ARG.
#
# Sliced files are per-agent so two parallel builds don't stomp each other.
# -----------------------------------------------------------------------------
_write_agent_hierarchy_slice() {
  local agent="$1"
  local slice_file="src/agents/.hierarchy-${agent}.json"
  .venv/bin/python - "$agent" "$slice_file" <<'PY'
import json, sys
agent, out_path = sys.argv[1], sys.argv[2]
with open("src/agents/hierarchy.json") as f:
    full = json.load(f)
if agent not in full:
    sys.stderr.write(f"agent {agent!r} not found in hierarchy.json\n")
    sys.exit(1)
with open(out_path, "w") as f:
    json.dump({agent: full[agent]}, f, indent=2, sort_keys=True)
PY
}

# -----------------------------------------------------------------------------
# _compute_agent_hash — deterministic hash of the sources that, if changed,
# should trigger a rebuild of this agent's container. The per-agent
# hierarchy.json slice replaces the full file, so edits to OTHER agents'
# entries don't flip this agent's hash (~13 min saved on prompt-only changes).
#
# The sliced file must already exist at src/agents/.hierarchy-<agent>.json;
# callers invoke _write_agent_hierarchy_slice before this helper.
# -----------------------------------------------------------------------------
_compute_agent_hash() {
  local agent="$1" agent_dir="$2" agent_type="$3"
  local slice_file="src/agents/.hierarchy-${agent}.json"

  if [ "$agent_type" = "frontend" ]; then
    find "$agent_dir/" src/agents/shared/ "$slice_file" \
      -type f \( -name '*.py' -o -name '*.txt' -o -name '*.json' -o -name 'Dockerfile' \) \
      -exec shasum {} + 2>/dev/null | sort | shasum | cut -d' ' -f1
  else
    find "$agent_dir/" src/agents/shared/ "$slice_file" \
      -type f \( -name '*.py' -o -name '*.txt' -o -name '*.json' -o -name 'Dockerfile' \) \
      ! -name 'agui_server.py' ! -name 'reports.py' ! -name 'memory.py' \
      ! -name 'suggestions.py' ! -name 'report_tool.py' \
      ! -path '*/report_templates/*' \
      -exec shasum {} + 2>/dev/null | sort | shasum | cut -d' ' -f1
  fi
}

ecr_login() {
  local account_id
  account_id=$(aws sts get-caller-identity --query 'Account' --output text)
  local ecr_registry="${account_id}.dkr.ecr.${AWS_REGION}.amazonaws.com"
  aws ecr get-login-password --region "$AWS_REGION" | $CONTAINER_CMD login --username AWS --password-stdin "$ecr_registry" || die "ECR login failed. Is $CONTAINER_CMD running?"
}

build_all_agent_images() {
  ecr_login

  # Phase 1: Check hashes and separate into build vs skip
  local to_build=()
  local account_id
  account_id=$(aws sts get-caller-identity --query 'Account' --output text)
  local ecr_registry="${account_id}.dkr.ecr.${AWS_REGION}.amazonaws.com"

  for agent in "${SELECTED_AGENTS[@]}"; do
    local agent_dir
    agent_dir=$(get_agent_dir "$agent")
    local repo_name="${PROJECT_PREFIX}-${ENVIRONMENT}-${agent}"
    local image_uri="${ecr_registry}/${repo_name}:latest"
    local hash_file="${HASH_DIR}/${agent}.sha"
    mkdir -p "$HASH_DIR"
    set_agent_image "$agent" "$image_uri"

    # Write per-agent hierarchy slice, then hash the slice + agent source.
    _write_agent_hierarchy_slice "$agent"
    local current_hash agent_type
    agent_type=$(get_agent_type "$agent" 2>/dev/null || echo "")
    current_hash=$(_compute_agent_hash "$agent" "$agent_dir" "$agent_type")
    local stored_hash
    stored_hash=$(cat "$hash_file" 2>/dev/null || echo "")

    if [ "$current_hash" = "$stored_hash" ]; then
      info "${agent}: unchanged, skipping build"
      set_agent_changed "$agent" "false"
    else
      to_build+=("$agent")
    fi
  done

  if [ ${#to_build[@]} -eq 0 ]; then
    info "All agent images up to date"
    return
  fi

  # Phase 2: Build + push in parallel batches. Default comes from a CPU-aware
  # helper so a small laptop doesn't thrash Finch's VM. User can still
  # override via MAX_PARALLEL_BUILDS env var for one-off tuning.
  local max_parallel="${MAX_PARALLEL_BUILDS:-$(_detect_max_parallel_builds)}"
  local total=${#to_build[@]}
  info "Building ${total} agent image(s) in parallel (max ${max_parallel} concurrent)..."

  local build_log_dir
  build_log_dir=$(mktemp -d)
  local idx=0

  while [ $idx -lt $total ]; do
    local batch_pids=()
    local batch_agents=()
    local batch_size=0

    # Launch a batch of up to max_parallel builds
    while [ $batch_size -lt "$max_parallel" ] && [ $idx -lt $total ]; do
      local agent="${to_build[$idx]}"
      _build_single_agent "$agent" > "${build_log_dir}/${agent}.log" 2>&1 &
      batch_pids+=($!)
      batch_agents+=("$agent")
      batch_size=$((batch_size + 1))
      idx=$((idx + 1))
    done

    # Wait for entire batch to complete
    local i=0
    for pid in "${batch_pids[@]}"; do
      local agent="${batch_agents[$i]}"
      wait "$pid"
      local exit_code=$?
      cat "${build_log_dir}/${agent}.log"
      if [ $exit_code -ne 0 ]; then
        # Kill remaining builds in this batch
        local j=$((i + 1))
        while [ $j -lt ${#batch_pids[@]} ]; do
          kill "${batch_pids[$j]}" 2>/dev/null
          j=$((j + 1))
        done
        rm -rf "$build_log_dir"
        die "${agent} build failed (exit $exit_code)"
      fi
      i=$((i + 1))
    done
  done

  rm -rf "$build_log_dir"
  info "All agent image builds complete"
}

_build_single_agent() {
  local agent_name="$1"
  local agent_dir
  agent_dir=$(get_agent_dir "$agent_name")
  local repo_name="${PROJECT_PREFIX}-${ENVIRONMENT}-${agent_name}"
  local tag="latest"
  local hash_file="${HASH_DIR}/${agent_name}.sha"

  local account_id
  account_id=$(aws sts get-caller-identity --query 'Account' --output text)
  local ecr_registry="${account_id}.dkr.ecr.${AWS_REGION}.amazonaws.com"
  local image_uri="${ecr_registry}/${repo_name}:${tag}"

  # Ensure ECR repo exists. ECR repos are created here via the CLI, NOT by
  # Terraform, so the provider's default_tags don't apply to them — tag them
  # explicitly so they carry the same project/environment/retention metadata
  # as every other resource in the stack.
  if ! aws ecr describe-repositories --repository-names "$repo_name" --region "$AWS_REGION" > /dev/null 2>&1; then
    info "Creating ECR repo: $repo_name"
    aws ecr create-repository --repository-name "$repo_name" --region "$AWS_REGION" \
      --image-scanning-configuration scanOnPush=true \
      --tags "Key=auto-delete,Value=no" "Key=project,Value=${PROJECT_PREFIX}" "Key=environment,Value=${ENVIRONMENT}" "Key=managed_by,Value=deploy-script" > /dev/null
  fi

  # Ensure the per-agent hierarchy slice exists (build_all_agent_images writes
  # it during the Phase-1 hash scan, but this helper may also be called by
  # the --build-agents-only path or a retry — re-write defensively.
  _write_agent_hierarchy_slice "$agent_name"
  local slice_arg="src/agents/.hierarchy-${agent_name}.json"

  info "Building ${agent_name} image..."
  $CONTAINER_CMD build \
    --build-arg "AGENT_HIERARCHY_PATH=${slice_arg}" \
    -t "${repo_name}:${tag}" -f "${agent_dir}/Dockerfile" . || return 1

  info "Pushing ${agent_name} image to ECR..."
  $CONTAINER_CMD tag "${repo_name}:${tag}" "$image_uri"
  $CONTAINER_CMD push "$image_uri" || return 1

  # Recompute and save hash (matches the Phase-1 computation).
  local current_hash agent_type
  agent_type=$(get_agent_type "$agent_name" 2>/dev/null || echo "")
  current_hash=$(_compute_agent_hash "$agent_name" "$agent_dir" "$agent_type")
  echo "$current_hash" > "$hash_file"
  set_agent_changed "$agent_name" "true"
  info "${agent_name}: $image_uri"
}

# ---------------------------------------------------------------------------
# Frontend Build + S3 Deploy
# ---------------------------------------------------------------------------
FRONTEND_DIR="src/frontend"
FRONTEND_CHANGED=false
HASH_DIR=".lambda-hashes"

build_frontend() {
  local hash_file="${HASH_DIR}/frontend.sha"
  mkdir -p "$HASH_DIR"

  local current_hash
  current_hash=$(find "$FRONTEND_DIR/src" "$FRONTEND_DIR/next.config.ts" "$FRONTEND_DIR/public" -type f 2>/dev/null -exec shasum {} + | sort | shasum | cut -d' ' -f1 || echo "unknown")
  local stored_hash
  stored_hash=$(cat "$hash_file" 2>/dev/null || echo "")

  if [ "$current_hash" = "$stored_hash" ] && [ -d "$FRONTEND_DIR/out" ]; then
    info "Frontend: unchanged, skipping build"
    return
  fi

  info "Building frontend..."
  npm --prefix "$FRONTEND_DIR" install --silent 2>/dev/null
  npm --prefix "$FRONTEND_DIR" run build || die "Frontend build failed"

  echo "$current_hash" > "$hash_file"
  FRONTEND_CHANGED=true
  info "Frontend build complete"
}

# ---------------------------------------------------------------------------
# Update .env.local from Terraform outputs and rebuild frontend if changed
# Called after terraform apply when outputs are available.
# ---------------------------------------------------------------------------
update_frontend_env_and_rebuild() {
  local env_local="$FRONTEND_DIR/.env.local"

  # Read current Terraform outputs
  local user_pool_id cognito_client_id cognito_domain cf_url runtime_arn frontend_api_url
  user_pool_id=$(tf_output cognito_user_pool_id)
  cognito_client_id=$(tf_output cognito_app_client_id)
  cognito_domain=$(tf_output cognito_domain)
  cf_url=$(tf_output cloudfront_url)
  runtime_arn=$(tf_output supervisor_runtime_arn)
  frontend_api_url=$(tf_output frontend_api_url)

  if [ -z "$user_pool_id" ] || [ -z "$cognito_client_id" ]; then
    warn "Terraform outputs not available, skipping .env.local update"
    return
  fi

  local discovery_url="https://cognito-idp.${AWS_REGION}.amazonaws.com/${user_pool_id}/.well-known/openid-configuration"
  local callback_url="${cf_url}/callback/"

  # Build desired content
  local desired
  desired=$(cat <<ENVEOF
# Frontend environment variables (baked at build time for Next.js static export)
# Auto-generated by deploy.sh from terraform outputs — do not edit manually

NEXT_PUBLIC_OIDC_DISCOVERY_URL=${discovery_url}
NEXT_PUBLIC_OIDC_CLIENT_ID=${cognito_client_id}
NEXT_PUBLIC_OIDC_CALLBACK_URL=${callback_url}
NEXT_PUBLIC_COGNITO_DOMAIN=${cognito_domain}
NEXT_PUBLIC_RUNTIME_ARN=${runtime_arn}
NEXT_PUBLIC_FRONTEND_API_URL=${frontend_api_url}
NEXT_PUBLIC_AWS_REGION=${AWS_REGION}
NEXT_PUBLIC_DEV_AUTH_BYPASS=false
ENVEOF
)

  # Compare with existing .env.local
  local existing
  existing=$(cat "$env_local" 2>/dev/null || echo "")

  if [ "$desired" = "$existing" ]; then
    info "Frontend .env.local already up to date"
    return
  fi

  info "Updating frontend .env.local with Terraform outputs..."
  echo "$desired" > "$env_local"

  # Force rebuild since baked env vars changed
  info "Rebuilding frontend with updated env vars..."
  rm -f "${HASH_DIR}/frontend.sha"
  rm -rf "$FRONTEND_DIR/out"
  build_frontend
}
