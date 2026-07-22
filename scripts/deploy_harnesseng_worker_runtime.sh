#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/deploy_harnesseng_worker_runtime.sh --worker-id ID [--worker-id ID ...] [options]

Create (or reuse) a clean runtime bundle, deploy it to named workers via SCP/SSH,
sync into each worker repo, and run worker preflight.

Options:
  --worker-id ID          Worker id from worker_registry.json (repeatable; required)
  --worker-registry PATH  Worker registry JSON path
                          (default: tracking/collab/autonomous_loop/eval_suite_v1_tournament_orchestration/worker_registry.json)
  --repo-root PATH        Local repo root (default: current directory)
  --bundle PATH           Existing bundle path (.tar.gz). If omitted, bundle is built.
  --bundle-output-dir DIR Bundle output directory when building
                          (default: /private/tmp/harnesseng_runtime_bundles)
  --preflight-script-rel PATH
                          Preflight script path relative to repo root on worker
                          (default: tracking/collab/autonomous_loop/eval_suite_v1_tournament_orchestration/scripts/preflight_vm_env.sh)
  --skip-preflight        Skip remote preflight
  --skip-azure-start      Do not call az vm start (still resolves VM IP with az)
  --ssh-connect-timeout N SSH connect timeout seconds (default: 10)
  --vm-wait-seconds N     Wait for SSH readiness timeout (default: 300)
  --dry-run               Print planned actions only
  --help, -h              Show this help
EOF
}

log() { printf '[deploy-worker-runtime] %s\n' "$*"; }
die() { printf '[deploy-worker-runtime] ERROR: %s\n' "$*" >&2; exit 1; }

run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    log "DRY_RUN: $*"
    return 0
  fi
  "$@"
}

resolve_worker_field() {
  local worker_id="$1"
  local field="$2"
  jq -r --arg wid "$worker_id" --arg field "$field" '
    .workers[] | select(.worker_id == $wid) | .[$field] // empty
  ' "$WORKER_REGISTRY"
}

wait_for_ssh_ready() {
  local target="$1"
  local deadline=$((SECONDS + VM_WAIT_SECONDS))
  if [[ "$DRY_RUN" == "1" ]]; then
    log "DRY_RUN: wait for SSH readiness on ${target}"
    return 0
  fi
  while (( SECONDS < deadline )); do
    if ssh "${SSH_OPTS[@]}" "$target" "true" >/dev/null 2>&1; then
      return 0
    fi
    sleep 5
  done
  return 1
}

REPO_ROOT="$PWD"
WORKER_REGISTRY="tracking/collab/autonomous_loop/eval_suite_v1_tournament_orchestration/worker_registry.json"
BUNDLE_PATH=""
BUNDLE_OUTPUT_DIR="/private/tmp/harnesseng_runtime_bundles"
PREFLIGHT_SCRIPT_REL="tracking/collab/autonomous_loop/eval_suite_v1_tournament_orchestration/scripts/preflight_vm_env.sh"
SKIP_PREFLIGHT="0"
SKIP_AZURE_START="0"
SSH_CONNECT_TIMEOUT="10"
VM_WAIT_SECONDS="300"
DRY_RUN="0"
declare -a WORKER_IDS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --worker-id) WORKER_IDS+=("$2"); shift 2 ;;
    --worker-registry) WORKER_REGISTRY="$2"; shift 2 ;;
    --repo-root) REPO_ROOT="$2"; shift 2 ;;
    --bundle) BUNDLE_PATH="$2"; shift 2 ;;
    --bundle-output-dir) BUNDLE_OUTPUT_DIR="$2"; shift 2 ;;
    --preflight-script-rel) PREFLIGHT_SCRIPT_REL="$2"; shift 2 ;;
    --skip-preflight) SKIP_PREFLIGHT="1"; shift ;;
    --skip-azure-start) SKIP_AZURE_START="1"; shift ;;
    --ssh-connect-timeout) SSH_CONNECT_TIMEOUT="$2"; shift 2 ;;
    --vm-wait-seconds) VM_WAIT_SECONDS="$2"; shift 2 ;;
    --dry-run) DRY_RUN="1"; shift ;;
    --help|-h) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ "${#WORKER_IDS[@]}" -gt 0 ]] || die "at least one --worker-id is required"
