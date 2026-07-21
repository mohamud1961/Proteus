#!/usr/bin/env bash
# Deterministic check for g2_01_file_artifact: report.txt exists with the
# expected content. Run AFTER the agent loop has returned.
set -u
WORKSPACE="${1:-.}"

file="$WORKSPACE/report.txt"
if [ ! -f "$file" ]; then
  echo "FAIL: $file does not exist"
  exit 1
fi

content="$(cat "$file")"
if [ "$content" != "status: ready" ]; then
  echo "FAIL: unexpected content: $content"
  exit 1
fi

echo "PASS: report.txt contains expected content"
exit 0
