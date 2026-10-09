#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
python "$root/scripts/breadth_bundle.py" check
cd "$root/producer"
uv sync --locked --python 3.14
