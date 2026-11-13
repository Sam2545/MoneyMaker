#!/bin/bash
# Installs and manages the moneymaker-monitor systemd service on an Ubuntu VPS. Run with sudo.
# The AI discovery pipeline (moneymaker match) is not scheduled; run it manually.
set -euo pipefail

SERVICE="moneymaker-monitor"
APP_USER="moneymaker"
APP_HOME="/home/$APP_USER"
REPO="$APP_HOME/MoneyMaker"
UV="$APP_HOME/.local/bin/uv"
UV_CACHE="$APP_HOME/.cache/uv"
UNIT="/etc/systemd/system/$SERVICE.service"
TEMPLATE="deploy/$SERVICE.service"
SCRIPT_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MONITOR_ARGS="${MONITOR_ARGS:---scope crypto --match btc|bitcoin|eth|sol|xrp}"
PACKAGES=(git curl ca-certificates openssh-client sqlite3 rsync)

usage() {
    cat >&2 <<EOF
usage: sudo $0 <command>
  install [SOURCE] [--move]  set up user, uv, repo and service; SOURCE is a git URL or a checkout
                             path (copied, or moved with --move); omit it if $REPO exists
  status                     service state and the last 20 journal lines
  logs                       follow the service journal
  update [PATH]              git pull (or rsync from PATH), uv sync, re-render the unit, restart
  restart                    restart the service
  uninstall                  stop, disable and remove the unit; keeps the repo and data
  render DIR                 write the rendered unit to DIR for inspection (no sudo needed)
Set MONITOR_ARGS to change the monitor arguments (default: $MONITOR_ARGS).
EOF
    exit 2
}

# Prints a progress message.
say() { printf '==> %s\n' "$*"; }

# Prints an error and exits.
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# Exits unless running as root.
require_root() { [ "$(id -u)" -eq 0 ] || die "run with sudo: sudo $0 ${1:-}"; }

# Runs a command as the app user from its home directory with uv on PATH.
as_app() {
    (cd "$APP_HOME" && sudo -u "$APP_USER" -H \
        env PATH="$APP_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" UV_CACHE_DIR="$UV_CACHE" "$@")
}

