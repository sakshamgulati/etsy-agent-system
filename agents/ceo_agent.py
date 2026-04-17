"""
agents/ceo_agent.py — CEO Orchestrator Agent for the Etsy AI Agent system.

Responsibilities:
- Gather data from all other agents via the DB
- Call Claude (haiku) to generate a strategic daily digest
- Send the digest to Telegram
- Expose Telegram command handlers: /status /listings /budget /run /pause /resume /logs /report
"""

import os
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from db.database import (
    init_db,
    get_conn,
    record_agent_run,
    get_agent_run_summary,
    get_latest_listing_stats,
    get_pending_approvals,
)
from integrations.etsy_client import EtsyClient
from integrations.telegram_client import TelegramClient
from agents.logger import get_logger

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"
_LOG_FILE = str(_PROJECT_ROOT / "logs" / "ceo.log")
_PROMPT_TEMPLATE_PATH = _PROJECT_ROOT / "prompts" / "ceo_digest.md"
_LOGS_DIR = _PROJECT_ROOT / "logs"

CLAUDE_MODEL = "claude-haiku-4-5-20251001"
AD_BUDGET_CAP = 100.0  # weekly cap in USD

log = get_logger("ceo", _LOG_FILE)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_weekly_revenue() -> float:
    """Sum orders.amount_paid for the last 7 days."""
    since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount_paid), 0) AS total FROM orders WHERE created_at >= ?",
            (since,),
        ).fetchone()
        return float(row["total"]) if row else 0.0
    finally:
        conn.close()


def _get_weekly_ad_spend() -> dict:
    """
    Return this week's ad spend summary from the ad_spend table.

    Returns:
        {
            "total": float,
            "by_listing": [{"listing_id": str, "spend": float}, ...]
        }
    """
    monday = datetime.now(timezone.utc)
    monday = monday - timedelta(days=monday.weekday())
    since = monday.strftime("%Y-%m-%d")

    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT listing_id, SUM(spend) AS spend
            FROM ad_spend
            WHERE date >= ?
            GROUP BY listing_id
            ORDER BY spend DESC
            """,
            (since,),
        ).fetchall()
        total = sum(float(r["spend"]) for r in rows)
        by_listing = [{"listing_id": r["listing_id"], "spend": float(r["spend"])} for r in rows]
        return {"total": total, "by_listing": by_listing}
    finally:
        conn.close()


def _format_agent_summary(summary: dict) -> str:
    if not summary:
        return "No agent runs recorded in the last 24 h."
    lines = []
    for agent, data in summary.items():
        last = data.get("last_run") or "never"
        errors = data.get("errors", 0)
        runs = data.get("runs", 0)
        status = "OK" if errors == 0 else f"{errors} errors"
        lines.append(f"- {agent}: {runs} runs, last={last}, {status}")
    return "\n".join(lines)


def _format_listing_stats(stats) -> str:
    if not stats:
        return "No listing stats available."
    lines = []
    for row in stats:
        lines.append(
            f"- [{row['listing_id']}] {row['title']!r}: "
            f"views={row['views']}, favs={row['favorites']}, price=${row['price']:.2f}"
        )
    return "\n".join(lines)


def _format_pending_approvals(approvals) -> str:
    if not approvals:
        return "None pending."
    lines = []
    for a in approvals:
        lines.append(f"- [{a['id']}] {a['agent_name']} / {a['action_type']} (since {a['created_at']})")
    return "\n".join(lines)


def _top_listing(stats) -> str:
    if not stats:
        return "N/A"
    top = max(stats, key=lambda r: (r["views"] or 0))
    return (
        f"ID={top['listing_id']} | {top['title']!r} | "
        f"views={top['views']}, favs={top['favorites']}, ${top['price']:.2f}"
    )


VALID_AGENTS = {"analytics", "seo", "finance", "marketing", "ceo"}

MAX_TG_LEN = 4096


def _truncate(text: str) -> str:
    """Truncate text to Telegram's 4096-char limit."""
    if len(text) > MAX_TG_LEN:
        text = text[:MAX_TG_LEN - 20] + "\n\n_(truncated)_"
    return text


