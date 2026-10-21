#!/usr/bin/env bash
# Daily database backup (cron: 0 3 * * * /path/to/repo/deploy/backup.sh).
# Keeps 14 days locally; if RCLONE_REMOTE is set (e.g. "b2:ai-admin-backups"), copies off-site.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
mkdir -p backups
file="backups/ai_admin-$(date -u +%Y%m%d-%H%M).sql.gz"
docker compose -f deploy/docker-compose.prod.yml exec -T db \
  pg_dump -U "${POSTGRES_USER:-ai_admin}" -d "${POSTGRES_DB:-ai_admin}" --no-owner | gzip > "$file"
find backups -name 'ai_admin-*.sql.gz' -mtime +14 -delete
if [[ -n "${RCLONE_REMOTE:-}" ]]; then
  rclone copy "$file" "$RCLONE_REMOTE"
fi
echo "backup: $file"
