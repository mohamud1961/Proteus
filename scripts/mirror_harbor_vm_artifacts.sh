#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/mirror_harbor_vm_artifacts.sh [options]

Mirror selected Harbor/Aether run artifacts from an Azure VM without opening
an interactive SSH session. The remote command is executed with:

  az vm run-command invoke --command-id RunShellScript ...

Options:
  --resource-group NAME   Azure resource group
  --vm-name NAME          Azure VM name
  --remote-root PATH      Remote repository or evidence root
  --destination PATH      Local destination directory
  --subscription ID       Optional Azure subscription override
  --dry-run               Print the planned command only
  --help, -h              Show this help

This script only prepares and invokes the Azure run-command transport. It does
not start or deallocate the VM.
EOF
}

log() {
  printf '[mirror-harbor-vm-artifacts] %s\n' "$*"
}

die() {
  printf '[mirror-harbor-vm-artifacts] ERROR: %s\n' "$*" >&2
  exit 1
}

RESOURCE_GROUP="${HARNESSENG_AZURE_RESOURCE_GROUP:-${AZURE_RESOURCE_GROUP:-}}"
VM_NAME="${HARNESSENG_AZURE_VM_NAME:-${AZURE_VM_NAME:-}}"
SUBSCRIPTION="${HARNESSENG_AZURE_SUBSCRIPTION:-${AZURE_SUBSCRIPTION:-}}"
REMOTE_ROOT=""
DESTINATION=""
DRY_RUN="0"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --resource-group) RESOURCE_GROUP="$2"; shift 2 ;;
    --vm-name) VM_NAME="$2"; shift 2 ;;
    --remote-root) REMOTE_ROOT="$2"; shift 2 ;;
    --destination) DESTINATION="$2"; shift 2 ;;
    --subscription) SUBSCRIPTION="$2"; shift 2 ;;
    --dry-run) DRY_RUN="1"; shift ;;
    --help|-h) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ -n "$RESOURCE_GROUP" ]] || die "missing resource group"
[[ -n "$VM_NAME" ]] || die "missing VM name"
[[ -n "$REMOTE_ROOT" ]] || die "missing --remote-root"
[[ -n "$DESTINATION" ]] || die "missing --destination"

remote_script="test -d '$REMOTE_ROOT' && tar -C '$REMOTE_ROOT' -czf /tmp/harnesseng-artifacts.tgz ."
cmd=(
  az vm run-command invoke
  --resource-group "$RESOURCE_GROUP"
  --name "$VM_NAME"
  --command-id RunShellScript
  --scripts "$remote_script"
  --output json
)
if [[ -n "$SUBSCRIPTION" ]]; then
  cmd+=(--subscription "$SUBSCRIPTION")
fi

if [[ "$DRY_RUN" == "1" ]]; then
  log "DRY_RUN: ${cmd[*]}"
  log "destination: $DESTINATION"
  exit 0
fi

command -v az >/dev/null 2>&1 || die "az not found"
mkdir -p "$DESTINATION"
"${cmd[@]}"
log "Remote archive prepared. Retrieval remains an explicit operator step."
