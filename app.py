#!/usr/bin/env python3
"""
Claude Code Usage Monitor — Cloud Run Service

Monitors Anthropic API usage (covers GitHub Actions and API-based usage)
and sends Discord notifications:
  - Every hour at :00
  - When billing period resets
  - When remaining budget drops below 25%
"""

import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
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
# Config (from environment variables)
# ---------------------------------------------------------------------------
def _require(name: str) -> str:
    v = os.getenv(name, "")
    if not v:
        raise RuntimeError(f"Required environment variable {name!r} is not set.")
    return v


ANTHROPIC_API_KEY: str = ""
DISCORD_WEBHOOK_URL: str = ""
MONTHLY_BUDGET_USD: float = 100.0
BILLING_CYCLE_DAY: int = 1
PORT: int = 8080

ANTHROPIC_API_BASE = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"

# USD per million tokens  (November 2024 public pricing)
PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4":     {"input": 15.0,  "output": 75.0,  "cache_write": 18.75, "cache_read": 1.50},
    "claude-opus-4-5":   {"input": 15.0,  "output": 75.0,  "cache_write": 18.75, "cache_read": 1.50},
    "claude-opus-4-6":   {"input": 15.0,  "output": 75.0,  "cache_write": 18.75, "cache_read": 1.50},
    "claude-sonnet-4":   {"input": 3.0,   "output": 15.0,  "cache_write": 3.75,  "cache_read": 0.30},
    "claude-sonnet-4-5": {"input": 3.0,   "output": 15.0,  "cache_write": 3.75,  "cache_read": 0.30},
    "claude-sonnet-4-6": {"input": 3.0,   "output": 15.0,  "cache_write": 3.75,  "cache_read": 0.30},
    "claude-haiku-4-5":  {"input": 0.80,  "output": 4.0,   "cache_write": 1.0,   "cache_read": 0.08},
    # Fallback for unknown models
    "default":           {"input": 3.0,   "output": 15.0,  "cache_write": 3.75,  "cache_read": 0.30},
}

LOW_BUDGET_THRESHOLD = 0.25  # notify when remaining budget <= 25%

# ---------------------------------------------------------------------------
# In-memory state  (reset on Cloud Run instance restart — acceptable)
# ---------------------------------------------------------------------------
_state: dict[str, Any] = {
    "period_start": None,           # date: start of current billing period
    "period_cost_usd": 0.0,        # float: accumulated cost this period
    "prev_period_cost_usd": None,  # float | None: cost at last hourly check
    "notified_low_budget": False,  # bool: already sent the <25% remaining alert?
    "last_check_at": None,         # datetime | None
}


# ---------------------------------------------------------------------------
# Billing period helpers
# ---------------------------------------------------------------------------
def billing_period_start(for_date: date | None = None) -> date:
    """Return the start date of the current billing period."""
    d = for_date or date.today()
    day = BILLING_CYCLE_DAY
    if d.day >= day:
        return d.replace(day=day)
    # Roll back to previous month
    if d.month == 1:
        return date(d.year - 1, 12, day)
    return date(d.year, d.month - 1, day)


# ---------------------------------------------------------------------------
# Anthropic usage API
# ---------------------------------------------------------------------------
async def fetch_usage(start: date, end: date) -> list[dict]:
    """
    Fetch daily usage from the Anthropic usage API.
    Returns a list of records:
      [{"date": "YYYY-MM-DD", "model": str, "input_tokens": int,
        "output_tokens": int, "cache_creation_input_tokens": int,
        "cache_read_input_tokens": int}, ...]

    NOTE: The exact endpoint path may vary by Anthropic plan / org.
    Adjust ANTHROPIC_USAGE_PATH env var if needed (default: /v1/usage).
    """
    path = os.getenv("ANTHROPIC_USAGE_PATH", "/v1/usage")
    url = f"{ANTHROPIC_API_BASE}{path}"
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": ANTHROPIC_VERSION,
        "Content-Type": "application/json",
    }
    params = {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=headers, params=params)
        if resp.status_code == 404:
            logger.warning(
                "Anthropic usage endpoint returned 404. "
                "Try setting ANTHROPIC_USAGE_PATH to the correct path for your plan."
            )
            return []
        resp.raise_for_status()
        data = resp.json()
        # Normalise: accept {"data": [...]} or {"usage": [...]} or [...]
        if isinstance(data, list):
            return data
        if "data" in data:
            return data["data"]
        if "usage" in data:
            return data["usage"]
        return []