def _call_claude(prompt: str) -> str:
    """Call Claude haiku and return the response text."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def _build_digest() -> str:
    """Gather all data, fill the prompt template, call Claude, return digest text."""
    template = _PROMPT_TEMPLATE_PATH.read_text()

    agent_summary = get_agent_run_summary(hours=24)
    listing_stats = get_latest_listing_stats()
    pending_approvals = get_pending_approvals()
    weekly_revenue = _get_weekly_revenue()
    weekly_spend = _get_weekly_ad_spend()

    prompt = template.format(
        date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        agent_summary=_format_agent_summary(agent_summary),
        listing_stats=_format_listing_stats(listing_stats),
        weekly_revenue=f"${weekly_revenue:.2f}",
        weekly_spend=(
            f"${weekly_spend['total']:.2f} of ${AD_BUDGET_CAP:.0f} cap\n"
            + "\n".join(
                f"  - {item['listing_id']}: ${item['spend']:.2f}"
                for item in weekly_spend["by_listing"]
            )
        ),
        pending_approvals=_format_pending_approvals(pending_approvals),
        top_listing=_top_listing(listing_stats),
    )

    return _call_claude(prompt)


# ---------------------------------------------------------------------------
# Daily digest runner
# ---------------------------------------------------------------------------

def run_ceo() -> dict:
    """
    Execute the CEO daily digest:
    1. Gather data from DB
    2. Call Claude to generate digest
    3. Send via Telegram
    """
    load_dotenv(_ENV_PATH)
    init_db()

    telegram = TelegramClient()
    summary = {"digest_sent": False, "errors": []}

    with record_agent_run("ceo") as run_id:
        log.info("ceo_run_started", run_id=run_id)
        try:
            digest_text = _truncate(_build_digest())
            telegram.send_digest(digest_text)
            summary["digest_sent"] = True
            log.info("ceo_digest_sent", chars=len(digest_text))
        except Exception as exc:
            err = f"CEO digest failed: {exc}"
            summary["errors"].append(err)
            log.error("ceo_digest_error", error=str(exc))
            raise

        log.info("ceo_run_complete", digest_sent=summary["digest_sent"])

    return summary


# ---------------------------------------------------------------------------
# Telegram command handlers
# ---------------------------------------------------------------------------

def _ago(ts: str | None) -> str:
    """Convert an ISO timestamp to a human-readable 'X ago' string."""
    if not ts:
        return "never"
    try:
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        secs = int(delta.total_seconds())
        if secs < 120:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"
    except Exception:
        return ts


def handle_status(chat_id, message_id, args):
    """Reply with a live system status summary."""
    load_dotenv(_ENV_PATH)
    telegram = TelegramClient()

    try:
        summary = get_agent_run_summary(hours=24)
        ad_spend = _get_weekly_ad_spend()

        total_errors = sum(d.get("errors", 0) for d in summary.values())

        def _agent_line(name: str, emoji: str = "") -> str:
            d = summary.get(name, {})
            last = _ago(d.get("last_run"))
            ok = "✅" if d.get("errors", 0) == 0 else "❌"
            return f"  {emoji or name.capitalize()}: {last} {ok}"

        lines = [
            "📡 *System Status* [live]",
            _agent_line("analytics", "Analytics"),
            _agent_line("seo", "SEO"),
            _agent_line("finance", "Finance"),
            _agent_line("marketing", "Marketing"),
            "  Software: always-on ✅",
            f"💰 Budget: ${ad_spend['total']:.2f}/${AD_BUDGET_CAP:.0f} this week",
            f"🚨 Errors (24h): {total_errors}",
        ]
        telegram.reply_to_command(chat_id, message_id, "\n".join(lines))
        log.info("handle_status_ok", chat_id=chat_id)
    except Exception as exc:
        log.error("handle_status_error", error=str(exc))
        telegram.reply_to_command(chat_id, message_id, f"❌ Status error: {exc}")


def handle_listings(chat_id, message_id, args):
    """Reply with the latest listing stats table."""
    load_dotenv(_ENV_PATH)
    telegram = TelegramClient()

    try:
        stats = get_latest_listing_stats()
        if not stats:
            telegram.reply_to_command(chat_id, message_id, "No listing data yet.")
            return

        lines = ["📋 *Latest Listings*"]
        for row in stats:
            lines.append(
                f"`{row['listing_id']}` {row['title'][:28]!r}\n"
                f"  👁 {row['views']}  ♥ {row['favorites']}  💲{row['price']:.2f}"
            )
        telegram.reply_to_command(chat_id, message_id, "\n".join(lines))
        log.info("handle_listings_ok", chat_id=chat_id, count=len(stats))
    except Exception as exc:
        log.error("handle_listings_error", error=str(exc))
        telegram.reply_to_command(chat_id, message_id, f"❌ Listings error: {exc}")


def handle_budget(chat_id, message_id, args):
    """Reply with this week's ad spend breakdown."""
    load_dotenv(_ENV_PATH)
    telegram = TelegramClient()

    try:
        spend = _get_weekly_ad_spend()
        remaining = AD_BUDGET_CAP - spend["total"]
        lines = [
            f"💰 *Ad Budget This Week*",
            f"Spent: ${spend['total']:.2f} / ${AD_BUDGET_CAP:.0f}",
            f"Remaining: ${remaining:.2f}",
        ]
        if spend["by_listing"]:
            lines.append("*By listing:*")
            for item in spend["by_listing"]:
                lines.append(f"  `{item['listing_id']}`: ${item['spend']:.2f}")
        else:
            lines.append("No ad spend recorded yet.")

        telegram.reply_to_command(chat_id, message_id, "\n".join(lines))
        log.info("handle_budget_ok", chat_id=chat_id, total=spend["total"])
    except Exception as exc:
        log.error("handle_budget_error", error=str(exc))
        telegram.reply_to_command(chat_id, message_id, f"❌ Budget error: {exc}")


