#!/usr/bin/env bash
# Nightly copy of BB's database (run by cron on the server; see scripts/bb-bot-backup.cron).
# Uses SQLite's online backup, so it's safe while BB is running. Keeps the newest 14.
set -euo pipefail
APP="${BB_DEST:-/opt/bb-bot}"
DEST="${BB_BACKUPS:-/opt/bb-bot-backups}"
cd "$APP"
mkdir -p "$DEST"
docker compose exec -T bb python -c "
import sqlite3
src, dst = sqlite3.connect('/app/data/bb.sqlite'), sqlite3.connect('/app/data/backup.sqlite')
src.backup(dst)
dst.close(); src.close()
"
docker compose cp bb:/app/data/backup.sqlite "$DEST/bb-$(date +%F).sqlite"
docker compose exec -T bb rm -f /app/data/backup.sqlite
ls -1t "$DEST"/bb-*.sqlite | tail -n +15 | xargs -r rm --
echo "$(date -Is) backed up to $DEST/bb-$(date +%F).sqlite"
