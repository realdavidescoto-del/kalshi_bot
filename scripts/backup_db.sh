#!/usr/bin/env bash
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-./data/backups}"
DB_PATH="${DB_PATH:-./data/kalshi_shadow.db}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"

mkdir -p "${BACKUP_DIR}"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/kalshi_${TIMESTAMP}.db"

echo "Backing up ${DB_PATH} to ${BACKUP_FILE}..."
sqlite3 "${DB_PATH}" ".backup '${BACKUP_FILE}'"
sqlite3 "${BACKUP_FILE}" "PRAGMA integrity_check;"

gzip "${BACKUP_FILE}"
echo "Backup complete: ${BACKUP_FILE}.gz"

# Prune old backups
find "${BACKUP_DIR}" -name "kalshi_*.db.gz" -mtime +${RETENTION_DAYS} -delete
echo "Pruned backups older than ${RETENTION_DAYS} days."
