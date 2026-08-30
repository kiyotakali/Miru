#!/bin/bash
# Miru daily backup script
# Usage: ./backup.sh [backup_dir]
# Add to crontab for daily backups:
#   0 4 * * * /path/to/deploy/backup.sh /path/to/backups

BACKUP_DIR="${1:-./backups}"
DATA_DIR="${DATA_DIR:-./data}"
DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/miru-data-$DATE.tar.gz"
KEEP_DAYS=30

mkdir -p "$BACKUP_DIR"

echo "[Backup] Backing up $DATA_DIR → $BACKUP_FILE"
tar czf "$BACKUP_FILE" -C "$(dirname "$DATA_DIR")" "$(basename "$DATA_DIR")"

if [ $? -eq 0 ]; then
    SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
    echo "[Backup] Done ($SIZE)"
else
    echo "[Backup] FAILED"
    exit 1
fi

# Clean up old backups
DELETED=$(find "$BACKUP_DIR" -name "miru-data-*.tar.gz" -mtime +$KEEP_DAYS -delete -print | wc -l)
if [ "$DELETED" -gt 0 ]; then
    echo "[Backup] Cleaned $DELETED backups older than ${KEEP_DAYS} days"
fi
