#!/usr/bin/env bash
set -euo pipefail
: "${RUNNER_TEMP:?RUNNER_TEMP is required}"
: "${GITLAB_DEPLOY_USER:?GITLAB_DEPLOY_USER is required}"
: "${GITLAB_DEPLOY_TOKEN:?GITLAB_DEPLOY_TOKEN is required}"
root=$(cd "$(dirname "$0")/.." && pwd)
revision=$(cat "$root/stock-analysis-revision.txt")
if [[ ! "$revision" =~ ^[0-9a-f]{40}$ ]]; then
  echo 'stock-analysis-revision.txt must contain a full commit SHA' >&2
  exit 1
fi
askpass=$(mktemp "$RUNNER_TEMP/breadth-askpass.XXXXXX")
trap 'rm -f "$askpass"' EXIT
cat > "$askpass" <<'ASKPASS'
#!/usr/bin/env bash
case "$1" in
  *Username*) printf '%s\n' "$GITLAB_DEPLOY_USER" ;;
  *Password*) printf '%s\n' "$GITLAB_DEPLOY_TOKEN" ;;
  *) exit 1 ;;
esac
ASKPASS
chmod 700 "$askpass"
export GIT_ASKPASS="$askpass" GIT_TERMINAL_PROMPT=0
checkout="$RUNNER_TEMP/stock-analysis"
git init -q "$checkout"
git -C "$checkout" remote add origin https://gitlab.com/g1eny0ung/stock_analysis.git
git -C "$checkout" -c credential.helper= fetch --quiet --depth=1 origin "$revision"
git -C "$checkout" checkout --quiet --detach FETCH_HEAD
actual=$(git -C "$checkout" rev-parse HEAD)
if [[ "$actual" != "$revision" ]]; then
  echo 'Producer checkout does not match the pinned revision' >&2
  exit 1
fi
printf 'Breadth producer revision %s\n' "$actual"
cd "$checkout"
uv sync --locked --python 3.14