def _price_for_model(model: str) -> dict[str, float]:
    # Try exact match, then prefix match
    if model in PRICING:
        return PRICING[model]
    for key in PRICING:
        if key != "default" and model.startswith(key):
            return PRICING[key]
    return PRICING["default"]


def calc_cost(records: list[dict]) -> tuple[float, dict[str, Any]]:
    """Return (total_cost_usd, aggregated_totals)."""
    totals: dict[str, Any] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_write_tokens": 0,
        "cache_read_tokens": 0,
    }
    total_cost = 0.0
    for rec in records:
        model = rec.get("model", "default")
        p = _price_for_model(model)
        inp = rec.get("input_tokens", 0)
        out = rec.get("output_tokens", 0)
        cw = rec.get("cache_creation_input_tokens", 0)
        cr = rec.get("cache_read_input_tokens", 0)
        total_cost += (
            inp * p["input"]
            + out * p["output"]
            + cw * p["cache_write"]
            + cr * p["cache_read"]
        ) / 1_000_000
        totals["input_tokens"] += inp
        totals["output_tokens"] += out
        totals["cache_write_tokens"] += cw
        totals["cache_read_tokens"] += cr
    return total_cost, totals


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


# ---------------------------------------------------------------------------
# Discord notification
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
            logger.error("Discord responded %d: %s", resp.status_code, resp.text)
        else:
            logger.info("Discord notification sent (status %d).", resp.status_code)


