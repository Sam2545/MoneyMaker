#!/bin/bash
# Takes a fresh snapshot on the VPS, copies it to this machine and verifies it locally.
# Run from the laptop: scripts/pull_backup.sh ubuntu@<ip> [DEST]
set -euo pipefail

TARGET="${1:-}"
DEST="${2:-${MONEYMAKER_BACKUP_DIR:-data/backups}}"
APP_USER="moneymaker"
REMOTE_REPO="/home/$APP_USER/MoneyMaker"

usage() {
    cat >&2 <<EOF
usage: $0 <ssh-target> [DEST]
  ssh-target  e.g. ubuntu@203.0.113.10
  DEST        local directory for the snapshot (default: $DEST)
Requires passwordless sudo on the VPS, as Oracle's ubuntu user has by default.
EOF
    exit 2
}

# Prints a progress message.
say() { printf '==> %s\n' "$*"; }

# Prints an error and exits.
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# Runs $* on the VPS as the app user from its repo.
on_vps() { ssh "$TARGET" "sudo -u $APP_USER -H bash -lc 'cd $REMOTE_REPO && $*'"; }

[ -n "$TARGET" ] || usage
case "$TARGET" in -*) usage ;; esac

say "snapshotting on $TARGET"
on_vps "scripts/backup_db.sh" >&2

newest="$(on_vps "ls -1t data/backups/matches-*.db.gz | head -n 1" | tr -d '\r')"
[ -n "$newest" ] || die "no snapshot found on $TARGET"
name="$(basename "$newest")"
install -d -m 700 "$DEST"

say "copying $name"
ssh "$TARGET" "sudo -u $APP_USER cat '$REMOTE_REPO/$newest'" > "$DEST/.$name.part"
mv "$DEST/.$name.part" "$DEST/$name"
chmod 600 "$DEST/$name"

gzip -t -- "$DEST/$name" || die "$DEST/$name is not a valid gzip file"
if command -v sqlite3 >/dev/null 2>&1; then
    check="$(gunzip -c -- "$DEST/$name" > "$DEST/.verify.db" \
        && sqlite3 "$DEST/.verify.db" 'pragma integrity_check;' | head -n 1)"
    rm -f -- "$DEST/.verify.db"
    [ "$check" = "ok" ] || die "integrity check failed on the copy: $check"
    say "verified: integrity_check ok"
fi
say "wrote $DEST/$name ($(du -h "$DEST/$name" | cut -f1))"
