#!/bin/bash
# Checks the monitor's heartbeat and pings MONITOR_PING_URL only while it is fresh.
# Exits non-zero when the heartbeat is missing or stale, leaving the ping unsent.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HEARTBEAT="${MONEYMAKER_HEARTBEAT:-$REPO/data/monitor.heartbeat}"
ENV_FILE="${MONEYMAKER_ENV:-$REPO/.env}"
STALE_AFTER="${MONEYMAKER_STALE_AFTER:-180}"
SERVICE="moneymaker-monitor"
QUIET=0

usage() {
    cat >&2 <<EOF
usage: $0 [--quiet]
Checks $HEARTBEAT is younger than $STALE_AFTER seconds, then GETs MONITOR_PING_URL.
Environment: MONEYMAKER_HEARTBEAT, MONEYMAKER_ENV, MONEYMAKER_STALE_AFTER, MONITOR_PING_URL.
EOF
    exit 2
}

[ $# -eq 0 ] || { [ "$1" = "--quiet" ] && QUIET=1 || usage; }

# Prints a message unless --quiet was given.
say() { [ "$QUIET" -eq 1 ] || printf '%s\n' "$*"; }

# Prints a message to stderr and exits 1.
unhealthy() { printf 'unhealthy: %s\n' "$*" >&2; exit 1; }

# Prints the modification time of $1 in epoch seconds on GNU or BSD stat.
mtime() { stat -c %Y -- "$1" 2>/dev/null || stat -f %m -- "$1"; }

# Prints MONITOR_PING_URL from the environment, falling back to ENV_FILE.
ping_url() {
    local url="${MONITOR_PING_URL:-}"
    if [ -z "$url" ] && [ -f "$ENV_FILE" ]; then
        url="$(sed -n 's/^[[:space:]]*MONITOR_PING_URL[[:space:]]*=[[:space:]]*//p' "$ENV_FILE" \
            | tail -n 1)"
        url="${url%\"}"; url="${url#\"}"; url="${url%\'}"; url="${url#\'}"
    fi
    printf '%s' "$url"
}

[ -f "$HEARTBEAT" ] || unhealthy "no heartbeat at $HEARTBEAT; the monitor has not run"
age=$(( $(date -u +%s) - $(mtime "$HEARTBEAT") ))
[ "$age" -le "$STALE_AFTER" ] || unhealthy "heartbeat is ${age}s old (limit ${STALE_AFTER}s)"

if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files "$SERVICE.service" \
        --no-legend 2>/dev/null | grep -q .; then
    systemctl is-active --quiet "$SERVICE" || unhealthy "$SERVICE is not active"
fi

say "healthy: heartbeat ${age}s old ($(cat "$HEARTBEAT"))"
url="$(ping_url)"
if [ -z "$url" ]; then
    say "no MONITOR_PING_URL set; not pinging"
    exit 0
fi
curl -fsS -m 10 --retry 2 -o /dev/null -- "$url" || unhealthy "ping to MONITOR_PING_URL failed"
say "pinged MONITOR_PING_URL"
