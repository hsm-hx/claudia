#!/usr/bin/env python3
"""
Claude Code Usage Monitor（ローカル実行版）

~/.claude/projects/ の JSONL を読み取り、直近4時間・24時間・7日の
トークン使用量を Discord に通知する。cron で毎時0分に実行する想定。

依存パッケージ: なし（標準ライブラリのみ）
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict
from urllib.request import urlopen, Request
from urllib.error import URLError

# ---------------------------------------------------------------------------
# 設定（環境変数または .env ファイル）
# ---------------------------------------------------------------------------
def _load_env():
    env = Path(__file__).parent / ".env"
    if env.exists():
        with open(env) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())

_load_env()

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
# 4時間ウィンドウあたりのトークン上限（Claude Code Pro/Max の目安に合わせて調整）
MAX_TOKENS_4H = int(os.getenv("MAX_TOKENS_4H", "500000"))
# 残り何%以下でアラートを送るか
LOW_THRESHOLD_PCT = float(os.getenv("LOW_THRESHOLD_PCT", "25"))

STATE_FILE = Path.home() / ".claude" / "usage_monitor_state.json"
PROJECTS_DIR = Path.home() / ".claude" / "projects"

# ---------------------------------------------------------------------------
# 4時間ウィンドウの定義（UTC で 0-4, 4-8, 8-12, 12-16, 16-20, 20-24）
# ---------------------------------------------------------------------------
def current_window_start(now: datetime) -> datetime:
    """現在の4時間ウィンドウの開始時刻を返す。"""
    window_hour = (now.hour // 4) * 4
    return now.replace(hour=window_hour, minute=0, second=0, microsecond=0)

# ---------------------------------------------------------------------------
# JSONL 読み取り
# ---------------------------------------------------------------------------
def iter_entries():
    """~/.claude/projects/ 以下の全エントリを yield する。"""
    if not PROJECTS_DIR.exists():
        return
    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl in project_dir.glob("*.jsonl"):
            try:
                with open(jsonl, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            yield json.loads(line)
                        except json.JSONDecodeError:
                            continue
            except OSError:
                continue


def collect_tokens(since: datetime, until: datetime) -> dict:
    """指定期間のトークン集計を返す。"""
    totals = defaultdict(int)
    for entry in iter_entries():
        if entry.get("type") != "assistant":
            continue
        ts_str = entry.get("timestamp")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        if not (since <= ts < until):
            continue
        usage = entry.get("message", {}).get("usage", {})
        if not usage:
            continue
        totals["input"]    += usage.get("input_tokens", 0)
        totals["output"]   += usage.get("output_tokens", 0)
        totals["cache_w"]  += usage.get("cache_creation_input_tokens", 0)
        totals["cache_r"]  += usage.get("cache_read_input_tokens", 0)
    totals["total"] = totals["input"] + totals["output"] + totals["cache_w"] + totals["cache_r"]
    return dict(totals)

# ---------------------------------------------------------------------------
# 状態の永続化
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}

def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))

# ---------------------------------------------------------------------------
# フォーマット
# ---------------------------------------------------------------------------
def fmt(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)

def bar(pct: float, width: int = 10) -> str:
    filled = min(width, int(pct / 100 * width))
    return "█" * filled + "░" * (width - filled)

def color(pct_remaining: float) -> int:
    if pct_remaining <= 25:  return 0xEF4444  # 赤
    if pct_remaining <= 50:  return 0xF59E0B  # 黄
    return 0x22C55E                            # 緑

def jst(dt: datetime) -> str:
    return (dt + timedelta(hours=9)).strftime("%m/%d %H:%M")

# ---------------------------------------------------------------------------
# Discord 送信
# ---------------------------------------------------------------------------
def send(embeds: list[dict]):
    if not DISCORD_WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL が未設定です。", file=sys.stderr)
        sys.exit(1)
    payload = json.dumps({"embeds": embeds}).encode()
    req = Request(DISCORD_WEBHOOK_URL, data=payload,
                  headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=15) as r:
            if r.status not in (200, 204):
                print(f"Discord error: {r.status}", file=sys.stderr)
    except URLError as e:
        print(f"Discord 送信失敗: {e}", file=sys.stderr)
        sys.exit(1)

# ---------------------------------------------------------------------------
# Embed 構築
# ---------------------------------------------------------------------------
def build_embed(now: datetime, win_start: datetime, tokens_4h: dict,
                tokens_24h: dict, tokens_7d: dict,
                extra_title: str = "") -> dict:
    win_end = win_start + timedelta(hours=4)
    total_4h = tokens_4h.get("total", 0)
    pct_used = min(100.0, total_4h / MAX_TOKENS_4H * 100) if MAX_TOKENS_4H > 0 else 0.0
    pct_rem  = 100.0 - pct_used
    remaining = max(0, MAX_TOKENS_4H - total_4h)

    title = "Claude Code 使用量レポート"
    if extra_title:
        title = f"{extra_title}  |  {title}"

    fields = [
        {
            "name": f"4時間ウィンドウ  `{jst(win_start)} 〜 {jst(win_end)} JST`",
            "value": (
                f"`{bar(pct_used)}` **{pct_used:.1f}%** 使用\n"
                f"📊 使用済み: **{fmt(total_4h)}** / {fmt(MAX_TOKENS_4H)} tokens\n"
                f"💚 残り:     **{fmt(remaining)}** tokens  （{pct_rem:.1f}%）\n"
                f"　📥 Input {fmt(tokens_4h.get('input',0))}　"
                f"📤 Output {fmt(tokens_4h.get('output',0))}　"
                f"⚡ Cache {fmt(tokens_4h.get('cache_r',0))}"
            ),
            "inline": False,
        },
        {
            "name": "過去24時間",
            "value": (
                f"**{fmt(tokens_24h.get('total',0))}** tokens\n"
                f"📥 {fmt(tokens_24h.get('input',0))}  "
                f"📤 {fmt(tokens_24h.get('output',0))}  "
                f"⚡ {fmt(tokens_24h.get('cache_r',0))}"
            ),
            "inline": True,
        },
        {
            "name": "過去7日間",
            "value": (
                f"**{fmt(tokens_7d.get('total',0))}** tokens\n"
                f"📥 {fmt(tokens_7d.get('input',0))}  "
                f"📤 {fmt(tokens_7d.get('output',0))}  "
                f"⚡ {fmt(tokens_7d.get('cache_r',0))}"
            ),
            "inline": True,
        },
    ]

    return {
        "title": title,
        "color": color(pct_rem),
        "fields": fields,
        "footer": {
            "text": (
                f"チェック: {jst(now)} JST  |  "
                f"上限設定: MAX_TOKENS_4H={fmt(MAX_TOKENS_4H)}  |  "
                "データ: ~/.claude/projects/"
            )
        },
        "timestamp": now.isoformat(),
    }


def build_alert(title: str, desc: str, col: int) -> dict:
    return {
        "title": title,
        "description": desc,
        "color": col,
        "footer": {"text": "Claude Code Usage Monitor"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------
def main():
    now = datetime.now(timezone.utc)
    win_start = current_window_start(now)
    win_end   = win_start + timedelta(hours=4)
    win_key   = win_start.isoformat()

    # 使用量を集計
    tokens_4h  = collect_tokens(win_start, win_end)
    tokens_24h = collect_tokens(now - timedelta(hours=24), now + timedelta(hours=1))
    tokens_7d  = collect_tokens(now - timedelta(days=7),   now + timedelta(hours=1))

    total_4h   = tokens_4h.get("total", 0)
    pct_used   = min(100.0, total_4h / MAX_TOKENS_4H * 100) if MAX_TOKENS_4H > 0 else 0.0
    pct_rem    = 100.0 - pct_used

    state = load_state()
    embeds: list[dict] = []
    extra_title = ""

    # --- ウィンドウ切り替わり（リセット）検知 ---
    prev_win_key = state.get("win_key")
    if prev_win_key and prev_win_key != win_key:
        extra_title = "🔄 ウィンドウ更新"
        embeds.append(build_alert(
            "🔄 4時間ウィンドウ リセット",
            (
                f"新しいウィンドウが始まりました。\n"
                f"`{jst(win_start)} JST` 〜 `{jst(win_end)} JST`\n\n"
                f"前のウィンドウの使用量: **{fmt(state.get('prev_total_4h', 0))}** tokens"
            ),
            0x6366F1,
        ))
        # ウィンドウが変わったら低残量フラグをリセット
        state["notified_low_win"] = None

    # --- 残り25%以下アラート（ウィンドウ内で1回だけ）---
    if pct_rem <= LOW_THRESHOLD_PCT and state.get("notified_low_win") != win_key:
        state["notified_low_win"] = win_key
        extra_title = extra_title or "🚨 残量アラート"
        embeds.append(build_alert(
            f"🚨 レートリミット残量 {pct_rem:.1f}%",
            (
                f"4時間ウィンドウの残りトークンが **{pct_rem:.1f}%** になりました。\n"
                f"使用済み: **{fmt(total_4h)}** / {fmt(MAX_TOKENS_4H)} tokens\n"
                f"ウィンドウ終了: `{jst(win_end)} JST`"
            ),
            0xEF4444,
        ))

    # --- 定期レポート ---
    embeds.append(build_embed(now, win_start, tokens_4h, tokens_24h, tokens_7d, extra_title))

    send(embeds)

    # 状態を保存
    state["win_key"]        = win_key
    state["prev_total_4h"]  = total_4h
    save_state(state)
    print(f"通知送信完了 | 4h: {fmt(total_4h)} tokens ({pct_used:.1f}%)")

if __name__ == "__main__":
    main()
