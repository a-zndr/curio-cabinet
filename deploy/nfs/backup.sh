#!/bin/sh
# Nightly backup for a NearlyFreeSpeech deployment.
#
# Register in the member UI: Site Information -> Scheduled Tasks
#   Tag:  backup
#   URL/command: /home/protected/app/deploy/nfs/backup.sh
#   Frequency: daily
#
# Uses VACUUM INTO (never a file copy — a live-WAL copy can be torn),
# verifies integrity, gzips, and prunes to 14 daily + 8 weekly.

set -e

DATA="/home/protected/data"
DB="$DATA/catalog.db"
BACKUPS="$DATA/backups"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
OUT="$BACKUPS/catalog-$STAMP.db"

mkdir -p "$BACKUPS"

sqlite3 "$DB" "VACUUM INTO '$OUT'"
if [ "$(sqlite3 "$OUT" 'PRAGMA integrity_check')" != "ok" ]; then
    echo "integrity check FAILED for $OUT" >&2
    rm -f "$OUT"
    exit 1
fi
gzip -f "$OUT"

# keep 14 most recent daily
ls -1t "$BACKUPS"/catalog-*.db.gz 2>/dev/null | tail -n +15 | while read -r f; do
    # keep Sunday backups as weeklies (up to 8)
    dow="$(date -u -r "$f" +%u 2>/dev/null || echo 1)"
    [ "$dow" = "7" ] || rm -f "$f"
done
ls -1t "$BACKUPS"/catalog-*.db.gz 2>/dev/null | grep -E '\-.*' | \
    awk 'NR>60' | while read -r f; do rm -f "$f"; done

echo "backup complete: $OUT.gz"
