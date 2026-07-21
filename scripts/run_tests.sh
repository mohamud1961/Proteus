#!/usr/bin/env bash
set -euo pipefail
python3 -m pytest -q tests --ignore=tests/aether_next/v5_ported
