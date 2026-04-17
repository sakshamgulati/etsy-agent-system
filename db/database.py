"""
database.py — SQLite helper module for the Etsy AI Agent system.

All timestamps are stored as UTC ISO 8601 strings (e.g. "2026-04-16T12:00:00Z").
DB file path is controlled by the ETSY_DB_PATH environment variable, defaulting
to <project_root>/db/etsy_agent.db.
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = os.environ.get(
    "ETSY_DB_PATH",
    str(_PROJECT_ROOT / "db" / "etsy_agent.db"),
)

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> str:
    """Return current UTC time as an ISO 8601 string with trailing Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Core connection helpers
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Create the DB file (if needed) and apply schema.sql (idempotent)."""
    db_file = Path(DB_PATH)
    db_file.parent.mkdir(parents=True, exist_ok=True)

    schema = _SCHEMA_PATH.read_text()
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(schema)
        conn.commit()


def get_conn() -> sqlite3.Connection:
    """Return a sqlite3.Connection with row_factory set to sqlite3.Row."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def _conn_ctx():
    """Internal context manager for auto-commit/rollback connections."""
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Agent run tracking
# ---------------------------------------------------------------------------

@contextmanager
def record_agent_run(agent_name: str, summary: str = None, error_detail: str = None):
    """
    Context manager that records an agent run in agent_runs.

    Usage:
        with record_agent_run("listing_agent") as run_id:
            ... do work ...

    On normal exit:  status → 'success', finished_at set.
    On exception:    status → 'error',   error_detail set, exception re-raised.

    Uses a single connection for both INSERT and UPDATE so there is no window
    where the row can be stuck at 'running' if the process is killed.
    """
    started_at = datetime.now(timezone.utc).isoformat()
    conn = get_conn()
    run_id = None
    try:
        cur = conn.execute(
            "INSERT INTO agent_runs (agent_name, started_at, status) VALUES (?, ?, 'running')",
            (agent_name, started_at)
        )
        run_id = cur.lastrowid
        conn.commit()
        yield run_id
        finished_at = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE agent_runs SET status='success', finished_at=?, summary=? WHERE id=?",
            (finished_at, summary, run_id)
        )
        conn.commit()
    except Exception as e:
        finished_at = datetime.now(timezone.utc).isoformat()
        if run_id:
            conn.execute(
                "UPDATE agent_runs SET status='error', finished_at=?, error_detail=? WHERE id=?",
                (finished_at, str(e), run_id)
            )
            conn.commit()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Listing stats
# ---------------------------------------------------------------------------