def handle_run(chat_id, message_id, args):
    """
    Trigger an agent run in a background thread.
    Usage: /run <agent_name>
    Supported: analytics, seo, finance, marketing
    """
    load_dotenv(_ENV_PATH)
    telegram = TelegramClient()

    if not args:
        telegram.reply_to_command(
            chat_id, message_id,
            "Usage: /run <analytics|seo|finance|marketing>"
        )
        return

    agent_name = args[0].lower()

    # Import lazily to avoid circular issues
    _agent_map = {
        "analytics": lambda: __import__(
            "agents.analytics_agent", fromlist=["run_analytics"]
        ).run_analytics(),
        "seo": lambda: __import__(
            "agents.seo_agent", fromlist=["run_seo"]
        ).run_seo(dry_run=False),
        "finance": lambda: __import__(
            "agents.finance_agent", fromlist=["run_finance"]
        ).run_finance(dry_run=False),
        "marketing": lambda: __import__(
            "agents.marketing_agent", fromlist=["run_marketing"]
        ).run_marketing(),
    }

    if agent_name not in _agent_map:
        telegram.reply_to_command(
            chat_id, message_id,
            f"Unknown agent '{agent_name}'. Choose: analytics, seo, finance, marketing"
        )
        return

    def _run():
        try:
            _agent_map[agent_name]()
            log.info("handle_run_complete", agent=agent_name)
        except Exception as exc:
            log.error("handle_run_error", agent=agent_name, error=str(exc))

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    telegram.reply_to_command(chat_id, message_id, f"▶️ Running {agent_name}...")
    log.info("handle_run_dispatched", agent=agent_name, chat_id=chat_id)