def build_hourly_embed(
    period_start: date,
    cost_usd: float,
    budget_usd: float,
    totals: dict,
    extra_label: str = "",
) -> dict:
    remaining_usd = max(0.0, budget_usd - cost_usd)
    pct_used = min(100.0, cost_usd / budget_usd * 100) if budget_usd > 0 else 0.0
    pct_remaining = 100.0 - pct_used

    bar_filled = int(pct_used / 10)
    bar = "█" * bar_filled + "░" * (10 - bar_filled)

    color = 0x22C55E  # green
    if pct_remaining <= 25:
        color = 0xEF4444  # red
    elif pct_remaining <= 50:
        color = 0xF59E0B  # amber

    title = "Claude Code Usage Report"
    if extra_label:
        title = f"⚡ {extra_label} — {title}"

    now_jst = datetime.now(timezone.utc) + timedelta(hours=9)

    return {
        "title": title,
        "color": color,
        "fields": [
            {
                "name": "Billing Period",
                "value": f"`{period_start}` 〜 now",
                "inline": True,
            },
            {
                "name": "Checked at (JST)",
                "value": now_jst.strftime("%Y-%m-%d %H:%M"),
                "inline": True,
            },
            {
                "name": "Budget Usage",
                "value": (
                    f"`{bar}` {pct_used:.1f}%\n"
                    f"💰 Used: **${cost_usd:.4f}** / ${budget_usd:.2f}\n"
                    f"💚 Remaining: **${remaining_usd:.4f}** ({pct_remaining:.1f}%)"
                ),
                "inline": False,
            },
            {
                "name": "Token Breakdown",
                "value": (
                    f"📥 Input:        {fmt_tokens(totals['input_tokens'])}\n"
                    f"📤 Output:       {fmt_tokens(totals['output_tokens'])}\n"
                    f"⚡ Cache Read:   {fmt_tokens(totals['cache_read_tokens'])}\n"
                    f"🔧 Cache Write:  {fmt_tokens(totals['cache_write_tokens'])}"
                ),
                "inline": False,
            },
        ],
        "footer": {
            "text": (
                "Claude Code Usage Monitor | "
                "Coverage: API usage (GitHub Actions, Claude Code API) — "
                "Claude.ai subscription usage not included"
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
# Core check logic
# ---------------------------------------------------------------------------
async def run_check(reason: str = "hourly") -> None:
    logger.info("Running usage check (reason=%s).", reason)

    today = date.today()
    period_start = billing_period_start(today)
    period_end = today + timedelta(days=1)

    embeds: list[dict] = []

    try:
        records = await fetch_usage(period_start, period_end)
    except Exception as exc:
        logger.error("Failed to fetch usage: %s", exc)
        embeds.append(
            build_alert_embed(
                "⚠️ Usage Fetch Error",
                f"Could not retrieve usage data from Anthropic API.\n\n```{exc}```",
                0xEF4444,
            )
        )
        await send_discord(embeds)
        return

    cost_usd, totals = calc_cost(records)
    prev_cost = _state["prev_period_cost_usd"]
    was_notified_low = _state["notified_low_budget"]

    # --- Detect billing period reset ---
    reset_detected = False
    if (
        prev_cost is not None
        and cost_usd < prev_cost * 0.5  # cost dropped by >50%
        and prev_cost > 0.01
    ):
        reset_detected = True
        _state["notified_low_budget"] = False  # reset the low-budget flag
        logger.info("Billing period reset detected (prev=%.4f, now=%.4f).", prev_cost, cost_usd)
        embeds.append(
            build_alert_embed(
                "🔄 Billing Period Reset",
                f"Usage has reset to **${cost_usd:.4f}**.\nNew period started from `{period_start}`.",
                0x6366F1,
            )
        )

    # --- Detect low budget (crossing below 25% remaining) ---
    remaining_pct = (1 - cost_usd / MONTHLY_BUDGET_USD) if MONTHLY_BUDGET_USD > 0 else 1.0
    if (
        remaining_pct <= LOW_BUDGET_THRESHOLD
        and not _state["notified_low_budget"]
        and not reset_detected
    ):
        _state["notified_low_budget"] = True
        remaining_usd = max(0.0, MONTHLY_BUDGET_USD - cost_usd)
        embeds.append(
            build_alert_embed(
                "🚨 Budget Alert: Below 25% Remaining",
                (
                    f"You have **${remaining_usd:.4f}** remaining "
                    f"({remaining_pct * 100:.1f}%) of your monthly budget "
                    f"(**${MONTHLY_BUDGET_USD:.2f}**).\n\n"
                    f"Current spend: **${cost_usd:.4f}**"
                ),
                0xEF4444,
            )
        )

    # --- Always send hourly summary ---
    extra_label = "Reset" if reset_detected else ("Low Budget" if _state["notified_low_budget"] and not was_notified_low else "")
    embeds.append(
        build_hourly_embed(period_start, cost_usd, MONTHLY_BUDGET_USD, totals, extra_label)
    )

    # Update state
    _state["period_start"] = period_start
    _state["period_cost_usd"] = cost_usd
    _state["prev_period_cost_usd"] = cost_usd
    _state["last_check_at"] = datetime.now(timezone.utc)

    await send_discord(embeds)
    logger.info("Check complete. Period cost: $%.4f", cost_usd)


# ---------------------------------------------------------------------------
# FastAPI + APScheduler lifecycle
# ---------------------------------------------------------------------------
scheduler = AsyncIOScheduler(timezone="UTC")


@asynccontextmanager
async def lifespan(_: FastAPI):
    global ANTHROPIC_API_KEY, DISCORD_WEBHOOK_URL, MONTHLY_BUDGET_USD, BILLING_CYCLE_DAY, PORT

    # Load .env if present (local development)
    env_file = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())

    ANTHROPIC_API_KEY = _require("ANTHROPIC_API_KEY")
    DISCORD_WEBHOOK_URL = _require("DISCORD_WEBHOOK_URL")
    MONTHLY_BUDGET_USD = float(os.getenv("MONTHLY_BUDGET_USD", "100.0"))
    BILLING_CYCLE_DAY = max(1, min(28, int(os.getenv("BILLING_CYCLE_DAY", "1"))))
    PORT = int(os.getenv("PORT", "8080"))

    logger.info(
        "Starting Claude Code Usage Monitor | budget=$%.2f | billing day=%d",
        MONTHLY_BUDGET_USD,
        BILLING_CYCLE_DAY,
    )

    # Run immediately on startup
    import asyncio
    asyncio.create_task(run_check("startup"))

    # Schedule hourly at :00
    scheduler.add_job(
        run_check,
        CronTrigger(minute=0),
        args=["hourly"],
        id="hourly_check",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started. Next run at the top of the next hour.")

    yield

    scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped.")


app = FastAPI(title="Claude Code Usage Monitor", lifespan=lifespan)


@app.get("/")
async def health() -> dict:
    return {
        "status": "ok",
        "last_check_at": _state["last_check_at"].isoformat() if _state["last_check_at"] else None,
        "period_cost_usd": _state["period_cost_usd"],
        "notified_low_budget": _state["notified_low_budget"],
    }


@app.post("/check")
async def manual_check() -> dict:
    """Trigger a manual usage check (useful for testing)."""
    import asyncio
    asyncio.create_task(run_check("manual"))
    return {"status": "check triggered"}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        log_level="info",
    )