def insert_listing_stat(
    listing_id: str,
    title: str,
    price: float,
    views: int,
    favorites: int,
    quantity: int,
    state: str,
) -> int:
    """Insert a listing snapshot. Returns the new row id."""
    with _conn_ctx() as conn:
        cur = conn.execute(
            """
            INSERT INTO listing_stats
                (listing_id, title, price, views, favorites, quantity, state, snapshot_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (listing_id, title, price, views, favorites, quantity, state, _utcnow()),
        )
        return cur.lastrowid


def get_latest_listing_stats() -> list[sqlite3.Row]:
    """Return the most recent snapshot row for each distinct listing_id."""
    with _conn_ctx() as conn:
        rows = conn.execute(
            """
            SELECT ls.*
            FROM listing_stats ls
            INNER JOIN (
                SELECT listing_id, MAX(snapshot_at) AS max_snap
                FROM listing_stats
                GROUP BY listing_id
            ) latest
            ON ls.listing_id = latest.listing_id
            AND ls.snapshot_at = latest.max_snap
            ORDER BY ls.listing_id
            """
        ).fetchall()
    return rows


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def insert_order(
    receipt_id: str,
    listing_id: str,
    amount_paid: float,
    currency: str,
    status: str,
    created_at: str,
) -> int:
    """Upsert an order (unique on receipt_id). Returns the row id."""
    with _conn_ctx() as conn:
        cur = conn.execute(
            """
            INSERT INTO orders
                (receipt_id, listing_id, amount_paid, currency, status, created_at, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(receipt_id) DO UPDATE SET
                listing_id   = excluded.listing_id,
                amount_paid  = excluded.amount_paid,
                currency     = excluded.currency,
                status       = excluded.status,
                created_at   = excluded.created_at,
                recorded_at  = excluded.recorded_at
            """,
            (receipt_id, listing_id, amount_paid, currency, status, created_at, _utcnow()),
        )
        return cur.lastrowid


# ---------------------------------------------------------------------------
# Shop stats
# ---------------------------------------------------------------------------

def insert_shop_stat(
    stat_date: str,
    visits: int,
    views: int,
    orders: int,
    revenue: float,
) -> int:
    """Upsert a daily shop stat snapshot (unique on stat_date). Returns the row id."""
    with _conn_ctx() as conn:
        cur = conn.execute(
            """
            INSERT INTO shop_stats (stat_date, visits, views, orders, revenue, snapshot_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(stat_date) DO UPDATE SET
                visits=excluded.visits,
                views=excluded.views,
                orders=excluded.orders,
                revenue=excluded.revenue,
                snapshot_at=excluded.snapshot_at
            """,
            (stat_date, visits, views, orders, revenue, _utcnow()),
        )
        return cur.lastrowid


# ---------------------------------------------------------------------------
# SEO changes
# ---------------------------------------------------------------------------

def insert_seo_change(
    listing_id: str,
    field: str,
    old_value: str,
    new_value: str,
    approved: int = 1,
) -> int:
    """Log an SEO field update. Returns the new row id."""
    with _conn_ctx() as conn:
        cur = conn.execute(
            """
            INSERT INTO seo_changes
                (listing_id, field, old_value, new_value, changed_at, approved)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (listing_id, field, old_value, new_value, _utcnow(), approved),
        )
        return cur.lastrowid


# ---------------------------------------------------------------------------
# Pricing recommendations
# ---------------------------------------------------------------------------

def insert_pricing_recommendation(
    listing_id: str,
    current_price: float,
    recommended_price: float,
    rationale: str,
) -> int:
    """Create a pending pricing recommendation. Returns the new row id."""
    with _conn_ctx() as conn:
        cur = conn.execute(
            """
            INSERT INTO pricing_recommendations
                (listing_id, current_price, recommended_price, rationale, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (listing_id, current_price, recommended_price, rationale, _utcnow()),
        )
        return cur.lastrowid


def update_pricing_recommendation_status(rec_id: int, status: str) -> None:
    """Set status ('approved', 'rejected', 'applied') on a recommendation."""
    with _conn_ctx() as conn:
        conn.execute(
            """
            UPDATE pricing_recommendations
            SET status = ?, resolved_at = ?
            WHERE id = ?
            """,
            (status, _utcnow(), rec_id),
        )


def update_pricing_recommendation_applied(rec_id: int) -> None:
    """Mark a recommendation as applied."""
    with _conn_ctx() as conn:
        conn.execute(
            "UPDATE pricing_recommendations SET status='applied', resolved_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), rec_id)
        )


def get_pending_pricing_recommendations() -> list[sqlite3.Row]:
    """Return all pricing recommendations with status = 'pending'."""
    with _conn_ctx() as conn:
        return conn.execute(
            "SELECT * FROM pricing_recommendations WHERE status = 'pending' ORDER BY created_at"
        ).fetchall()


# ---------------------------------------------------------------------------
# Ad spend
# ---------------------------------------------------------------------------

def insert_ad_spend(
    listing_id: str,
    date: str,
    spend: float,
    clicks: int,
    impressions: int,
    sales: int,
    roas: float,
) -> int:
    """Log daily ad spend data. Returns the new row id."""
    with _conn_ctx() as conn:
        cur = conn.execute(
            """
            INSERT INTO ad_spend
                (listing_id, date, spend, clicks, impressions, sales, roas, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (listing_id, date, spend, clicks, impressions, sales, roas, _utcnow()),
        )
        return cur.lastrowid


# ---------------------------------------------------------------------------
# Social posts
# ---------------------------------------------------------------------------

def insert_social_post(
    listing_id: str,
    platform: str,
    post_id: str,
    caption: str,
    utm_params: str,
) -> int:
    """Log a published social media post. Returns the new row id."""
    with _conn_ctx() as conn:
        cur = conn.execute(
            """
            INSERT INTO social_posts
                (listing_id, platform, post_id, caption, utm_params, posted_at, status)
            VALUES (?, ?, ?, ?, ?, ?, 'published')
            """,
            (listing_id, platform, post_id, caption, utm_params, _utcnow()),
        )
        return cur.lastrowid


# ---------------------------------------------------------------------------
# Pending approvals
# ---------------------------------------------------------------------------

def create_pending_approval(
    agent_name: str,
    action_type: str,
    payload_dict: dict,
) -> int:
    """Create an approval request. payload_dict is serialised to JSON. Returns row id."""
    with _conn_ctx() as conn:
        cur = conn.execute(
            """
            INSERT INTO pending_approvals
                (agent_name, action_type, payload, status, created_at)
            VALUES (?, ?, ?, 'pending', ?)
            """,
            (agent_name, action_type, json.dumps(payload_dict), _utcnow()),
        )
        return cur.lastrowid


def resolve_approval(approval_id: int, status: str) -> None:
    """Set status ('approved' or 'rejected') on a pending approval."""
    with _conn_ctx() as conn:
        conn.execute(
            """
            UPDATE pending_approvals
            SET status = ?, resolved_at = ?
            WHERE id = ?
            """,
            (status, _utcnow(), approval_id),
        )


def get_pending_approvals() -> list[sqlite3.Row]:
    """Return all approvals with status = 'pending'."""
    with _conn_ctx() as conn:
        return conn.execute(
            "SELECT * FROM pending_approvals WHERE status = 'pending' ORDER BY created_at"
        ).fetchall()


# ---------------------------------------------------------------------------
# Agent run summary
# ---------------------------------------------------------------------------

def get_agent_run_summary(hours: int = 24) -> dict:
    """
    Return a summary of agent runs in the last N hours.

    Returns:
        {
            "<agent_name>": {
                "runs": <int>,
                "errors": <int>,
                "last_run": "<ISO timestamp or None>"
            },
            ...
        }
    """
    since = datetime.now(timezone.utc)
    # Subtract hours manually to stay stdlib-only
    from datetime import timedelta
    since = since - timedelta(hours=hours)
    since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")

    with _conn_ctx() as conn:
        rows = conn.execute(
            """
            SELECT
                agent_name,
                COUNT(*) AS runs,
                SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errors,
                MAX(started_at) AS last_run
            FROM agent_runs
            WHERE started_at >= ?
            GROUP BY agent_name
            """,
            (since_str,),
        ).fetchall()

    return {
        row["agent_name"]: {
            "runs": row["runs"],
            "errors": row["errors"],
            "last_run": row["last_run"],
        }
        for row in rows
    }
