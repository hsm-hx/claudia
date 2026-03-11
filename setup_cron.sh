#!/bin/bash
# Setup cron jobs for Claude Code usage reports
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$SCRIPT_DIR/usage_reporter.py"
LOG_DIR="$SCRIPT_DIR/logs"
PYTHON=$(which python3)

mkdir -p "$LOG_DIR"

# Check .env exists
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo "Error: .env file not found."
    echo "Please copy .env.example to .env and set DISCORD_WEBHOOK_URL."
    exit 1
fi

# Build cron entries
# Daily report at 09:00 JST (00:00 UTC)
DAILY_CRON="0 0 * * * $PYTHON $SCRIPT --daily >> $LOG_DIR/daily.log 2>&1"
# Weekly report every Monday at 09:00 JST (00:00 UTC)
WEEKLY_CRON="0 0 * * 1 $PYTHON $SCRIPT --weekly >> $LOG_DIR/weekly.log 2>&1"

# Get existing crontab (ignore error if empty)
EXISTING=$(crontab -l 2>/dev/null || true)

# Remove old entries if any
CLEANED=$(echo "$EXISTING" | grep -v "usage_reporter.py" || true)

# Add new entries
NEW_CRONTAB="${CLEANED}
${DAILY_CRON}
${WEEKLY_CRON}
"

echo "$NEW_CRONTAB" | crontab -

echo "Cron jobs registered:"
echo "  Daily  (09:00 JST): $DAILY_CRON"
echo "  Weekly (Mon 09:00 JST): $WEEKLY_CRON"
echo ""
echo "Current crontab:"
crontab -l
