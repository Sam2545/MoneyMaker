#!/bin/bash
# Runs `uv run moneymaker <command> [args...]` from the repo root, appending its output to
# data/logs/<command>-YYYY-MM-DD.log (the date is taken per line) and forwarding SIGTERM/SIGINT.
set -uo pipefail

if [ $# -lt 1 ]; then
    echo "usage: $0 <moneymaker command> [args...]" >&2
    exit 2
fi

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
export PYTHONUNBUFFERED=1
job="$1"
mkdir -p data/logs

append_daily() {
    while IFS= read -r line; do
        printf '%s\n' "$line" >> "data/logs/${job}-$(date +%F).log"
    done
}

echo "=== $(date '+%F %T') start: moneymaker $*" | append_daily
uv run moneymaker "$@" > >(append_daily) 2>&1 &
child=$!
trap 'kill -TERM "$child" 2>/dev/null' TERM
trap 'kill -INT "$child" 2>/dev/null' INT
wait "$child"
status=$?
if kill -0 "$child" 2>/dev/null; then
    wait "$child"
    status=$?
fi
echo "=== $(date '+%F %T') exit $status: moneymaker $*" | append_daily
exit "$status"
