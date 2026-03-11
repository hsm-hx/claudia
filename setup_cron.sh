#!/bin/bash
# Claude Code Usage Monitor — cron セットアップ
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$SCRIPT_DIR/logs"
PYTHON=$(which python3)

mkdir -p "$LOG_DIR"

if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo "Error: .env が見つかりません。"
    echo ".env.example をコピーして DISCORD_WEBHOOK_URL を設定してください。"
    exit 1
fi

# ---------------------------------------------------------------------------
# monitor.py  — 毎時0分（4時間ウィンドウの使用率・アラート）
HOURLY_CRON="0 * * * * $PYTHON $SCRIPT_DIR/monitor.py >> $LOG_DIR/monitor.log 2>&1"

# usage_reporter.py — 毎朝9時 JST (= 0:00 UTC) に昨日の日次レポート
DAILY_CRON="0 0 * * * $PYTHON $SCRIPT_DIR/usage_reporter.py --daily >> $LOG_DIR/daily.log 2>&1"

# usage_reporter.py — 毎週月曜9時 JST (= 0:00 UTC) に週次レポート
WEEKLY_CRON="0 0 * * 1 $PYTHON $SCRIPT_DIR/usage_reporter.py --weekly >> $LOG_DIR/weekly.log 2>&1"
# ---------------------------------------------------------------------------

EXISTING=$(crontab -l 2>/dev/null || true)
CLEANED=$(echo "$EXISTING" | grep -v "monitor\.py\|usage_reporter\.py" || true)

NEW_CRONTAB="${CLEANED}
${HOURLY_CRON}
${DAILY_CRON}
${WEEKLY_CRON}
"

echo "$NEW_CRONTAB" | crontab -

echo "cron 登録完了:"
echo "  毎時0分         : monitor.py（4時間ウィンドウ使用率）"
echo "  毎日 09:00 JST  : usage_reporter.py --daily（昨日の日次レポート）"
echo "  毎週月 09:00 JST: usage_reporter.py --weekly（先週の週次レポート）"
echo ""
echo "現在の crontab:"
crontab -l
