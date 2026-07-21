#!/usr/bin/env bash
# Deterministic check for g2_04_package_install: cowsay must be installed and
# cowsay_output.txt must contain its rendered output.
set -u
WORKSPACE="${1:-.}"

file="$WORKSPACE/cowsay_output.txt"
if [ ! -f "$file" ]; then
  echo "FAIL: $file does not exist"
  exit 1
fi

if ! grep -q "hello" "$file"; then
  echo "FAIL: cowsay_output.txt does not contain expected text"
  exit 1
fi

if ! python3 -c "import cowsay" >/dev/null 2>&1; then
  echo "FAIL: cowsay package is not importable"
  exit 1
fi

echo "PASS: cowsay installed and output produced"
exit 0
