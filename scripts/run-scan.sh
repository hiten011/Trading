#!/usr/bin/env bash
# Run one of PKScreener's own scanners and print the result.
#   ./scripts/run-scan.sh              uses SCAN_OPTIONS from .env
#   ./scripts/run-scan.sh X:12:9       overrides it for this run
set -euo pipefail
cd "$(dirname "$0")/.."

OPTIONS="${1:-}"
if [[ -n "$OPTIONS" ]]; then
  exec docker compose --profile manual run --rm screener -a Y -e -o "$OPTIONS"
fi
exec docker compose --profile manual run --rm screener
