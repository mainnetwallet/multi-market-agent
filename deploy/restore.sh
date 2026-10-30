#!/usr/bin/env bash
# Restore a backup. STOP the agent first. Verify the file path explicitly.
# Usage: DATABASE_URL=... deploy/restore.sh backups/mm_agent_YYYY....db.gz
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: deploy/restore.sh <backup file>" >&2
  exit 2
fi
BACKUP="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
if [[ ! -f "$BACKUP" ]]; then
  echo "Backup not found: $BACKUP" >&2
  exit 3
fi
if pgrep -f "mm_agent.main" >/dev/null 2>&1 || pgrep -f "uvicorn.*mm_agent" >/dev/null 2>&1; then
  echo "Refusing to restore while the agent appears to be running. Stop it first." >&2
  exit 4
fi

DATABASE_URL="${DATABASE_URL:-}"
if [[ -z "$DATABASE_URL" ]]; then
  echo "Set DATABASE_URL explicitly for restore." >&2
  exit 5
fi

if [[ "$DATABASE_URL" == sqlite:* ]]; then
  if [[ "$DATABASE_URL" == sqlite:////* ]]; then
    DB_PATH="/${DATABASE_URL#sqlite:////}"
  else
    DB_PATH="${DATABASE_URL#sqlite:///}"
  fi
  TMP="$(mktemp)"
  trap 'rm -f "$TMP"' EXIT
  if [[ "$BACKUP" == *.gz ]]; then
    gzip -dc "$BACKUP" > "$TMP"
  else
    cp "$BACKUP" "$TMP"
  fi
  sqlite3 "$TMP" "PRAGMA integrity_check;" | grep -q '^ok$' || { echo "Backup failed integrity check" >&2; exit 6; }
  mkdir -p "$(dirname "$DB_PATH")"
  sqlite3 "$TMP" ".backup '$DB_PATH'"
  echo "Restored SQLite database to $DB_PATH"
else
  pg_restore --clean --if-exists --no-owner --no-privileges --dbname="$DATABASE_URL" "$BACKUP"
  echo "Restored PostgreSQL database"
fi

echo "Run 'alembic current' and then start the agent. Re-run the doctor."
