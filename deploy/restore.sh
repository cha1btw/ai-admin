#!/usr/bin/env bash
# Restore drill: load a backup into a fresh database and check it answers.
#   deploy/restore.sh backups/ai_admin-20261022-0300.sql.gz [target_db]
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
backup="${1:?path to .sql.gz}"
target="${2:-ai_admin_restore_check}"
compose="docker compose -f deploy/docker-compose.prod.yml"
$compose exec -T db dropdb -U "${POSTGRES_USER:-ai_admin}" --if-exists "$target"
$compose exec -T db createdb -U "${POSTGRES_USER:-ai_admin}" "$target"
gunzip -c "$backup" | $compose exec -T db psql -q -U "${POSTGRES_USER:-ai_admin}" -d "$target"
$compose exec -T db psql -U "${POSTGRES_USER:-ai_admin}" -d "$target" -c \
  "select (select count(*) from tenant) as tenants, (select count(*) from booking) as bookings;"
echo "restore check ok: $target (drop it when done)"
