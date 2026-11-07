#!/bin/bash
# Manages the com.moneymaker.monitor LaunchAgent: install, uninstall, status, or render DIR.
# The AI discovery pipeline (moneymaker match) is not scheduled; run it manually.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.moneymaker.monitor"
AGENTS="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"
MATCH="btc|bitcoin|eth|sol|xrp"

xml_escape() {
    printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}

render_plist() {
    local repo
    repo="$(xml_escape "$REPO")"
    cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${repo}/scripts/run_job.sh</string>
        <string>monitor</string>
        <string>--scope</string>
        <string>crypto</string>
        <string>--match</string>
        <string>$(xml_escape "$MATCH")</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${repo}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>ExitTimeOut</key>
    <integer>30</integer>
    <key>ProcessType</key>
    <string>Standard</string>
    <key>StandardOutPath</key>
    <string>${repo}/data/logs/launchd-monitor.log</string>
    <key>StandardErrorPath</key>
    <string>${repo}/data/logs/launchd-monitor.log</string>
</dict>
</plist>
EOF
}

render() {
    local dir="$1"
    mkdir -p "$dir"
    render_plist > "$dir/$LABEL.plist"
    plutil -lint "$dir/$LABEL.plist" >/dev/null
    echo "$dir/$LABEL.plist"
}

install() {
    mkdir -p "$REPO/data/logs"
    chmod +x "$REPO/scripts/run_job.sh"
    local plist
    plist="$(render "$AGENTS")"
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$plist"
    echo "installed and started $LABEL ($plist)"
}

uninstall() {
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$AGENTS/$LABEL.plist"
    echo "uninstalled $LABEL"
}

status() {
    if launchctl print "$DOMAIN/$LABEL" 2>/dev/null \
        | grep -E '^[[:space:]]*(state|pid|last exit code|runs) ='; then
        :
    else
        echo "$LABEL is not loaded"
    fi
    local log="$REPO/data/logs/monitor-$(date +%F).log"
    if [ -f "$log" ]; then
        echo "--- last heartbeat in $log"
        grep 'monitor.heartbeat' "$log" | tail -n 1 || true
    fi
}

case "${1:-}" in
    install) install ;;
    uninstall) uninstall ;;
    status) status ;;
    render)
        [ $# -ge 2 ] || { echo "usage: $0 render DIR" >&2; exit 2; }
        render "$2"
        ;;
    *)
        echo "usage: $0 {install|uninstall|status|render DIR}" >&2
        exit 2
        ;;
esac