[[ "$SSH_CONNECT_TIMEOUT" =~ ^[0-9]+$ ]] || die "--ssh-connect-timeout must be numeric"
[[ "$VM_WAIT_SECONDS" =~ ^[0-9]+$ ]] || die "--vm-wait-seconds must be numeric"

command -v jq >/dev/null 2>&1 || die "jq not found"
command -v ssh >/dev/null 2>&1 || die "ssh not found"
command -v scp >/dev/null 2>&1 || die "scp not found"
command -v az >/dev/null 2>&1 || die "az not found"

REPO_ROOT="$(cd "$REPO_ROOT" && pwd)"
WORKER_REGISTRY="${REPO_ROOT}/${WORKER_REGISTRY#./}"
[[ -f "$WORKER_REGISTRY" ]] || die "worker registry not found: $WORKER_REGISTRY"

HARNESSENG_AZURE_CONFIG_SOURCE="${HARNESSENG_AZURE_CONFIG_SOURCE:-${HOME}/.azure}"
HARNESSENG_AZURE_CONFIG_WORKDIR="${HARNESSENG_AZURE_CONFIG_WORKDIR:-/private/tmp/harnesseng_azcfg_${USER:-user}}"

if [[ -z "${AZURE_CONFIG_DIR:-}" && -d "$HARNESSENG_AZURE_CONFIG_SOURCE" ]]; then
  mkdir -p "$HARNESSENG_AZURE_CONFIG_WORKDIR"
  if [[ "$DRY_RUN" == "1" ]]; then
    log "DRY_RUN: copy Azure config ${HARNESSENG_AZURE_CONFIG_SOURCE} -> ${HARNESSENG_AZURE_CONFIG_WORKDIR}"
  else
    cp -R "${HARNESSENG_AZURE_CONFIG_SOURCE}/." "$HARNESSENG_AZURE_CONFIG_WORKDIR/"
  fi
  export AZURE_CONFIG_DIR="$HARNESSENG_AZURE_CONFIG_WORKDIR"
  log "using copied writable AZURE_CONFIG_DIR=${AZURE_CONFIG_DIR}"
fi

if [[ -z "$BUNDLE_PATH" ]]; then
  build_script="${REPO_ROOT}/scripts/build_harnesseng_runtime_bundle.sh"
  [[ -x "$build_script" || -f "$build_script" ]] || die "bundle builder missing: $build_script"
  if [[ "$DRY_RUN" == "1" ]]; then
    run "$build_script" --repo-root "$REPO_ROOT" --output-dir "$BUNDLE_OUTPUT_DIR" --dry-run
    BUNDLE_PATH="${BUNDLE_OUTPUT_DIR}/harnesseng_runtime_<dry-run>.tar.gz"
  else
    BUNDLE_PATH="$("$build_script" --repo-root "$REPO_ROOT" --output-dir "$BUNDLE_OUTPUT_DIR" --print-path-only)"
  fi
fi

[[ "$DRY_RUN" == "1" || -f "$BUNDLE_PATH" ]] || die "bundle not found: $BUNDLE_PATH"
bundle_file="$(basename "$BUNDLE_PATH")"
bundle_id="${bundle_file#harnesseng_runtime_}"
bundle_id="${bundle_id%.tar.gz}"
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout="${SSH_CONNECT_TIMEOUT}")

