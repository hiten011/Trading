#!/usr/bin/env bash
# Run YOUR indicator over the market and print the alert instead of sending it.
#   ./scripts/dry-run.sh                       whole market
#   ./scripts/dry-run.sh RELIANCE,TCS,INFY     just these
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -n "${1:-}" ]]; then
  exec docker compose run --rm --no-deps alerts --once --dry-run --symbols "$1"
fi
exec docker compose run --rm --no-deps alerts --once --dry-run
