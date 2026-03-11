#!/usr/bin/env python3
"""
Claude Code Usage Reporter
Reads usage data from ~/.claude/projects/ and sends a report to Discord.
"""

import json
import os
import sys
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError
from collections import defaultdict

# === Pricing (per 1M tokens) ===
PRICING = {
    "input": 3.00,
    "output": 15.00,
    "cache_creation": 3.75,
    "cache_read": 0.30,
}


def load_env():
    """Load .env file if it exists."""
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())


def get_webhook_url():
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        print("Error: DISCORD_WEBHOOK_URL is not set.", file=sys.stderr)
        sys.exit(1)
    return url


def iter_sessions():
    """Yield (project_name, session_id, entries) for all sessions."""
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.exists():
        return

    for project_dir in sorted(projects_dir.iterdir()):
        if not project_dir.is_dir():
            continue
        project_name = project_dir.name
        for jsonl_file in sorted(project_dir.glob("*.jsonl")):
            session_id = jsonl_file.stem
            entries = []
            try:
                with open(jsonl_file, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            except OSError:
                continue
            yield project_name, session_id, entries


def collect_usage(since: datetime, until: datetime):
    """
    Collect usage stats between [since, until).
    Returns a dict with aggregated stats per project.
    """
    stats = defaultdict(lambda: {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "sessions": set(),
        "messages": 0,
    })

    for project_name, session_id, entries in iter_sessions():
        for entry in entries:
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

            s = stats[project_name]
            s["input_tokens"] += usage.get("input_tokens", 0)
            s["output_tokens"] += usage.get("output_tokens", 0)
            s["cache_creation_tokens"] += usage.get("cache_creation_input_tokens", 0)
            s["cache_read_tokens"] += usage.get("cache_read_input_tokens", 0)
            s["sessions"].add(session_id)
            s["messages"] += 1

    return stats


def calc_cost(s):
    """Calculate estimated cost in USD for a stats dict."""
    cost = (
        s["input_tokens"] * PRICING["input"]
        + s["output_tokens"] * PRICING["output"]
        + s["cache_creation_tokens"] * PRICING["cache_creation"]
        + s["cache_read_tokens"] * PRICING["cache_read"]
    ) / 1_000_000
    return cost


def fmt_tokens(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def build_embed(period_label: str, since: datetime, until: datetime, stats: dict):
    """Build a Discord embed payload."""
    total = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "sessions": set(),
        "messages": 0,
    }
    for s in stats.values():
        for k in ("input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens", "messages"):
            total[k] += s[k]
        total["sessions"] |= s["sessions"]

    total_cost = calc_cost(total)
    total_sessions = len(total["sessions"])

    # Build per-project field
    project_lines = []
    sorted_projects = sorted(stats.items(), key=lambda x: calc_cost(x[1]), reverse=True)
    for proj, s in sorted_projects[:10]:  # top 10 projects
        cost = calc_cost(s)
        # Convert project slug back to path-like name
        display_name = proj.replace("-home-hsm-hx-", "~/").replace("-", "/")
        project_lines.append(
            f"`{display_name}` — ${cost:.4f} ({len(s['sessions'])} sessions, {s['messages']} msgs)"
        )

    date_range = f"{since.strftime('%Y-%m-%d')} ~ {(until - timedelta(seconds=1)).strftime('%Y-%m-%d')}"

    embed = {
        "title": f"Claude Code {period_label} Usage Report",
        "description": f"**{date_range}**",
        "color": 0x7C3AED,  # purple
        "fields": [
            {
                "name": "Summary",
                "value": (
                    f"💰 **Estimated Cost**: ${total_cost:.4f}\n"
                    f"📨 **Sessions**: {total_sessions}\n"
                    f"💬 **Messages**: {total['messages']}\n"
                    f"📥 Input: {fmt_tokens(total['input_tokens'])} tokens\n"
                    f"📤 Output: {fmt_tokens(total['output_tokens'])} tokens\n"
                    f"⚡ Cache Read: {fmt_tokens(total['cache_read_tokens'])} tokens\n"
                    f"🔧 Cache Create: {fmt_tokens(total['cache_creation_tokens'])} tokens"
                ),
                "inline": False,
            },
        ],
        "footer": {"text": "Claude Code Usage Reporter"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if project_lines:
        embed["fields"].append({
            "name": f"Top Projects ({len(stats)} total)",
            "value": "\n".join(project_lines) if project_lines else "No data",
            "inline": False,
        })

    return embed


def send_discord(webhook_url: str, embed: dict):
    payload = json.dumps({"embeds": [embed]}).encode("utf-8")
    req = Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=10) as resp:
            if resp.status not in (200, 204):
                print(f"Discord responded with status {resp.status}", file=sys.stderr)
    except URLError as e:
        print(f"Failed to send to Discord: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    load_env()

    parser = argparse.ArgumentParser(description="Send Claude Code usage stats to Discord")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--daily", action="store_true", help="Report yesterday's usage")
    group.add_argument("--weekly", action="store_true", help="Report last week's usage")
    group.add_argument("--today", action="store_true", help="Report today's usage so far")
    parser.add_argument("--dry-run", action="store_true", help="Print the embed without sending")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)

    if args.daily:
        yesterday = (now - timedelta(days=1)).date()
        since = datetime(yesterday.year, yesterday.month, yesterday.day, tzinfo=timezone.utc)
        until = since + timedelta(days=1)
        label = "Daily"
    elif args.today:
        today = now.date()
        since = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
        until = since + timedelta(days=1)
        label = "Today's"
    else:  # weekly
        # Report Mon-Sun of last week
        days_since_monday = now.weekday()  # 0=Mon
        last_monday = (now - timedelta(days=days_since_monday + 7)).date()
        since = datetime(last_monday.year, last_monday.month, last_monday.day, tzinfo=timezone.utc)
        until = since + timedelta(days=7)
        label = "Weekly"

    print(f"Collecting {label} usage from {since} to {until}...")
    stats = collect_usage(since, until)

    if not stats:
        print("No usage data found for the period.")
        if not args.dry_run:
            # Still send a "no data" notification
            embed = {
                "title": f"Claude Code {label} Usage Report",
                "description": f"No usage data found for this period.",
                "color": 0x6B7280,
                "footer": {"text": "Claude Code Usage Reporter"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            send_discord(get_webhook_url(), embed)
        return

    embed = build_embed(label, since, until, stats)

    if args.dry_run:
        print(json.dumps({"embeds": [embed]}, indent=2, ensure_ascii=False))
    else:
        webhook_url = get_webhook_url()
        send_discord(webhook_url, embed)
        print("Report sent to Discord successfully.")


if __name__ == "__main__":
    main()
