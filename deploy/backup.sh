#!/usr/bin/env bash
# Consistent backup for SQLite or PostgreSQL. Run from the repo root.
# Usage: DATABASE_URL=... BACKUP_DIR=./backups deploy/backup.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
BACKUP_DIR="${BACKUP_DIR:-$ROOT/backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

DATABASE_URL="${DATABASE_URL:-}"
if [[ -z "$DATABASE_URL" ]]; then
  if [[ -f .env ]]; then
    DATABASE_URL="$(grep -E '^DATABASE_URL=' .env | head -1 | cut -d= -f2- || true)"
  fi
fi
if [[ -z "$DATABASE_URL" ]]; then
  echo "DATABASE_URL is not set and not found in .env" >&2
  exit 2
fi

if [[ "$DATABASE_URL" == sqlite:* ]]; then
  # sqlite:///./data/x.db -> ./data/x.db ; sqlite:////app/data/x.db -> /app/data/x.db
  if [[ "$DATABASE_URL" == sqlite:////* ]]; then
    DB_PATH="/${DATABASE_URL#sqlite:////}"
  else
    DB_PATH="${DATABASE_URL#sqlite:///}"
  fi
  if [[ ! -f "$DB_PATH" ]]; then
    echo "SQLite database not found: $DB_PATH" >&2
    exit 3
  fi
  OUT="$BACKUP_DIR/mm_agent_${STAMP}.db"
  sqlite3 "$DB_PATH" ".backup '$OUT'"
  gzip -9 "$OUT"
  echo "Wrote $OUT.gz"
else
  if ! command -v pg_dump >/dev/null 2>&1; then
    echo "pg_dump is required for PostgreSQL backups" >&2
    exit 4
  fi
  OUT="$BACKUP_DIR/mm_agent_${STAMP}.dump"
  pg_dump --format=custom --no-owner --no-privileges --dbname="$DATABASE_URL" --file="$OUT"
  chmod 600 "$OUT"
  echo "Wrote $OUT"
fi

# Keep 14 days of backups.
find "$BACKUP_DIR" -type f \( -name 'mm_agent_*.dump' -o -name 'mm_agent_*.db.gz' \) -mtime +14 -print -delete
