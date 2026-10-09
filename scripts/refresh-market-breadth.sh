#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
exec python "$root/scripts/run_market_breadth.py" "$@"
