"""
main.py — APScheduler entry point for Etsy AI Agent system.
Runs as a long-lived process on the Raspberry Pi.

Schedules:
  - Analytics  : every 15 minutes
  - SEO        : daily at 03:00 UTC
  - Finance    : every Monday at 06:00 UTC
  - Marketing  : daily at 09:00 UTC
  - CEO digest : daily at 08:00 UTC

Telegram webhook Flask server runs on port 5002 in a background thread.
"""

import logging
import signal
import sys
import threading
import time
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

from agents.analytics_agent import run_analytics
from agents.seo_agent import run_seo
from agents.finance_agent import run_finance
from agents.marketing_agent import run_marketing
from agents.ceo_agent import run_ceo, get_command_handlers
from db.database import init_db
from integrations.telegram_client import TelegramClient, create_webhook_app

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("main")

# ---------------------------------------------------------------------------
# Paths / config
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).parent
_ENV_PATH = _PROJECT_ROOT / ".env"
_LOGS_DIR = _PROJECT_ROOT / "logs"
_LOGS_DIR.mkdir(parents=True, exist_ok=True)

WEBHOOK_PORT = 5002


# ---------------------------------------------------------------------------
# Pause-aware agent wrappers
# ---------------------------------------------------------------------------

def _is_paused(agent_name: str) -> bool:
    """Return True if logs/{agent_name}.paused flag file exists."""
    return (_LOGS_DIR / f"{agent_name}.paused").exists()


def _guarded(agent_name: str, fn, *fn_args, **fn_kwargs):
    """Run fn only if the agent is not paused. Logs skip/start/error."""
    if _is_paused(agent_name):
        logger.info("Agent '%s' is paused — skipping scheduled run.", agent_name)
        return
    logger.info("Scheduled run starting: %s", agent_name)
    try:
        fn(*fn_args, **fn_kwargs)
        logger.info("Scheduled run complete: %s", agent_name)
    except Exception:
        logger.exception("Scheduled run FAILED: %s", agent_name)


# ---------------------------------------------------------------------------
# Scheduled job functions (thin wrappers — APScheduler calls these)
# ---------------------------------------------------------------------------

def _job_analytics():
    _guarded("analytics", run_analytics)


def _job_seo():
    _guarded("seo", run_seo, dry_run=False)


def _job_finance():
    _guarded("finance", run_finance, dry_run=False)


def _job_marketing():
    _guarded("marketing", run_marketing)


def _job_ceo():
    _guarded("ceo", run_ceo)


# ---------------------------------------------------------------------------
# Telegram webhook thread
# ---------------------------------------------------------------------------

def _start_webhook_server():
    """Start the Flask webhook server in a daemon thread on WEBHOOK_PORT."""
    handlers = get_command_handlers()
    app = create_webhook_app(handlers)

    def _serve():
        # Use Werkzeug directly to suppress the dev-server warning and allow
        # clean shutdown when the main process exits.
        from werkzeug.serving import make_server
        server = make_server("0.0.0.0", WEBHOOK_PORT, app)
        logger.info("Telegram webhook server listening on port %d", WEBHOOK_PORT)
        server.serve_forever()

    t = threading.Thread(target=_serve, daemon=True, name="webhook-server")
    t.start()
    return t


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

def _make_shutdown_handler(scheduler: BackgroundScheduler):
    def _handler(signum, frame):
        sig_name = signal.Signals(signum).name
        logger.info("Received %s — shutting down scheduler.", sig_name)
        scheduler.shutdown(wait=False)
        logger.info("Etsy Agent System stopped.")
        sys.exit(0)
    return _handler


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    load_dotenv(_ENV_PATH)

    # Initialise DB
    init_db()
    logger.info("Etsy Agent System starting...")

    # Send startup notification to Telegram
    try:
        telegram = TelegramClient()
        telegram.send_message("🚀 Etsy Agent System started")
        logger.info("Startup Telegram notification sent.")
    except Exception:
        logger.exception("Could not send Telegram startup notification — continuing.")

    # Build scheduler
    scheduler = BackgroundScheduler(timezone="UTC")

    # Analytics: every 15 minutes
    scheduler.add_job(_job_analytics, "interval", minutes=15, id="analytics",
                      name="Analytics Agent")

    # SEO: daily at 03:00 UTC
    scheduler.add_job(_job_seo, "cron", hour=3, minute=0, id="seo",
                      name="SEO Agent")

    # Finance: every Monday at 06:00 UTC
    scheduler.add_job(_job_finance, "cron", day_of_week="mon", hour=6, minute=0,
                      id="finance", name="Finance Agent")

    # Marketing: daily at 09:00 UTC
    scheduler.add_job(_job_marketing, "cron", hour=9, minute=0, id="marketing",
                      name="Marketing Agent")

    # CEO digest: daily at 08:00 UTC
    scheduler.add_job(_job_ceo, "cron", hour=8, minute=0, id="ceo",
                      name="CEO Digest Agent")

    # Register graceful shutdown handlers
    shutdown_handler = _make_shutdown_handler(scheduler)
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    # Start Telegram webhook server
    _start_webhook_server()

    # Start scheduler
    scheduler.start()
    logger.info(
        "Scheduler started with %d jobs. Webhook on port %d. Keeping alive...",
        len(scheduler.get_jobs()),
        WEBHOOK_PORT,
    )

    # Keep the main thread alive; check for restart flag each cycle
    restart_flag = Path("logs/restart_requested.flag")
    while True:
        time.sleep(60)
        if restart_flag.exists():
            restart_flag.unlink()
            logger.info("Restart flag detected — shutting down cleanly for systemd restart")
            scheduler.shutdown()
            sys.exit(0)


if __name__ == "__main__":
    main()
