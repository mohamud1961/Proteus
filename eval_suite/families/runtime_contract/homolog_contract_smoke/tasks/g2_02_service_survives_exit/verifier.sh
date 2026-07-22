#!/usr/bin/env bash
# Deterministic check for g2_02_service_survives_exit: a server on port 8123
# must STILL be serving after the agent process tree has exited.
set -u

if command -v curl >/dev/null 2>&1; then
  body="$(curl -sS --max-time 5 http://127.0.0.1:8123/ 2>/dev/null)"
  status=$?
else
  body="$(python3 - <<'PY'
import urllib.request
try:
    print(urllib.request.urlopen("http://127.0.0.1:8123/", timeout=5).read().decode("utf-8", "replace"))
except Exception as exc:
    print(f"ERROR: {exc}")
PY
)"
  status=0
fi

if [ $status -ne 0 ]; then
  echo "FAIL: could not reach http://127.0.0.1:8123/"
  exit 1
fi

case "$body" in
  *ok*) echo "PASS: service still responding after agent exit: $body"; exit 0 ;;
  *) echo "FAIL: unexpected response body: $body"; exit 1 ;;
esac
