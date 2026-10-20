#!/usr/bin/env bash
# Nightly copy of BB's database (run by cron on the server; see scripts/bb-bot-backup.cron).
# Uses SQLite's online backup, so it's safe while BB is running. Keeps the newest 14.
set -euo pipefail
APP="${BB_DEST:-/opt/bb-bot}"
DEST="${BB_BACKUPS:-/opt/bb-bot-backups}"
cd "$APP"
mkdir -p "$DEST"
# Optional: BACKUP_HEARTBEAT_URL in .env, an uptime check pinged after every verified backup (and told
# about failures), so a backup job that silently stops gets noticed.
PING="$(grep -E '^BACKUP_HEARTBEAT_URL=' .env 2>/dev/null | cut -d= -f2- | tr -d '[:space:]' || true)"
ping() { [ -n "$PING" ] && curl -fsS -m 10 --retry 3 -o /dev/null "$PING$1" || true; }
trap 'ping /fail' ERR

docker compose exec -T bb python -c "
import sqlite3
src, dst = sqlite3.connect('/app/data/bb.sqlite'), sqlite3.connect('/app/data/backup.sqlite')
src.backup(dst)
dst.close(); src.close()
"
# Restore test: open the copy as BB would, check it's intact and every table reads.
docker compose exec -T bb python -c "
import sqlite3, sys
db = sqlite3.connect('file:/app/data/backup.sqlite?mode=ro', uri=True)
ok = db.execute('PRAGMA integrity_check').fetchone()[0]
counts = {t: db.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in ('guild_settings', 'posts', 'reposts', 'rerun_events', 'fixer_health')}
print('backup check:', ok, counts)
sys.exit(0 if ok == 'ok' else 1)
"
docker compose cp bb:/app/data/backup.sqlite "$DEST/bb-$(date +%F).sqlite"
docker compose exec -T bb rm -f /app/data/backup.sqlite
ls -1t "$DEST"/bb-*.sqlite | tail -n +15 | xargs -r rm --
echo "$(date -Is) backed up to $DEST/bb-$(date +%F).sqlite"
ping ""
