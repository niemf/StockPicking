#!/usr/bin/env bash
# Nightly SQLite backup with 14-day retention.
#
#   DB=/opt/tradeai/data/tradeai.db BACKUP_DIR=/opt/tradeai/backups ./backup.sh
set -euo pipefail

DB="${DB:-/opt/tradeai/data/tradeai.db}"
BACKUP_DIR="${BACKUP_DIR:-/opt/tradeai/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"

if [[ ! -f "$DB" ]]; then
    echo "database not found: $DB" >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"
stamp="$(date +%Y%m%d-%H%M%S)"
tmp="$BACKUP_DIR/tradeai-$stamp.db"
target="$tmp.gz"

# .backup is WAL-safe and does not block readers/writers for long.
sqlite3 "$DB" ".backup '$tmp'"
gzip -9 "$tmp"

# Keep the newest N files; delete older ones.
find "$BACKUP_DIR" -maxdepth 1 -name 'tradeai-*.db.gz' -type f \
    -printf '%T@ %p\n' | sort -nr | tail -n +$((KEEP_DAYS + 1)) | cut -d' ' -f2- \
    | xargs -r rm -f

echo "backup ok: $target ($(du -h "$target" | cut -f1))"
