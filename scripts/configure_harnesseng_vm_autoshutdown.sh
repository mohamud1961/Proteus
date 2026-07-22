#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/configure_harnesseng_vm_autoshutdown.sh [options]

Configure auto-shutdown policy for the Azure VM.

Options:
  --resource-group NAME   Azure resource group (default: $HARNESSENG_AZURE_RESOURCE_GROUP or $AZURE_RESOURCE_GROUP)
  --vm-name NAME          Azure VM name (default: $HARNESSENG_AZURE_VM_NAME or $AZURE_VM_NAME)
  --time HHMM             Shutdown time in 24h UTC format (default: $HARNESSENG_VM_AUTOSHUTDOWN_TIME or 2300)
  --subscription ID       Optional Azure subscription override
  --dry-run               Print planned actions only
  --help, -h              Show this help

Planned command:
  az vm auto-shutdown --resource-group <resource-group> --name <vm-name> --time <HHMM>
EOF
}

log() {
  printf '[configure-harnesseng-vm-autoshutdown] %s\n' "$*"
}

die() {
  printf '[configure-harnesseng-vm-autoshutdown] ERROR: %s\n' "$*" >&2
  exit 1
}

run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    log "DRY_RUN: $*"
    return 0
  fi
  "$@"
}

RESOURCE_GROUP="${HARNESSENG_AZURE_RESOURCE_GROUP:-${AZURE_RESOURCE_GROUP:-}}"
VM_NAME="${HARNESSENG_AZURE_VM_NAME:-${AZURE_VM_NAME:-}}"
AUTO_SHUTDOWN_TIME="${HARNESSENG_VM_AUTOSHUTDOWN_TIME:-2300}"
SUBSCRIPTION="${HARNESSENG_AZURE_SUBSCRIPTION:-${AZURE_SUBSCRIPTION:-}}"
DRY_RUN="0"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --resource-group) RESOURCE_GROUP="$2"; shift 2 ;;
    --vm-name) VM_NAME="$2"; shift 2 ;;
    --time) AUTO_SHUTDOWN_TIME="$2"; shift 2 ;;
    --subscription) SUBSCRIPTION="$2"; shift 2 ;;
    --dry-run) DRY_RUN="1"; shift ;;
    --help|-h) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ -n "$RESOURCE_GROUP" ]] || die "missing resource group (set HARNESSENG_AZURE_RESOURCE_GROUP or AZURE_RESOURCE_GROUP)"
[[ -n "$VM_NAME" ]] || die "missing VM name (set HARNESSENG_AZURE_VM_NAME or AZURE_VM_NAME)"
[[ "$AUTO_SHUTDOWN_TIME" =~ ^[0-9]{4}$ ]] || die "--time must be HHMM"

if [[ "$DRY_RUN" != "1" ]]; then
  command -v az >/dev/null 2>&1 || die "az not found"
fi

cmd=(az vm auto-shutdown --resource-group "$RESOURCE_GROUP" --name "$VM_NAME" --time "$AUTO_SHUTDOWN_TIME")
if [[ -n "$SUBSCRIPTION" ]]; then
  cmd+=(--subscription "$SUBSCRIPTION")
fi

run "${cmd[@]}"
