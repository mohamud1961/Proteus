#!/usr/bin/env bash
# Deterministic check for g2_03_interactive_session: result.txt must contain
# 42 AND the harness-authored verifier context must prove a real persistent
# interactive session was started, used, and left registered at finalize time.
set -u
WORKSPACE="${1:-.}"
VERIFY_CONTEXT="${2:-}"

file="$WORKSPACE/result.txt"
if [ ! -f "$file" ]; then
  echo "FAIL: $file does not exist"
  exit 1
fi

content="$(tr -d '[:space:]' < "$file")"
if [ "$content" != "42" ]; then
  echo "FAIL: unexpected content: $content"
  exit 1
fi

if [ -z "$VERIFY_CONTEXT" ] || [ ! -f "$VERIFY_CONTEXT" ]; then
  echo "FAIL: verifier context missing: $VERIFY_CONTEXT"
  exit 1
fi

python3 - "$VERIFY_CONTEXT" <<'PY'
import json
import sys
from pathlib import Path

context_path = Path(sys.argv[1])
data = json.loads(context_path.read_text(encoding="utf-8"))
run_result = data.get("run_result") or {}
if run_result.get("session_survival") is not True:
    print("FAIL: session_survival was not true in verifier context")
    raise SystemExit(1)

tool_invocations = data.get("tool_invocations") or []
session_ids: set[str] = set()
send_ids: set[str] = set()
read_ids: set[str] = set()
observed_42 = False

for record in tool_invocations:
    tool_name = record.get("tool_name")
    arguments = record.get("arguments") or {}
    envelope = record.get("envelope") or {}
    session_id = str(arguments.get("session_id") or "").strip()
    if tool_name == "session_start" and envelope.get("exit_code") == 0 and session_id:
        session_ids.add(session_id)
    elif tool_name == "session_send" and envelope.get("exit_code") == 0 and session_id:
        send_ids.add(session_id)
    elif tool_name == "session_read" and session_id:
        read_ids.add(session_id)
        screen = f"{envelope.get('stdout_head', '')}{envelope.get('stdout_tail', '')}"
        if "42" in screen:
            observed_42 = True

usable_ids = sorted(session_ids & send_ids & read_ids)
if not usable_ids:
    print("FAIL: verifier context does not show a successful session_start/session_send/session_read sequence")
    raise SystemExit(1)
if not observed_42:
    print("FAIL: verifier context never observed 42 through session_read")
    raise SystemExit(1)

print(f"PASS: result.txt contains 42 and interactive session evidence is present for {usable_ids[0]}")
PY
