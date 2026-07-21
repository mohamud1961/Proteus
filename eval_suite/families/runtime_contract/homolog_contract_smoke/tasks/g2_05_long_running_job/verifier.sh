#!/usr/bin/env bash
# Deterministic check for g2_05_long_running_job: done.txt must exist with
# expected content.
set -u
WORKSPACE="${1:-.}"

file="$WORKSPACE/done.txt"
if [ ! -f "$file" ]; then
  echo "FAIL: $file does not exist"
  exit 1
fi

if ! grep -q "job complete" "$file"; then
  echo "FAIL: unexpected content in done.txt"
  exit 1
fi

echo "PASS: done.txt contains expected content"
exit 0
