#!/usr/bin/env bash
# Minimal gate for ticket 9998.
set -euo pipefail
echo "gate: checking files..."
test -f README.md
echo "gate: OK"
