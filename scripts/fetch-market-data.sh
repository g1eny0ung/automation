#!/usr/bin/env bash
# Read-only input collection; no AI generation or report writes.
set -euo pipefail
: "${MARKET_API_TOKEN:?MARKET_API_TOKEN is required}"
response_dir=$(mktemp -d)
trap 'rm -rf "$response_dir"' EXIT

fetch() {
  local endpoint="$1" output="$2" code
  code=$(curl --silent --show-error --connect-timeout 15 --max-time 180 \
    --header "Authorization: Bearer ${MARKET_API_TOKEN}" \
    --output "$output" --write-out '%{http_code}' \
    "https://stock-analysis-umber.vercel.app${endpoint}")
  if [ "$code" != '200' ]; then
    echo "Failed to fetch ${endpoint} (HTTP ${code})" >&2
    return 1
  fi
}

fetch '/api/macro/en' "$response_dir/macro.json"
fetch '/api/market-performance/en' "$response_dir/us.json"
fetch '/api/market-performance/zh' "$response_dir/china.json"

# Macro is a record dictionary; performance endpoints return snapshot envelopes.
# Preserve dates, source and null metrics instead of inventing missing values.
jq -e -n \
  --slurpfile macro "$response_dir/macro.json" \
  --slurpfile us "$response_dir/us.json" \
  --slurpfile china "$response_dir/china.json" '
  def performance_valid:
    (.data.benchmark.date | type == "string") and
    (.data.tickers | type == "array" and length > 0);
  if ($macro[0] | type == "object" and length > 0) and
     ($us[0] | performance_valid) and ($china[0] | performance_valid)
  then {macro: $macro[0], us_market: $us[0], china_market: $china[0]}
  else error("Unexpected market API response shape") end
'
