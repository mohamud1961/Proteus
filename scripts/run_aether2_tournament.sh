#!/usr/bin/env bash
set -uo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/run_aether2_tournament.sh [options]

Launcher for running a task list through a child entrypoint.

Options:
  --repo-root PATH         Repo root to place on PYTHONPATH (default: script parent)
  --entrypoint PATH        Child entrypoint to execute for each task launch
  --task-ids-file PATH     File with one task id per line
  --task-root PATH         Task root passed to the child entrypoint
  --output-root PATH       Output root for logs and rows
  --attempt N              Attempt number recorded in progress.tsv (default: 1)
  --agent-timeout-sec N    Forwarded agent timeout (default: 1800)
  --test-timeout-sec N     Forwarded test timeout (default: 2400)
  --per-task-timeout-sec N Wall-clock timeout per launch (default: 5400)
  --fail-fast-count N      Abort after this many consecutive fast nonzero launches (default: 5)
  --fail-fast-elapsed-sec N Count launches as fast when elapsed seconds are <= this threshold (default: 2)
  --dry-run                Print planned commands; do not launch tasks or create output files
  --help, -h               Show this help

Behavior:
  1. Exports PYTHONPATH=<repo-root> for child processes.
  2. Runs an import preflight before task corpus access.
  3. Records rc and elapsed time for each launch.
  4. Stops after N consecutive fast nonzero launches.
  5. Writes explicit invalid_launch marker rows when no row.json is produced.

Exit codes:
  0  completed or dry-run
  2  preflight import failed
  3  fail-fast threshold reached
  64 usage error
EOF
}

log() {
  printf '[run-aether2-tournament] %s\n' "$*"
}

die() {
  printf '[run-aether2-tournament] ERROR: %s\n' "$*" >&2
  exit "${2:-64}"
}

latest_row_json() {
  local task_id="$1"
  find "$OUTPUT_ROOT" -type f -path "*/$task_id/row.json" -print 2>/dev/null | sort | tail -n1
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3.11}"

ENTRYPOINT=""
TASK_IDS_FILE=""
TASK_ROOT="/home/azureuser/terminal-bench-official/original-tasks"
OUTPUT_ROOT=""
ATTEMPT="1"
AGENT_TIMEOUT_SEC="1800"
TEST_TIMEOUT_SEC="2400"
PER_TASK_TIMEOUT_SEC="5400"
FAIL_FAST_COUNT="5"
FAIL_FAST_ELAPSED_SEC="2"
DRY_RUN="0"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-root) REPO_ROOT="$2"; shift 2 ;;
    --entrypoint) ENTRYPOINT="$2"; shift 2 ;;
    --task-ids-file) TASK_IDS_FILE="$2"; shift 2 ;;
    --task-root) TASK_ROOT="$2"; shift 2 ;;
    --output-root) OUTPUT_ROOT="$2"; shift 2 ;;
    --attempt) ATTEMPT="$2"; shift 2 ;;
    --agent-timeout-sec) AGENT_TIMEOUT_SEC="$2"; shift 2 ;;
    --test-timeout-sec) TEST_TIMEOUT_SEC="$2"; shift 2 ;;
    --per-task-timeout-sec) PER_TASK_TIMEOUT_SEC="$2"; shift 2 ;;
    --fail-fast-count) FAIL_FAST_COUNT="$2"; shift 2 ;;
    --fail-fast-elapsed-sec) FAIL_FAST_ELAPSED_SEC="$2"; shift 2 ;;
    --dry-run) DRY_RUN="1"; shift ;;
    --help|-h) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ -d "$REPO_ROOT" ]] || die "repo root not found: $REPO_ROOT"

export PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PREFLIGHT_CMD=("$PYTHON_BIN" -c "import runner.aether2.bridge_harbor")

log "PYTHONPATH=$PYTHONPATH"
if [[ "$DRY_RUN" == "1" ]]; then
  log "dry-run preflight: ${PREFLIGHT_CMD[*]}"
else
  log "preflight: ${PREFLIGHT_CMD[*]}"
fi

if ! "${PREFLIGHT_CMD[@]}"; then
  die "preflight import failed; no task corpus was touched" 2
