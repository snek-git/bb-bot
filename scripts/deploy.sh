#!/usr/bin/env bash
# Test, copy to the server, rebuild, and wait until BB is back on Discord.
#   scripts/deploy.sh            (BB_HOST and BB_DEST override the target)
set -euo pipefail
HOST="${BB_HOST:-mc-server}"
DEST="${BB_DEST:-/opt/bb-bot}"
cd "$(dirname "$0")/.."

echo "== lint and tests"
uv run ruff check bb tests
uv run pytest -q -p no:warnings

echo "== copying to $HOST:$DEST"
rsync -az --delete \
  --exclude .venv --exclude data --exclude .git --exclude __pycache__ \
  --exclude .pytest_cache --exclude .ruff_cache --exclude dryrun-report.md --exclude .DS_Store \
  ./ "$HOST:$DEST/"

echo "== building and restarting"
ssh "$HOST" "cd $DEST && chmod 600 .env && docker compose up -d --build 2>&1 | tail -2"

echo "== waiting for BB to reconnect"
for _ in $(seq 1 30); do
  if ssh "$HOST" "cd $DEST && docker compose logs bb 2>&1 | grep -q 'connected to Gateway'"; then
    echo "BB is online."
    exit 0
  fi
  sleep 2
done
echo "BB didn't reconnect within a minute. Last logs:"
ssh "$HOST" "cd $DEST && docker compose logs --tail 40 bb"
exit 1