# Installs the apt packages that are missing.
install_packages() {
    local missing=() pkg
    for pkg in "${PACKAGES[@]}"; do
        dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
    done
    if [ ${#missing[@]} -eq 0 ]; then
        say "packages already installed: ${PACKAGES[*]}"
        return
    fi
    say "installing packages: ${missing[*]}"
    apt-get update -q
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q "${missing[@]}"
}

# Creates the app system user with a home directory if it does not exist.
create_user() {
    if id "$APP_USER" >/dev/null 2>&1; then
        say "user $APP_USER exists"
    else
        say "creating system user $APP_USER"
        useradd --system --create-home --home-dir "$APP_HOME" --shell /bin/bash --user-group \
            "$APP_USER"
    fi
    chmod 750 "$APP_HOME"
    install -d -m 700 -o "$APP_USER" -g "$APP_USER" "$APP_HOME/.cache" "$UV_CACHE"
}

# Installs uv into the app user's ~/.local/bin if it is missing.
install_uv() {
    if [ -x "$UV" ]; then
        say "uv present: $(as_app "$UV" --version)"
        return
    fi
    say "installing uv for $APP_USER"
    as_app sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
    [ -x "$UV" ] || die "uv was not installed at $UV"
}

# Creates an SSH key for the app user and prints it, returning 1 when the key is new.
ensure_deploy_key() {
    local key="$APP_HOME/.ssh/id_ed25519"
    [ -f "$key" ] && return 0
    install -d -m 700 -o "$APP_USER" -g "$APP_USER" "$APP_HOME/.ssh"
    as_app ssh-keygen -q -t ed25519 -N "" -C "$APP_USER@$(hostname)" -f "$key"
    say "created $key. Add this public key to the GitHub repo as a read-only deploy key"
    say "(Settings > Deploy keys > Add deploy key, leave 'Allow write access' off), then rerun:"
    cat "$key.pub"
    return 1
}

# Places the repo at REPO from a git URL or a local checkout path, unless it is already there.
place_repo() {
    local src="$1" move="$2"
    if [ -f "$REPO/pyproject.toml" ]; then
        say "repo already at $REPO (use 'update' to refresh it)"
        return
    fi
    [ -n "$src" ] || die "no repo at $REPO; pass a git URL or a checkout path to install"
    [ ! -e "$REPO" ] || [ -z "$(ls -A "$REPO")" ] || die "$REPO exists but is not a checkout"
    if [ -d "$src" ]; then
        src="$(cd "$src" && pwd)"
        [ -f "$src/pyproject.toml" ] || die "$src is not a MoneyMaker checkout"
        if [ "$move" = 1 ]; then
            say "moving $src to $REPO"
            rmdir "$REPO" 2>/dev/null || true
            mv "$src" "$REPO"
            rm -rf "$REPO/.venv"
        else
            say "copying $src to $REPO"
            install -d -o "$APP_USER" -g "$APP_USER" "$REPO"
            rsync -a --exclude /.venv "$src/" "$REPO/"
        fi
        chown -R "$APP_USER:$APP_USER" "$REPO"
    else
        case "$src" in
            git@*|ssh://*) ensure_deploy_key || exit 0 ;;
        esac
        say "cloning $src to $REPO"
        as_app env GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new" \
            git clone "$src" "$REPO"
    fi
}

# Creates data/logs and secrets with 700 permissions and tightens .env and secret files to 600.
setup_dirs() {
    install -d -m 700 -o "$APP_USER" -g "$APP_USER" "$REPO/data" "$REPO/data/logs" "$REPO/secrets"
    local f
    for f in "$REPO/.env" "$REPO"/secrets/*; do
        [ -f "$f" ] || continue
        chown "$APP_USER:$APP_USER" "$f"
        chmod 600 "$f"
    done
}

# Runs uv sync in the repo as the app user.
uv_sync() {
    say "uv sync"
    as_app "$UV" --directory "$REPO" sync --frozen
}

# Prints the unit from the template in repo $1, filled in with the user, paths and arguments.
render_unit() {
    local template="$1/$TEMPLATE" unit
    [ -f "$template" ] || die "missing $template"
    case "$MONITOR_ARGS" in
        *[\"\\%\$\']*|*$'\n'*) die "MONITOR_ARGS must not contain quotes, backslashes, % or \$" ;;
    esac
    shopt -u patsub_replacement 2>/dev/null || true
    unit="$(<"$template")"
    unit="${unit//@USER@/$APP_USER}"
    unit="${unit//@HOME@/$APP_HOME}"
    unit="${unit//@REPO@/$REPO}"
    unit="${unit//@MONITOR_ARGS@/$MONITOR_ARGS}"
    printf '%s\n' "$unit"
}

# Writes the rendered unit to DIR.
render() {
    mkdir -p "$1"
    render_unit "$SCRIPT_REPO" > "$1/$SERVICE.service"
    echo "$1/$SERVICE.service"
}

# Installs the rendered unit if it changed, reloads systemd and enables the service.
install_unit() {
    local tmp
    tmp="$(mktemp)"
    render_unit "$REPO" > "$tmp"
    if cmp -s "$tmp" "$UNIT"; then
        say "$UNIT is up to date"
        rm -f "$tmp"
    else
        say "writing $UNIT"
        install -m 644 "$tmp" "$UNIT"
        rm -f "$tmp"
    fi
    systemctl daemon-reload
    systemctl enable --quiet "$SERVICE"
    say "enabled $SERVICE (starts at boot)"
}

# Restarts the service when .env exists, otherwise prints the files to copy.
start_if_ready() {
    if [ -f "$REPO/.env" ]; then
        systemctl restart "$SERVICE"
        say "started $SERVICE; check it with: sudo $0 status"
    else
        say "not starting: $REPO/.env is missing. Copy these from your laptop, then run"
        say "'sudo $0 restart':"
        cat <<EOF
    .env               -> $REPO/.env
    secrets/kalshi.pem -> $REPO/secrets/kalshi.pem
    data/matches.db    -> $REPO/data/matches.db   (a sqlite3 .backup snapshot)
  each with: sudo install -m 600 -o $APP_USER -g $APP_USER FILE DEST
EOF
    fi
}

# Runs the full installation; safe to rerun.
install() {
    local src="" move=0 arg
    for arg in "$@"; do
        case "$arg" in
            --move) move=1 ;;
            -*) usage ;;
            *) [ -z "$src" ] || usage; src="$arg" ;;
        esac
    done
    install_packages
    create_user
    install_uv
    place_repo "$src" "$move"
    setup_dirs
    uv_sync
    install_unit
    start_if_ready
}

# Shows the service state and the last 20 journal lines.
status() {
    systemctl status "$SERVICE" --no-pager || true
    echo "--- last 20 journal lines"
    journalctl -u "$SERVICE" -n 20 --no-pager || true
}

# Pulls new code (or rsyncs it from a path), syncs dependencies, refreshes the unit and restarts.
update() {
    [ -f "$REPO/pyproject.toml" ] || die "no repo at $REPO; run install first"
    if [ $# -ge 1 ]; then
        [ -f "$1/pyproject.toml" ] || die "$1 is not a MoneyMaker checkout"
        say "syncing code from $1 (keeps .git, .venv, .env, secrets and data)"
        rsync -a --delete --exclude /.git --exclude /.venv --exclude /.env --exclude /secrets \
            --exclude /data "$1/" "$REPO/"
        chown -R "$APP_USER:$APP_USER" "$REPO"
    else
        say "git pull"
        as_app git -C "$REPO" pull --ff-only
    fi
    setup_dirs
    uv_sync
    install_unit
    start_if_ready
}

# Stops, disables and removes the unit, leaving the repo and data in place.
uninstall() {
    systemctl disable --now "$SERVICE" >/dev/null 2>&1 || true
    rm -f "$UNIT"
    systemctl daemon-reload
    systemctl reset-failed "$SERVICE" >/dev/null 2>&1 || true
    say "removed $SERVICE; $REPO and its data are untouched"
}

cmd="${1:-}"
[ $# -gt 0 ] && shift
case "$cmd" in
    install) require_root "$cmd"; install "$@" ;;
    status) require_root "$cmd"; status ;;
    logs) require_root "$cmd"; journalctl -u "$SERVICE" -f -n 50 ;;
    update) require_root "$cmd"; [ $# -le 1 ] || usage; update "$@" ;;
    restart) require_root "$cmd"; systemctl restart "$SERVICE"; say "restarted $SERVICE" ;;
    uninstall) require_root "$cmd"; uninstall ;;
    render) [ $# -eq 1 ] || usage; render "$1" ;;
    *) usage ;;
esac
