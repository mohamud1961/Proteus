#!/usr/bin/env bash
# Usage: scripts/run_benchmark_batch_20260709.sh <run_id>
set -euo pipefail

RUN_ID="${1:-20260709T000000Z_batch_15task_D16}"
IP="74.249.212.125"
RUN_ROOT="/home/azureuser/harnesseng_vm/aether_next_build/vm_goal_runs/${RUN_ID}"

log() { printf '[run-benchmark-batch] %s\n' "$*"; }

log "Creating run root directory: ${RUN_ROOT}"
ssh -i ~/.ssh/id_rsa azureuser@$IP "mkdir -p '${RUN_ROOT}'"

TASKS="log-summary-date-ranges gcode-to-text video-processing kv-store-grpc code-from-image openssl-selfsigned-cert fix-git git-multibranch regex-log write-compressor nginx-request-logging headless-terminal qemu-startup crack-7z-hash train-fasttext"

for task in $TASKS; do
  log "Launching task: ${task}"
  ssh -i ~/.ssh/id_rsa azureuser@$IP "
    set -eu
    mkdir -p '${RUN_ROOT}/${task}/traces' '${RUN_ROOT}/${task}/snapshots'
    source /home/azureuser/.aether2/model.env

    # Export PATH to include local bin just in case
    export PATH=/home/azureuser/.local/bin:\$PATH

    nohup python3 /home/azureuser/harnesseng_vm/aether_next_build/run_pilot.py \
      --tasks '${task}' \
      --max-steps 500 \
      --vision-deploy-env AZURE_OPENAI_GPT54_MINI_DEPLOYMENT \
      --trace-dir '${RUN_ROOT}/${task}/traces' \
      --snapshot-dir '${RUN_ROOT}/${task}/snapshots' \
      --out '${RUN_ROOT}/${task}/results.json' \
      > '${RUN_ROOT}/${task}/run.log' 2>&1 &

    echo \$! > '${RUN_ROOT}/${task}/pid'
  "
done

log "All 15 tasks have been launched in the background on the VM."
