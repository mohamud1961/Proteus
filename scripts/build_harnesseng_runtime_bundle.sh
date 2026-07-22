#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/build_harnesseng_runtime_bundle.sh [options]

Create a clean runtime bundle for worker deployment without macOS metadata.

Options:
  --repo-root PATH        Repo root to bundle (default: current directory)
  --output-dir PATH       Bundle output directory
                          (default: /private/tmp/harnesseng_runtime_bundles)
  --bundle-id ID          Override generated bundle id
  --print-path-only       Print bundle path only (for scripting)
  --dry-run               Print planned actions only
  --help, -h              Show this help
EOF
}

log() {
  printf '[build-worker-runtime-bundle] %s\n' "$*"
}

die() {
  printf '[build-worker-runtime-bundle] ERROR: %s\n' "$*" >&2
  exit 1
}

run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    log "DRY_RUN: $*"
    return 0
  fi
  "$@"
}

REPO_ROOT="${PWD}"
OUTPUT_DIR="/private/tmp/harnesseng_runtime_bundles"
BUNDLE_ID=""
PRINT_PATH_ONLY="0"
DRY_RUN="0"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-root) REPO_ROOT="$2"; shift 2 ;;
    --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
    --bundle-id) BUNDLE_ID="$2"; shift 2 ;;
    --print-path-only) PRINT_PATH_ONLY="1"; shift ;;
    --dry-run) DRY_RUN="1"; shift ;;
    --help|-h) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

command -v rsync >/dev/null 2>&1 || die "rsync not found"
command -v tar >/dev/null 2>&1 || die "tar not found"
command -v python3 >/dev/null 2>&1 || die "python3 not found"

REPO_ROOT="$(cd "$REPO_ROOT" && pwd)"
[[ -d "$REPO_ROOT/.git" ]] || die "repo root missing .git directory: $REPO_ROOT"

if [[ -z "$BUNDLE_ID" ]]; then
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  short_rev="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || true)"
  if [[ -z "$short_rev" ]]; then
    short_rev="unknown"
  fi
  BUNDLE_ID="${stamp}_${short_rev}"
fi

bundle_name="harnesseng_runtime_${BUNDLE_ID}.tar.gz"
bundle_path="${OUTPUT_DIR}/${bundle_name}"
checksum_path="${bundle_path}.sha256"

stage_root="$(mktemp -d /private/tmp/harnesseng_runtime_bundle_stage.XXXXXX)"
trap 'rm -rf "$stage_root"' EXIT
stage_repo="${stage_root}/harnesseng_runtime_bundle"

run mkdir -p "$OUTPUT_DIR" "$stage_repo"

include_paths=(
  "blocks"
  "runner"
  "tools"
  "tests"
  "tracking/collab/autonomous_loop/eval_suite_v1_tournament_orchestration"
  "tracking/collab/eval_suite_v1_build"
  "tracking/collab/autonomous_loop/goal_1_recovery_and_next_unlocks"
  "tracking/collab/stage_03_execution_planning/packets/packet_04_first_atomic_variants/outputs"
  "tracking/collab/autonomous_loop/eval_suite_v1_certification_baseline_runs/20260522T184129Z_first_core_full_vm_copy/certified_core_baseline"
  "tracking/collab/autonomous_loop/single_family_winner_discovery_gate/long_horizon_artifact_handoff/fresh_certified_vm_rerun"
  "tracking/collab/certify_clean_tool_contract_diagnostic_family/certified_runs"
  "tracking/collab/eval_suite_v1_baseline/certified_runs/20260523T145906Z_tooling_family_gpt54mini"
  "tracking/collab/final_harness_eval_suite"
  "official_tasks"
)

# Aether-2 lives under runner/tools/tests, so the existing include list already bundles
# `runner/aether2/` and `tools/aether2_genericity_check.py` without needing extra paths.

rsync_common=(
  -a
  --delete
  --exclude '.git/'
  --exclude '.venv/'
  --exclude 'venv/'
  --exclude '.pytest_cache/'
  --exclude '__pycache__/'
  --exclude '*.pyc'
  --exclude '.DS_Store'
  --exclude '._*'
  --exclude '__MACOSX/'
  --exclude 'runs/'
  --exclude 'vm_pulled_runs/'
  --exclude '.tmp_codex_home/'
)

for rel in "${include_paths[@]}"; do
  src="${REPO_ROOT}/${rel}"
  [[ -e "$src" ]] || die "required runtime path missing: $src"
  dst_parent="${stage_repo}/$(dirname "$rel")"
  run mkdir -p "$dst_parent"
  run rsync "${rsync_common[@]}" "$src" "$dst_parent/"
done

manifest_path="${stage_repo}/tracking/collab/autonomous_loop/eval_suite_v1_tournament_orchestration/worker_runtime_bundle_manifest.json"
run mkdir -p "$(dirname "$manifest_path")"

git_rev="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || printf 'unknown')"
git_dirty="clean"
if [[ -n "$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null)" ]]; then
  git_dirty="dirty"
fi

if [[ "$DRY_RUN" != "1" ]]; then
  python3 - "$manifest_path" "$BUNDLE_ID" "$git_rev" "$git_dirty" "$REPO_ROOT" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

manifest_path, bundle_id, git_rev, git_dirty, repo_root = sys.argv[1:6]
payload = {
    "schema_version": "harnesseng_worker_runtime_bundle.v1",
    "bundle_id": bundle_id,
    "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "source_repo_root": repo_root,
    "git_revision": git_rev,
    "git_tree_state": git_dirty,
}
Path(manifest_path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
fi

if [[ "$DRY_RUN" == "1" ]]; then
  log "DRY_RUN complete: ${bundle_path}"
  if [[ "$PRINT_PATH_ONLY" == "1" ]]; then
    printf '%s\n' "$bundle_path"
  fi
  exit 0
fi

(
  cd "$stage_root"
  COPYFILE_DISABLE=1 tar -czf "$bundle_path" harnesseng_runtime_bundle
)

shasum -a 256 "$bundle_path" | awk '{print $1}' > "$checksum_path"

if [[ "$PRINT_PATH_ONLY" == "1" ]]; then
  printf '%s\n' "$bundle_path"
else
  log "bundle: $bundle_path"
  log "sha256: $(cat "$checksum_path")"
fi
