#!/usr/bin/env python3
"""
Claude Code GitHub Actions Usage Monitor

GitHub Actions のログから Claude Code の使用コスト（total_cost_usd）を集計し、
Discord に通知する。cron で毎時0分に実行する想定。

依存パッケージ: なし（標準ライブラリのみ）
必要なもの: GITHUB_TOKEN（repo の read 権限）, DISCORD_WEBHOOK_URL
"""

import gzip
import io
import json
import os
import re
import sys
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

# ---------------------------------------------------------------------------
# 設定
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
GITHUB_TOKEN        = os.getenv("GITHUB_TOKEN", "")
# 監視するリポジトリ（カンマ区切り, 例: "owner/repo1,owner/repo2"）
GITHUB_REPOS        = [r.strip() for r in os.getenv("GITHUB_REPOS", "").split(",") if r.strip()]

# 4時間ウィンドウあたりのコスト上限 USD（目安値、自由に設定）
MAX_COST_4H_USD     = float(os.getenv("MAX_COST_4H_USD", "5.0"))
LOW_THRESHOLD_PCT   = float(os.getenv("LOW_THRESHOLD_PCT", "25"))

# GitHub API から取得するワークフロー実行の最大件数（1リポジトリあたり）
MAX_RUNS_PER_REPO   = int(os.getenv("MAX_RUNS_PER_REPO", "100"))

STATE_FILE = Path.home() / ".claude" / "usage_monitor_state.json"

GITHUB_API = "https://api.github.com"

# ---------------------------------------------------------------------------
# GitHub API ヘルパー
# ---------------------------------------------------------------------------
def gh_get(path: str) -> dict | list:
    url = f"{GITHUB_API}{path}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    req = Request(url, headers=headers)
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def gh_download(path: str) -> bytes:
    """ログ zip など、バイナリをダウンロードする。リダイレクトを追う。"""
    url = f"{GITHUB_API}{path}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    req = Request(url, headers=headers)
    # GitHub はログ URL に 302 リダイレクトを返す場合がある
    # urlopen は自動でリダイレクトを追う
    with urlopen(req, timeout=60) as r:
        return r.read()

# ---------------------------------------------------------------------------
# ログパース
# ---------------------------------------------------------------------------
def parse_result_entries(log_text: str) -> list[dict]:
    """
    GitHub Actions のログテキストから Claude Code の result JSON を抽出する。
    ログの各行は "YYYY-MM-DDTHH:MM:SS.fffZ <text>" の形式。
    """
    results = []
    for line in log_text.splitlines():
        # タイムスタンプを除いた部分を取り出す
        # 例: "2026-03-12T01:23:45.000Z {"type":"result",...}"
        m = re.match(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s+(.+)$", line)
        text = m.group(1) if m else line.strip()
        if '"type"' not in text or '"result"' not in text:
            continue
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "result" and "total_cost_usd" in obj:
            results.append(obj)
    return results


def fetch_runs_with_cost(repo: str, since: datetime) -> list[dict]:
    """
    指定リポジトリの since 以降の完了したワークフロー実行を取得し、
    ログから total_cost_usd を抽出して返す。

    返値: [{"repo": str, "run_id": int, "workflow": str, "created_at": datetime,
             "cost_usd": float, "num_turns": int, "model": str, ...}, ...]
    """
    since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    path = f"/repos/{repo}/actions/runs?status=completed&per_page={MAX_RUNS_PER_REPO}&created=>={since_str}"
    try:
        data = gh_get(path)
    except Exception as exc:
        print(f"[{repo}] runs 取得失敗: {exc}", file=sys.stderr)
        return []

    runs = data.get("workflow_runs", []) if isinstance(data, dict) else []
    enriched = []
    for run in runs:
        created_at_str = run.get("created_at", "")
        try:
            created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        if created_at < since:
            continue

        run_id = run["id"]
        workflow_name = run.get("name", "unknown")

        # ログをダウンロードして result エントリを抽出
        try:
            zip_bytes = gh_download(f"/repos/{repo}/actions/runs/{run_id}/logs")
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                log_text = ""
                for name in zf.namelist():
                    with zf.open(name) as f:
                        log_text += f.read().decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[{repo}] run {run_id} ログ取得失敗: {exc}", file=sys.stderr)
            continue

        for entry in parse_result_entries(log_text):
            enriched.append({
                "repo":        repo,
                "run_id":      run_id,
                "workflow":    workflow_name,
                "created_at":  created_at,
                "cost_usd":    entry.get("total_cost_usd", 0.0),
                "num_turns":   entry.get("num_turns", 0),
                "duration_ms": entry.get("duration_ms", 0),
                "model":       entry.get("model", "unknown"),
                "is_error":    entry.get("is_error", False),
            })

    return enriched

# ---------------------------------------------------------------------------
# 集計
# ---------------------------------------------------------------------------
def aggregate(runs: list[dict], since: datetime, until: datetime) -> dict:
    filtered = [r for r in runs if since <= r["created_at"] <= until]
    cost     = sum(r["cost_usd"] for r in filtered)
    turns    = sum(r["num_turns"] for r in filtered)
    count    = len(filtered)
    by_repo: dict[str, float] = defaultdict(float)
    for r in filtered:
        by_repo[r["repo"]] += r["cost_usd"]
    return {"cost": cost, "turns": turns, "count": count, "by_repo": dict(by_repo)}

# ---------------------------------------------------------------------------
# 状態管理
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
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str))