def handle_pause(chat_id, message_id, args):
    """
    Pause an agent by writing a flag file logs/{agent}.paused.
    Usage: /pause <agent_name>
    """
    load_dotenv(_ENV_PATH)
    telegram = TelegramClient()

    if not args:
        telegram.reply_to_command(chat_id, message_id, "Usage: /pause <agent_name>")
        return

    agent_name = args[0].lower() if args else ""
    if agent_name not in VALID_AGENTS:
        telegram.reply_to_command(
            chat_id, message_id,
            f"❌ Unknown agent: {agent_name}. Valid: {', '.join(sorted(VALID_AGENTS))}"
        )
        return

    flag_file = _LOGS_DIR / f"{agent_name}.paused"
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    flag_file.touch()

    telegram.reply_to_command(chat_id, message_id, f"⏸ {agent_name} paused")
    log.info("handle_pause", agent=agent_name, chat_id=chat_id)


def handle_resume(chat_id, message_id, args):
    """
    Resume an agent by removing the flag file logs/{agent}.paused.
    Usage: /resume <agent_name>
    """
    load_dotenv(_ENV_PATH)
    telegram = TelegramClient()

    if not args:
        telegram.reply_to_command(chat_id, message_id, "Usage: /resume <agent_name>")
        return

    agent_name = args[0].lower() if args else ""
    if agent_name not in VALID_AGENTS:
        telegram.reply_to_command(
            chat_id, message_id,
            f"❌ Unknown agent: {agent_name}. Valid: {', '.join(sorted(VALID_AGENTS))}"
        )
        return

    flag_file = _LOGS_DIR / f"{agent_name}.paused"

    if flag_file.exists():
        flag_file.unlink()
        msg = f"▶️ {agent_name} resumed"
    else:
        msg = f"ℹ️ {agent_name} was not paused"

    telegram.reply_to_command(chat_id, message_id, msg)
    log.info("handle_resume", agent=agent_name, chat_id=chat_id)


def handle_logs(chat_id, message_id, args):
    """Send the last 10 lines of logs/errors.log to Telegram."""
    load_dotenv(_ENV_PATH)
    telegram = TelegramClient()

    errors_log = _LOGS_DIR / "errors.log"
    try:
        if errors_log.exists():
            lines = errors_log.read_text().splitlines()
            tail = lines[-10:] if len(lines) > 10 else lines
            text = "📄 *Last 10 error log lines:*\n```\n" + "\n".join(tail) + "\n```"
        else:
            text = "ℹ️ No errors.log file found."
        telegram.reply_to_command(chat_id, message_id, text)
        log.info("handle_logs_ok", chat_id=chat_id)
    except Exception as exc:
        log.error("handle_logs_error", error=str(exc))
        telegram.reply_to_command(chat_id, message_id, f"❌ Logs error: {exc}")


def handle_report(chat_id, message_id, args):
    """Generate and send the CEO digest on demand."""
    load_dotenv(_ENV_PATH)
    telegram = TelegramClient()

    try:
        telegram.reply_to_command(chat_id, message_id, "⏳ Generating report...")
        digest_text = _truncate(_build_digest())
        telegram.reply_to_command(chat_id, message_id, digest_text)
        log.info("handle_report_ok", chat_id=chat_id, chars=len(digest_text))
    except Exception as exc:
        log.error("handle_report_error", error=str(exc))
        telegram.reply_to_command(chat_id, message_id, f"❌ Report error: {exc}")


# ---------------------------------------------------------------------------
# Command handler registry
# ---------------------------------------------------------------------------

def get_command_handlers() -> dict:
    """Return the mapping of Telegram command strings to handler functions."""
    return {
        "status": handle_status,
        "listings": handle_listings,
        "budget": handle_budget,
        "run": handle_run,
        "pause": handle_pause,
        "resume": handle_resume,
        "logs": handle_logs,
        "report": handle_report,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    result = run_ceo()
    print(result)