for worker_id in "${WORKER_IDS[@]}"; do
  enabled="$(resolve_worker_field "$worker_id" "enabled")"
  [[ -n "$enabled" ]] || die "worker not found in registry: $worker_id"
  [[ "$enabled" == "true" ]] || die "worker disabled in registry: $worker_id"

  rg="$(resolve_worker_field "$worker_id" "azure_resource_group")"
  vm_name="$(resolve_worker_field "$worker_id" "azure_vm_name")"
  vm_user="$(resolve_worker_field "$worker_id" "azure_vm_user")"
  remote_repo="$(resolve_worker_field "$worker_id" "remote_repo")"
  [[ -n "$rg" && -n "$vm_name" && -n "$vm_user" && -n "$remote_repo" ]] || die "worker missing required fields: $worker_id"

  log "worker=${worker_id} vm=${rg}/${vm_name}"
  if [[ "$SKIP_AZURE_START" != "1" ]]; then
    run az vm start --resource-group "$rg" --name "$vm_name" --output none
  fi

  if [[ "$DRY_RUN" == "1" ]]; then
    vm_ip="<dry-run-ip>"
  else
    vm_ip="$(az vm list-ip-addresses --resource-group "$rg" --name "$vm_name" --query "[0].virtualMachine.network.publicIpAddresses[0].ipAddress" -o tsv)"
    [[ -n "$vm_ip" ]] || die "failed to resolve public IP for worker ${worker_id}"
  fi
  ssh_target="${vm_user}@${vm_ip}"

  log "waiting for SSH on ${ssh_target}"
  wait_for_ssh_ready "$ssh_target" || die "SSH timeout for ${worker_id} (${ssh_target})"

  remote_bundle="/tmp/${bundle_file}"
  run scp "${SSH_OPTS[@]}" "$BUNDLE_PATH" "${ssh_target}:${remote_bundle}"

  printf -v q_bundle '%q' "$remote_bundle"
  printf -v q_repo '%q' "$remote_repo"
  printf -v q_bundle_id '%q' "$bundle_id"
  printf -v q_preflight '%q' "$PREFLIGHT_SCRIPT_REL"
  printf -v q_skip_preflight '%q' "$SKIP_PREFLIGHT"
remote_cmd="$(cat <<EOF
set -euo pipefail
BUNDLE_PATH=${q_bundle}
REMOTE_REPO=${q_repo}
BUNDLE_ID=${q_bundle_id}
PREFLIGHT_REL=${q_preflight}
SKIP_PREFLIGHT=${q_skip_preflight}
DEPLOY_ROOT="\${HOME}/.harnesseng_worker_runtime"
STAGE_DIR="\${DEPLOY_ROOT}/staging/\${BUNDLE_ID}"
SRC_DIR="\${STAGE_DIR}/harnesseng_runtime_bundle"
LOCK_DIR="\${DEPLOY_ROOT}/deploy.lock"

mkdir -p "\${DEPLOY_ROOT}" "\$(dirname "\${STAGE_DIR}")"
if ! mkdir "\${LOCK_DIR}" 2>/dev/null; then
  echo "deploy lock already held: \${LOCK_DIR}" >&2
  exit 1
fi
trap 'rmdir "\${LOCK_DIR}"' EXIT

rm -rf "\${STAGE_DIR}"
mkdir -p "\${STAGE_DIR}"
tar -xzf "\${BUNDLE_PATH}" -C "\${STAGE_DIR}"
test -d "\${SRC_DIR}"
mkdir -p "\${REMOTE_REPO}"
command -v rsync >/dev/null 2>&1

for rel in \
  blocks \
  runner \
  tools \
  tests \
  tracking/collab \
  tracking/collab/autonomous_loop \
  tracking/collab/eval_suite_v1_build \
  tracking/collab/stage_03_execution_planning \
  tracking/collab/final_harness_eval_suite; do
  if [[ -e "\${REMOTE_REPO}/\${rel}" ]]; then
    sudo chown -R "${vm_user}:${vm_user}" "\${REMOTE_REPO}/\${rel}" || true
  fi
done
find "\${REMOTE_REPO}" -name '._*' -delete || true

rsync -a --delete \
  --no-owner \
  --no-group \
  --filter='P .git/' \
  --filter='P .venv/' \
  --filter='P venv/' \
  --filter='P tracking/ledger/' \
  --filter='P tracking/collab/eval_suite_v1_tournament_runs/' \
  --filter='P tracking/collab/eval_suite_v1_baseline/certified_runs/' \
  --filter='P tracking/ledger/inbox/' \
  "\${SRC_DIR}/" "\${REMOTE_REPO}/"

echo "\${BUNDLE_ID}" > "\${DEPLOY_ROOT}/last_bundle_id"
rm -f "\${BUNDLE_PATH}"

if [[ "\${SKIP_PREFLIGHT}" != "1" ]]; then
  test -f "\${REMOTE_REPO}/\${PREFLIGHT_REL}"
  REPO="\${REMOTE_REPO}" ENV_FILE="\${HOME}/.harnesseng_eval_suite_v1.env" \
    bash "\${REMOTE_REPO}/\${PREFLIGHT_REL}"
fi
EOF
)"

  run ssh "${SSH_OPTS[@]}" "$ssh_target" "$remote_cmd"
  log "worker ${worker_id} deployed bundle ${bundle_id}"
done

log "deployment complete for ${#WORKER_IDS[@]} worker(s)"
