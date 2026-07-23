#!/bin/bash
# ─────────────────────────────────────────────────────────────────
# run_scanner.sh — wrapper for daily/weekly AAPL scanners
# Usage:  ./run_scanner.sh daily
#         ./run_scanner.sh weekly
#
# Why this exists: launchd/cron do NOT load your .zshrc, so conda
# and ANTHROPIC_API_KEY are missing in automated runs. This wrapper
# makes each run self-contained and logs everything.
# ─────────────────────────────────────────────────────────────────
set -euo pipefail

# ── EDIT THESE THREE PATHS ──────────────────────────────────────
SCANNER_DIR="$HOME/trading/scanner"          # where your .py files live
CONDA_BASE="$HOME/miniconda3"                # or /opt/anaconda3 — check with: conda info --base
CONDA_ENV="trading"                          # your conda env name
# ────────────────────────────────────────────────────────────────

MODE="${1:-daily}"
LOG_DIR="$SCANNER_DIR/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/${MODE}_$(date +%Y%m%d_%H%M%S).log"

# ── API key: load from locked-down file, never hardcoded ────────
# One-time setup:
#   echo 'sk-ant-...' > ~/.anthropic_key && chmod 600 ~/.anthropic_key
KEY_FILE="$HOME/.anthropic_key"
if [[ -f "$KEY_FILE" ]]; then
  export ANTHROPIC_API_KEY="$(cat "$KEY_FILE")"
else
  echo "WARN: $KEY_FILE not found — AI thesis will 401" >> "$LOG_FILE"
fi

# ── Skip weekends/holidays for the daily scanner ────────────────
if [[ "$MODE" == "daily" ]]; then
  DOW=$(date +%u)   # 6=Sat 7=Sun
  if [[ "$DOW" -ge 6 ]]; then
    echo "$(date): weekend, skipping daily run" >> "$LOG_FILE"
    exit 0
  fi
  # US market holiday check (cheap heuristic: yfinance returns no
  # bar for today; scanner itself will just show stale data — the
  # diff logic makes that obvious). For a hard guard, add a
  # holidays check inside the Python script with the `holidays` pkg.
fi

# ── Activate conda without an interactive shell ─────────────────
source "$CONDA_BASE/etc/profile.d/conda.sh"
conda activate "$CONDA_ENV"

cd "$SCANNER_DIR"

case "$MODE" in
  daily)  SCRIPT="dailyScaner.py" ;;
  weekly) SCRIPT="weekly.py" ;;
  *) echo "unknown mode: $MODE" >&2; exit 1 ;;
esac

echo "── $(date) │ running $SCRIPT ──" >> "$LOG_FILE"
if python "$SCRIPT" >> "$LOG_FILE" 2>&1; then
  STATUS="OK"
else
  STATUS="FAILED"
fi
echo "── $(date) │ $STATUS ──" >> "$LOG_FILE"

# ── Optional: Telegram notification so you actually SEE results ─
# One-time: create a bot via @BotFather, get token + your chat_id,
# then:  echo 'TOKEN:CHAT_ID' > ~/.telegram_notify && chmod 600 ~/.telegram_notify
NOTIFY_FILE="$HOME/.telegram_notify"
if [[ -f "$NOTIFY_FILE" ]]; then
  TG_TOKEN="$(cut -d: -f1-2 "$NOTIFY_FILE")"
  TG_CHAT="$(cut -d: -f3 "$NOTIFY_FILE")"
  # Send the checklist verdict lines (grep the report), not the whole wall of text
  SUMMARY=$(grep -A2 "PRE-TRADE CHECKLIST\|Score:" "$LOG_FILE" | tail -5 || echo "run $STATUS")
  curl -s "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TG_CHAT}" \
    --data-urlencode "text=📊 ${MODE} scanner ${STATUS} $(date +%H:%M)
${SUMMARY}" > /dev/null || true
fi
