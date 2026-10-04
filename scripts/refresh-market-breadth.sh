#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo 'Usage: refresh-market-breadth.sh en|zh' >&2
  exit 1
fi
market="$1"
case "$market" in
  en|zh) ;;
  *) echo 'Market must be en or zh' >&2; exit 1 ;;
esac

: "${MARKET_API_TOKEN:?MARKET_API_TOKEN is required}"
response="${RUNNER_TEMP}/breadth-${market}.json"
code=$(curl --silent --show-error --connect-timeout 15 --max-time 900 \
  --request POST \
  --header "Authorization: Bearer ${MARKET_API_TOKEN}" \
  --output "$response" --write-out '%{http_code}' \
  "https://stock-analysis-umber.vercel.app/api/market-breadth/${market}")
echo "HTTP $code"
if [ "$code" != '200' ]; then
  echo '::error::Breadth refresh failed; the previous snapshot is retained.'
  exit 1
fi
jq '{refresh_date, data_date: .data.date, refreshed_at, partial: .data.partial}' "$response"
if jq -e '.data.date != .refresh_date' "$response" >/dev/null; then
  echo '::warning::Upstream data is behind the target trading day; inspect data_date.'
fi