# ---------------------------------------------------------------------------
# フォーマット
# ---------------------------------------------------------------------------
def jst(dt: datetime) -> str:
    return (dt + timedelta(hours=9)).strftime("%m/%d %H:%M")

def bar(pct: float, width: int = 10) -> str:
    filled = min(width, int(pct / 100 * width))
    return "█" * filled + "░" * (width - filled)

def color_for(pct_rem: float) -> int:
    if pct_rem <= 25: return 0xEF4444
    if pct_rem <= 50: return 0xF59E0B
    return 0x22C55E

def current_window_start(now: datetime) -> datetime:
    return now.replace(hour=(now.hour // 4) * 4, minute=0, second=0, microsecond=0)

# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------
def send_discord(embeds: list[dict]):
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

def build_embed(now, win_start, agg_4h, agg_24h, agg_7d, extra_title="") -> dict:
    win_end  = win_start + timedelta(hours=4)
    cost_4h  = agg_4h["cost"]
    pct_used = min(100.0, cost_4h / MAX_COST_4H_USD * 100) if MAX_COST_4H_USD > 0 else 0.0
    pct_rem  = 100.0 - pct_used
    remaining = max(0.0, MAX_COST_4H_USD - cost_4h)

    title = "Claude Code GitHub Actions 使用量"
    if extra_title:
        title = f"{extra_title}  |  {title}"

    # リポジトリ別内訳（4h）
    repo_lines = []
    for repo, cost in sorted(agg_4h["by_repo"].items(), key=lambda x: x[1], reverse=True):
        repo_lines.append(f"`{repo.split('/')[-1]}` ${cost:.4f}")
    repo_text = "  ".join(repo_lines) if repo_lines else "なし"

    fields = [
        {
            "name": f"4時間ウィンドウ  `{jst(win_start)} 〜 {jst(win_end)} JST`",
            "value": (
                f"`{bar(pct_used)}` **{pct_used:.1f}%** 使用\n"
                f"💰 使用済み: **${cost_4h:.4f}** / ${MAX_COST_4H_USD:.2f}\n"
                f"💚 残り:     **${remaining:.4f}**  （{pct_rem:.1f}%）\n"
                f"🔄 実行数: {agg_4h['count']}回  💬 ターン数: {agg_4h['turns']}\n"
                f"📁 {repo_text}"
            ),
            "inline": False,
        },
        {
            "name": "過去24時間",
            "value": f"**${agg_24h['cost']:.4f}**\n{agg_24h['count']}回 / {agg_24h['turns']}ターン",
            "inline": True,
        },
        {
            "name": "過去7日間",
            "value": f"**${agg_7d['cost']:.4f}**\n{agg_7d['count']}回 / {agg_7d['turns']}ターン",
            "inline": True,
        },
    ]

    return {
        "title": title,
        "color": color_for(pct_rem),
        "fields": fields,
        "footer": {
            "text": (
                f"チェック: {jst(now)} JST  |  "
                f"上限設定: MAX_COST_4H_USD=${MAX_COST_4H_USD}  |  "
                f"監視リポジトリ: {', '.join(GITHUB_REPOS) or '未設定'}"
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
    if not GITHUB_TOKEN:
        print("GITHUB_TOKEN が未設定です。", file=sys.stderr)
        sys.exit(1)
    if not GITHUB_REPOS:
        print("GITHUB_REPOS が未設定です。例: owner/repo1,owner/repo2", file=sys.stderr)
        sys.exit(1)

    now      = datetime.now(timezone.utc)
    win_start = current_window_start(now)
    win_end   = win_start + timedelta(hours=4)
    win_key   = win_start.isoformat()

    # 7日前から全ラン取得（4h/24h/7d を一度のフェッチで賄う）
    since_7d = now - timedelta(days=7)
    all_runs: list[dict] = []
    for repo in GITHUB_REPOS:
        print(f"[{repo}] ログ取得中...")
        all_runs.extend(fetch_runs_with_cost(repo, since_7d))

    agg_4h  = aggregate(all_runs, win_start,          win_end)
    agg_24h = aggregate(all_runs, now - timedelta(hours=24), now)
    agg_7d  = aggregate(all_runs, since_7d,            now)

    state = load_state()
    embeds: list[dict] = []
    extra_title = ""

    # --- ウィンドウ切り替わり検知 ---
    prev_win = state.get("win_key")
    if prev_win and prev_win != win_key:
        prev_cost = state.get("prev_cost_4h", 0.0)
        extra_title = "🔄 ウィンドウ更新"
        embeds.append(build_alert(
            "🔄 4時間ウィンドウ リセット",
            (
                f"新しいウィンドウが始まりました。\n"
                f"`{jst(win_start)} 〜 {jst(win_end)} JST`\n\n"
                f"前ウィンドウのコスト: **${prev_cost:.4f}**"
            ),
            0x6366F1,
        ))
        state["notified_low_win"] = None

    # --- 残り25%以下アラート ---
    cost_4h  = agg_4h["cost"]
    pct_rem  = (1 - cost_4h / MAX_COST_4H_USD) * 100 if MAX_COST_4H_USD > 0 else 100.0
    if pct_rem <= LOW_THRESHOLD_PCT and state.get("notified_low_win") != win_key:
        state["notified_low_win"] = win_key
        extra_title = extra_title or "🚨 予算アラート"
        embeds.append(build_alert(
            f"🚨 4時間予算残量 {pct_rem:.1f}%",
            (
                f"4時間ウィンドウの残り予算が **{pct_rem:.1f}%** になりました。\n"
                f"使用済み: **${cost_4h:.4f}** / ${MAX_COST_4H_USD:.2f}\n"
                f"ウィンドウ終了: `{jst(win_end)} JST`"
            ),
            0xEF4444,
        ))

    # --- 定期レポート ---
    embeds.append(build_embed(now, win_start, agg_4h, agg_24h, agg_7d, extra_title))

    send_discord(embeds)

    state["win_key"]       = win_key
    state["prev_cost_4h"]  = cost_4h
    save_state(state)
    print(f"通知送信完了 | 4h: ${cost_4h:.4f} ({100-pct_rem:.1f}%) | runs: {agg_4h['count']}")

if __name__ == "__main__":
    main()
