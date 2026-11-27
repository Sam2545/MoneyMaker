#!/bin/bash
# Writes a verified, compressed snapshot of the match database and prunes old ones.
set -euo pipefail

DB="${MONEYMAKER_DB:-data/matches.db}"
DEST="${MONEYMAKER_BACKUP_DIR:-data/backups}"
KEEP="${MONEYMAKER_BACKUP_KEEP:-14}"

usage() {
    cat >&2 <<EOF
usage: $0 [--db PATH] [--dest DIR] [--keep N]
  --db PATH    database to snapshot (default: $DB)
  --dest DIR   directory for snapshots (default: $DEST)
  --keep N     number of snapshots to retain (default: $KEEP)
Environment: MONEYMAKER_DB, MONEYMAKER_BACKUP_DIR, MONEYMAKER_BACKUP_KEEP.
EOF
    exit 2
}

# Prints a progress message.
say() { printf '==> %s\n' "$*"; }

# Prints an error and exits.
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --db) [ $# -ge 2 ] || usage; DB="$2"; shift 2 ;;
        --dest) [ $# -ge 2 ] || usage; DEST="$2"; shift 2 ;;
        --keep) [ $# -ge 2 ] || usage; KEEP="$2"; shift 2 ;;
        *) usage ;;
    esac
done

case "$KEEP" in
    ''|*[!0-9]*) die "--keep must be a non-negative integer, got '$KEEP'" ;;
esac
command -v sqlite3 >/dev/null 2>&1 || die "sqlite3 is not installed"
[ -f "$DB" ] || die "no database at $DB"

# Snapshots DB to $1 and fails unless sqlite reports it as intact.
snapshot() {
    local out="$1" check
    sqlite3 "$DB" ".backup '$out'"
    check="$(sqlite3 "$out" 'pragma integrity_check;' | head -n 1)"
    [ "$check" = "ok" ] || die "integrity check on the snapshot failed: $check"
}

# Removes the oldest snapshots in DEST, keeping the KEEP newest by name.
prune() {
    local files=() n drop i
    while IFS= read -r f; do files+=("$f"); done < <(
        find "$DEST" -maxdepth 1 -name 'matches-*.db.gz' -type f | sort
    )
    n=${#files[@]}
    drop=$((n - KEEP))
    [ "$drop" -gt 0 ] || return 0
    say "pruning $drop of $n snapshots, keeping the newest $KEEP"
    for ((i = 0; i < drop; i++)); do
        rm -f -- "${files[i]}"
    done
}

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="$DEST/matches-$stamp.db.gz"
install -d -m 700 "$DEST"
tmp="$(mktemp "$DEST/.snapshot.XXXXXX")"
trap 'rm -f -- "$tmp" "$tmp.gz"' EXIT

say "snapshotting $DB"
snapshot "$tmp"
gzip -9 -- "$tmp"
install -m 600 "$tmp.gz" "$out"
say "wrote $out ($(du -h "$out" | cut -f1))"
prune