fi

if [[ -z "$TASK_IDS_FILE" ]]; then
  if [[ "$DRY_RUN" == "1" ]]; then
    log "dry-run preflight complete"
    exit 0
  fi
  die "--task-ids-file is required"
fi
[[ -f "$TASK_IDS_FILE" ]] || die "--task-ids-file not found: $TASK_IDS_FILE"

if [[ -z "$OUTPUT_ROOT" ]]; then
  if [[ "$DRY_RUN" == "1" ]]; then
    OUTPUT_ROOT="<output-root>"
  else
    die "--output-root is required"
  fi
fi

if [[ -z "$ENTRYPOINT" ]]; then
  if [[ "$DRY_RUN" == "1" ]]; then
    ENTRYPOINT="<entrypoint>"
  else
    die "--entrypoint is required; no canonical default entrypoint is wired in this launcher"
  fi
fi

if [[ "$DRY_RUN" != "1" ]]; then
  [[ -f "$ENTRYPOINT" ]] || die "entrypoint not found: $ENTRYPOINT"
  mkdir -p "$OUTPUT_ROOT/logs"
fi

PROGRESS_TSV="$OUTPUT_ROOT/progress.tsv"
INVALID_LAUNCHES_TSV="$OUTPUT_ROOT/invalid_launches.tsv"
consecutive_fast_failures=0

while IFS= read -r task_id || [[ -n "$task_id" ]]; do
  task_id="${task_id%$'\r'}"
  [[ -z "$task_id" ]] && continue
  [[ "$task_id" == \#* ]] && continue

  SAFE_TASK="$(printf '%s' "$task_id" | tr -c 'A-Za-z0-9_.-' '_')"
  TASK_LOG="$OUTPUT_ROOT/logs/attempt_${ATTEMPT}_${SAFE_TASK}.log"

  CMD=(timeout --foreground "$PER_TASK_TIMEOUT_SEC" "$PYTHON_BIN" "$ENTRYPOINT"
       --task-id "$task_id"
       --task-root "$TASK_ROOT"
       --output-root "$OUTPUT_ROOT"
       --agent-timeout-sec "$AGENT_TIMEOUT_SEC"
       --test-timeout-sec "$TEST_TIMEOUT_SEC")

  if [[ "$DRY_RUN" == "1" ]]; then
    log "dry-run would run: ${CMD[*]} > $TASK_LOG 2>&1"
    continue
  fi

  log "attempt=$ATTEMPT task=$task_id start $(date -u)"
  START_TS="$(date +%s)"
  "${CMD[@]}" > "$TASK_LOG" 2>&1
  RC="$?"
  END_TS="$(date +%s)"
  ELAPSED="$((END_TS - START_TS))"

  printf "%s\t%s\t%s\t%s\t%s\n" "$ATTEMPT" "$task_id" "$RC" "$ELAPSED" "$(date -u)" >> "$PROGRESS_TSV"
  log "attempt=$ATTEMPT task=$task_id end rc=$RC elapsed=${ELAPSED}s $(date -u)"

  ROW_JSON="$(latest_row_json "$task_id")"
  if [[ -z "$ROW_JSON" ]]; then
    printf "%s\t%s\t%s\t%s\t%s\t%s\n" "$ATTEMPT" "$task_id" "$RC" "$ELAPSED" "invalid_launch" "$(date -u)" >> "$INVALID_LAUNCHES_TSV"
    log "attempt=$ATTEMPT task=$task_id recorded invalid_launch"
  fi

  if [[ "$RC" != "0" && "$ELAPSED" -le "$FAIL_FAST_ELAPSED_SEC" ]]; then
    consecutive_fast_failures=$((consecutive_fast_failures + 1))
  else
    consecutive_fast_failures=0
  fi

  if [[ "$consecutive_fast_failures" -ge "$FAIL_FAST_COUNT" ]]; then
    die "fail-fast threshold reached after $consecutive_fast_failures consecutive fast nonzero launches" 3
  fi
done < "$TASK_IDS_FILE"

if [[ "$DRY_RUN" == "1" ]]; then
  log "dry-run complete"
else
  log "tournament complete"
fi
