#!/usr/bin/env python3
"""
Claude Code Rate-Limit & Usage Monitor — Cloud Run Service

毎時0分に Anthropic API のレートリミット消費状況を Discord に通知します。
  - 現在のウィンドウ（~4時間）でのトークン使用率（%）
  - リセットまでの残り時間
  - 残り25%以下でアラート
  - リセット検知でアラート

プローブ方法:
  POST /v1/messages/count_tokens（トークン消費ゼロ）を呼び出し、
  レスポンスヘッダーからリアルタイムのレートリミット情報を取得します。
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
import uvicorn

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY: str = ""
DISCORD_WEBHOOK_URL: str = ""
PORT: int = 8080

ANTHROPIC_API_BASE = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"

# count_tokens に使うモデル（最安・最軽量）
PROBE_MODEL = "claude-haiku-4-5-20251001"

LOW_REMAINING_THRESHOLD = 0.25  # 残り25%以下でアラート

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
_state: dict[str, Any] = {
    # 前回チェック時のレートリミット情報（リセット検知用）
    "prev_reset_at": None,       # str | None: 前回の reset タイムスタンプ
    "notified_low": False,       # bool: 今ウィンドウで低残量アラートを送信済みか
    "last_check_at": None,       # datetime | None
    "last_rl": {},               # dict: 最後に取得したレートリミット情報
}


# ---------------------------------------------------------------------------
# Rate limit probe
# ---------------------------------------------------------------------------
def _parse_rl_headers(resp: httpx.Response) -> dict[str, Any]:
    """レスポンスヘッダーから anthropic-ratelimit-* を抽出して返す。"""
    result: dict[str, Any] = {}
    for name in ("tokens", "requests", "input-tokens", "output-tokens"):
        prefix = f"anthropic-ratelimit-{name}-"
        raw_limit = resp.headers.get(f"{prefix}limit")
        raw_remaining = resp.headers.get(f"{prefix}remaining")
        raw_reset = resp.headers.get(f"{prefix}reset")
        if raw_limit is None:
            continue
        limit = int(raw_limit)
        remaining = int(raw_remaining or 0)
        used = limit - remaining
        result[name] = {
            "limit": limit,
            "remaining": remaining,
            "used": used,
            "pct_used": used / limit * 100 if limit > 0 else 0.0,
            "pct_remaining": remaining / limit * 100 if limit > 0 else 0.0,
            "reset": raw_reset,
        }
    return result


async def probe_rate_limits() -> dict[str, Any]:
    """
    Anthropic API をプローブしてレートリミットヘッダーを取得する。

    試行順:
      1. GET /v1/models          （無料・確実）
      2. POST /v1/messages/count_tokens  （ベータ、フォールバック）

    返値の例:
    {
      "tokens": {"limit": 200000, "remaining": 150000, "used": 50000,
                 "pct_used": 25.0, "pct_remaining": 75.0, "reset": "..."},
      "requests": { ... },
    }
    """
    common_headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": ANTHROPIC_VERSION,
    }

    async with httpx.AsyncClient(timeout=20) as client:
        # --- 試行1: GET /v1/models（無料） ---
        try:
            resp = await client.get(
                f"{ANTHROPIC_API_BASE}/v1/models",
                headers=common_headers,
            )
            resp.raise_for_status()
            result = _parse_rl_headers(resp)
            if result:
                logger.info("probe via GET /v1/models OK")
                return result
            logger.info("GET /v1/models returned no RL headers; falling back to count_tokens")
        except Exception as exc:
            logger.warning("GET /v1/models failed: %s", exc)

        # --- 試行2: POST /v1/messages/count_tokens ---
        try:
            resp = await client.post(
                f"{ANTHROPIC_API_BASE}/v1/messages/count_tokens",
                headers={**common_headers, "anthropic-beta": "token-counting-2024-11-01",
                         "content-type": "application/json"},
                json={"model": PROBE_MODEL, "messages": [{"role": "user", "content": "ping"}]},
            )
            if not resp.is_success:
                body = resp.text
                raise RuntimeError(
                    f"count_tokens {resp.status_code}: {body}"
                )
            result = _parse_rl_headers(resp)
            logger.info("probe via count_tokens OK")
            return result
        except Exception as exc:
            raise RuntimeError(f"すべてのプローブに失敗しました: {exc}") from exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _parse_reset(reset_str: str | None) -> datetime | None:
    if not reset_str:
        return None
    try:
        return datetime.fromisoformat(reset_str.replace("Z", "+00:00"))
    except ValueError:
        return None


def _time_until_reset(reset_str: str | None) -> str:
    reset_dt = _parse_reset(reset_str)
    if reset_dt is None:
        return "不明"
    now = datetime.now(timezone.utc)
    delta = reset_dt - now
    if delta.total_seconds() <= 0:
        return "まもなくリセット"
    total_sec = int(delta.total_seconds())
    h, rem = divmod(total_sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}時間{m}分後"
    return f"{m}分{s}秒後"


def _bar(pct_used: float, width: int = 10) -> str:
    filled = min(width, int(pct_used / 100 * width))
    return "█" * filled + "░" * (width - filled)


def _color(pct_remaining: float) -> int:
    if pct_remaining <= 25:
        return 0xEF4444   # red
    if pct_remaining <= 50:
        return 0xF59E0B   # amber
    return 0x22C55E       # green


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------
async def send_discord(embeds: list[dict]) -> None:
    payload = json.dumps({"embeds": embeds}).encode()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            DISCORD_WEBHOOK_URL,
            content=payload,
            headers={"Content-Type": "application/json"},
        )
    if resp.status_code not in (200, 204):
        logger.error("Discord %d: %s", resp.status_code, resp.text)
    else:
        logger.info("Discord notified (%d).", resp.status_code)


def build_rate_limit_embed(rl: dict[str, Any], extra_label: str = "") -> dict:
    """レートリミット使用率を示す Discord Embed を生成する。"""
    now_jst = datetime.now(timezone.utc) + timedelta(hours=9)

    # tokens が最重要指標。なければ input-tokens を使う
    primary = rl.get("tokens") or rl.get("input-tokens") or {}
    pct_used = primary.get("pct_used", 0.0)
    pct_remaining = primary.get("pct_remaining", 100.0)
    reset_str = primary.get("reset")

    title = "Claude Code レートリミット使用状況"
    if extra_label:
        title = f"{extra_label}  |  {title}"

    fields: list[dict] = [
        {
            "name": "チェック時刻 (JST)",
            "value": now_jst.strftime("%Y-%m-%d %H:%M"),
            "inline": True,
        },
        {
            "name": "リセットまで",
            "value": _time_until_reset(reset_str),
            "inline": True,
        },
    ]

    # 主要ウィンドウの使用率バー
    if primary:
        limit = primary.get("limit", 0)
        used = primary.get("used", 0)
        remaining = primary.get("remaining", 0)
        fields.append({
            "name": f"トークン使用率（ウィンドウ上限: {limit:,}）",
            "value": (
                f"`{_bar(pct_used)}` **{pct_used:.1f}%** 使用\n"
                f"✅ 使用済み: **{used:,}** トークン\n"
                f"💚 残り:     **{remaining:,}** トークン  （{pct_remaining:.1f}%）"
            ),
            "inline": False,
        })

    # リクエスト数
    req = rl.get("requests")
    if req:
        fields.append({
            "name": f"リクエスト数（上限: {req['limit']:,}）",
            "value": (
                f"`{_bar(req['pct_used'])}` **{req['pct_used']:.1f}%** 使用\n"
                f"残り: **{req['remaining']:,}** req  （リセット: {_time_until_reset(req.get('reset'))}）"
            ),
            "inline": False,
        })

    # input / output 内訳
    inp = rl.get("input-tokens")
    out = rl.get("output-tokens")
    if inp or out:
        sub_lines = []
        if inp:
            sub_lines.append(
                f"📥 Input:  {inp['used']:,} / {inp['limit']:,}  （{inp['pct_used']:.1f}%）"
            )
        if out:
            sub_lines.append(
                f"📤 Output: {out['used']:,} / {out['limit']:,}  （{out['pct_used']:.1f}%）"
            )
        fields.append({
            "name": "トークン内訳",
            "value": "\n".join(sub_lines),
            "inline": False,
        })

    return {
        "title": title,
        "color": _color(pct_remaining),
        "fields": fields,
        "footer": {
            "text": (
                "Claude Code Usage Monitor  |  "
                "データソース: Anthropic API rate-limit headers  |  "
                "対象: API キー経由の全使用（GitHub Actions 含む）"
            )
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def build_alert_embed(title: str, description: str, color: int) -> dict:
    return {
        "title": title,
        "description": description,
        "color": color,
        "footer": {"text": "Claude Code Usage Monitor"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Core check
# ---------------------------------------------------------------------------
async def run_check(reason: str = "hourly") -> None:
    logger.info("Running check (reason=%s).", reason)
    embeds: list[dict] = []

    try:
        rl = await probe_rate_limits()
    except Exception as exc:
        logger.error("probe_rate_limits failed: %s", exc)
        embeds.append(build_alert_embed(
            "⚠️ プローブ失敗",
            f"Anthropic API へのプローブが失敗しました。\n\n```\n{exc}\n```",
            0xEF4444,
        ))
        await send_discord(embeds)
        return

    primary = rl.get("tokens") or rl.get("input-tokens") or {}
    pct_remaining = primary.get("pct_remaining", 100.0)
    reset_str = primary.get("reset")
    prev_reset = _state["prev_reset_at"]

    extra_label = ""

    # --- リセット検知 ---
    # reset タイムスタンプが前回から変わっていたらリセット発生
    if prev_reset is not None and reset_str and reset_str != prev_reset:
        _state["notified_low"] = False
        extra_label = "🔄 リセット"
        embeds.append(build_alert_embed(
            "🔄 レートリミット リセット",
            f"レートリミットがリセットされました！\n新しいウィンドウ: リセット予定 `{reset_str}`",
            0x6366F1,
        ))
        logger.info("Rate limit reset detected. New window reset=%s", reset_str)

    # --- 残り25%以下アラート ---
    if pct_remaining <= LOW_REMAINING_THRESHOLD * 100 and not _state["notified_low"]:
        _state["notified_low"] = True
        extra_label = extra_label or "🚨 低残量"
        embeds.append(build_alert_embed(
            "🚨 レートリミット残量アラート（残り25%以下）",
            (
                f"現在のウィンドウでのレートリミット残量が **{pct_remaining:.1f}%** になりました。\n\n"
                f"リセットまで: **{_time_until_reset(reset_str)}**\n"
                f"残りトークン: **{primary.get('remaining', 0):,}** / {primary.get('limit', 0):,}"
            ),
            0xEF4444,
        ))

    # --- 毎時の定期レポート ---
    embeds.append(build_rate_limit_embed(rl, extra_label))

    # State 更新
    _state["prev_reset_at"] = reset_str
    _state["last_check_at"] = datetime.now(timezone.utc)
    _state["last_rl"] = rl

    await send_discord(embeds)
    logger.info("Check done. tokens remaining=%.1f%%", pct_remaining)


# ---------------------------------------------------------------------------
# FastAPI + APScheduler
# ---------------------------------------------------------------------------
scheduler = AsyncIOScheduler(timezone="UTC")


@asynccontextmanager
async def lifespan(_: FastAPI):
    global ANTHROPIC_API_KEY, DISCORD_WEBHOOK_URL, PORT

    # ローカル開発用 .env 読み込み
    env_file = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())

    def _require(name: str) -> str:
        v = os.getenv(name, "")
        if not v:
            raise RuntimeError(f"必須の環境変数 {name!r} が設定されていません。")
        return v

    ANTHROPIC_API_KEY = _require("ANTHROPIC_API_KEY")
    DISCORD_WEBHOOK_URL = _require("DISCORD_WEBHOOK_URL")
    PORT = int(os.getenv("PORT", "8080"))

    logger.info("Claude Code Usage Monitor 起動")

    # 起動直後に1回チェック
    asyncio.create_task(run_check("startup"))

    # 毎時0分にチェック
    scheduler.add_job(
        run_check,
        CronTrigger(minute=0),
        args=["hourly"],
        id="hourly_check",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("スケジューラ起動。毎時0分に実行。")

    yield

    scheduler.shutdown(wait=False)


app = FastAPI(title="Claude Code Usage Monitor", lifespan=lifespan)


@app.get("/")
async def health() -> dict:
    rl = _state.get("last_rl", {})
    primary = rl.get("tokens") or rl.get("input-tokens") or {}
    return {
        "status": "ok",
        "last_check_at": _state["last_check_at"].isoformat() if _state["last_check_at"] else None,
        "tokens_pct_remaining": primary.get("pct_remaining"),
        "tokens_pct_used": primary.get("pct_used"),
        "reset_at": primary.get("reset"),
        "notified_low": _state["notified_low"],
    }


@app.post("/check")
async def manual_check() -> dict:
    """手動チェックをトリガーする（動作確認用）。"""
    asyncio.create_task(run_check("manual"))
    return {"status": "check triggered"}


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")), log_level="info")
