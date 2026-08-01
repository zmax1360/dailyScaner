#!/usr/bin/env bash
# nightly.sh — backup the attribution DB and generate the daily report.
# Runs weekdays at 17:15 ET via launchd, after the close-mark pass (16:15–17:00).
#
# Deliberately a shell script, not another Python service: it must keep working
# even if the Python side breaks, since its whole job is protecting the data.

set -uo pipefail          # NOT -e: a failing report must not skip the backup

REPO="$HOME/works/optionTrading"
DB="$REPO/data/attribution.db"
BACKUP_DIR="$HOME/works/dbbackup"
REPORT_DIR="$REPO/report"
LOG="$REPO/logs/nightly.log"
KEEP_DAYS=30
PY="$HOME/works/Conda/anaconda3/bin/python"

mkdir -p "$BACKUP_DIR" "$REPORT_DIR" "$(dirname "$LOG")"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S %Z') $*" >> "$LOG"; }

log "── nightly start ──"

# ── 1. Backup ────────────────────────────────────────────────────────────────
# sqlite3 .backup, NOT cp. The DB is in WAL mode with the marker writing every
# 15 min; a plain cp can miss uncheckpointed transactions or catch a torn write.
STAMP=$(date +%Y%m%d)
DEST="$BACKUP_DIR/attribution_$STAMP.db"

if [ ! -f "$DB" ]; then
    log "FATAL: database not found at $DB"
    exit 1
fi

if sqlite3 "$DB" ".backup '$DEST'" 2>>"$LOG"; then
    # Verify the copy is readable and non-trivial before trusting it.
    ROWS=$(sqlite3 "$DEST" "SELECT COUNT(*) FROM flags;" 2>/dev/null || echo 0)
    SRC_ROWS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM flags;" 2>/dev/null || echo 0)
    SIZE=$(du -h "$DEST" | cut -f1)
    if [ "$ROWS" -gt 0 ] && [ "$ROWS" -ge "$((SRC_ROWS - 100))" ]; then
        log "backup OK  $DEST  ($SIZE, $ROWS rows; source $SRC_ROWS)"
    else
        log "BACKUP SUSPECT: $ROWS rows in copy vs $SRC_ROWS in source — NOT pruning old backups"
        SKIP_PRUNE=1
    fi
else
    log "BACKUP FAILED — sqlite3 .backup returned non-zero"
    SKIP_PRUNE=1
fi

# Prune only if today's backup verified. Never delete history on a failed run.
if [ -z "${SKIP_PRUNE:-}" ]; then
    PRUNED=$(find "$BACKUP_DIR" -name "attribution_*.db" -mtime +$KEEP_DAYS -print -delete | wc -l | tr -d ' ')
    [ "$PRUNED" -gt 0 ] && log "pruned $PRUNED backups older than $KEEP_DAYS days"
fi

# ── 2. Daily report ──────────────────────────────────────────────────────────
cd "$REPO" || { log "FATAL: cannot cd to $REPO"; exit 1; }

if "$PY" eod_report.py --ticker AAPL >> "$LOG" 2>&1; then
    log "daily report OK"
else
    log "daily report FAILED (exit $?)"
fi

# ── 3. Rolling window, Fridays only ──────────────────────────────────────────
# 5 = Friday in BSD date. The weekly read is the one that matters; the daily is
# mostly a coverage check.
if [ "$(date +%u)" -eq 5 ]; then
    if "$PY" eod_report.py --days 15 --ticker AAPL >> "$LOG" 2>&1; then
        log "weekly 15-day report OK"
    else
        log "weekly report FAILED (exit $?)"
    fi
fi

# ── 4. Pipeline health — surfaces silent breakage ────────────────────────────
TODAY=$(date +%Y-%m-%d)
read -r RUNS EOD FLAGS MARKED CLOSED <<EOF
$(sqlite3 "$DB" "
  SELECT
    (SELECT COUNT(*) FROM runs  WHERE substr(ts_et,1,10)='$TODAY'),
    (SELECT COUNT(*) FROM runs  WHERE substr(ts_et,1,10)='$TODAY' AND run_kind='eod'),
    (SELECT COUNT(*) FROM flags WHERE substr(ts_et,1,10)='$TODAY'),
    (SELECT COUNT(*) FROM flags WHERE substr(ts_et,1,10)='$TODAY' AND mark_t1h IS NOT NULL),
    (SELECT COUNT(*) FROM flags WHERE substr(ts_et,1,10)='$TODAY' AND mark_close IS NOT NULL);" 2>/dev/null)
EOF

log "today: runs=$RUNS eod=$EOD flags=$FLAGS t1h_marked=$MARKED close_marked=$CLOSED"

DOW=$(date +%u)
if [ "$DOW" -le 5 ]; then          # weekday — expect activity
    [ "${RUNS:-0}"   -eq 0 ] && log "ALERT: no runs today — is the scheduler alive?"
    [ "${EOD:-0}"    -eq 0 ] && log "ALERT: no EOD run today"
    [ "${CLOSED:-0}" -eq 0 ] && log "ALERT: no close marks — did the 16:15–17:00 pass run?"
    [ "${MARKED:-0}" -eq 0 ] && log "ALERT: no t1h marks — is mark_runner alive?"
fi

log "── nightly done ──"
