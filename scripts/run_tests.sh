#!/usr/bin/env bash
set -euo pipefail
python3 -m pytest -q tests aether_next_build/tests --ignore=aether_next_build/tests/v5_ported
