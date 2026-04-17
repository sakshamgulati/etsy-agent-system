"""
test_database.py — Tests for db/database.py

All tests use the tmp_db fixture (real SQLite, no mocking).
"""

import sqlite3
import pytest
from datetime import datetime, timezone, timedelta

import db.database as db_module
from db.database import (
    init_db,
    get_conn,
    record_agent_run,
    insert_listing_stat,
    get_latest_listing_stats,
    insert_order,
    insert_shop_stat,
    insert_seo_change,
    insert_pricing_recommendation,
    update_pricing_recommendation_status,
    update_pricing_recommendation_applied,
    get_pending_pricing_recommendations,
    insert_ad_spend,
    insert_social_post,
    create_pending_approval,
    resolve_approval,
    get_pending_approvals,
    get_agent_run_summary,
)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_init_db_creates_tables(tmp_db):
    """init_db() should create all 9 required tables."""
    conn = sqlite3.connect(tmp_db)
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = {row[0] for row in cursor.fetchall()}
    conn.close()

    expected = {
        "listing_stats",
        "orders",
        "shop_stats",
        "agent_runs",
        "seo_changes",
        "pricing_recommendations",
        "ad_spend",
        "social_posts",
        "pending_approvals",
    }
    assert expected.issubset(tables)


# ---------------------------------------------------------------------------
# Listing stats
# ---------------------------------------------------------------------------

def test_insert_listing_stat(tmp_db):
    """insert_listing_stat() should return a valid row id."""
    row_id = insert_listing_stat(
        listing_id="L001",
        title="Test Print",
        price=25.00,
        views=100,
        favorites=10,
        quantity=999,
        state="active",
    )
    assert isinstance(row_id, int)
    assert row_id > 0


def test_get_latest_listing_stats(tmp_db):
    """get_latest_listing_stats() should return only the newest snapshot per listing."""
    # Insert two snapshots for the same listing — different snapshot_at values
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO listing_stats (listing_id, title, price, views, favorites, quantity, state, snapshot_at)
        VALUES ('L001', 'Old Print', 20.0, 50, 5, 999, 'active', '2026-01-01T00:00:00Z')
        """
    )
    conn.execute(
        """
        INSERT INTO listing_stats (listing_id, title, price, views, favorites, quantity, state, snapshot_at)
        VALUES ('L001', 'New Print', 25.0, 100, 10, 999, 'active', '2026-01-02T00:00:00Z')
        """
    )
    conn.commit()
    conn.close()

    rows = get_latest_listing_stats()
    assert len(rows) == 1
    assert rows[0]["title"] == "New Print"
    assert rows[0]["views"] == 100


def test_get_latest_listing_stats_multiple_listings(tmp_db):
    """get_latest_listing_stats() returns one row per distinct listing_id."""
    for lid, title, ts in [
        ("L001", "Print A", "2026-01-01T00:00:00Z"),
        ("L002", "Print B", "2026-01-01T00:00:00Z"),
        ("L001", "Print A v2", "2026-01-02T00:00:00Z"),
    ]:
        conn = get_conn()
        conn.execute(
            """INSERT INTO listing_stats (listing_id, title, price, views, favorites, quantity, state, snapshot_at)
               VALUES (?, ?, 25.0, 10, 1, 999, 'active', ?)""",
            (lid, title, ts),
        )
        conn.commit()
        conn.close()

    rows = get_latest_listing_stats()
    assert len(rows) == 2
    titles = {r["title"] for r in rows}
    assert "Print A v2" in titles
    assert "Print B" in titles


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def test_insert_order_upsert(tmp_db):
    """Inserting the same receipt_id twice should result in only 1 row."""
    for _ in range(2):
        insert_order(
            receipt_id="R001",
            listing_id="L001",
            amount_paid=25.00,
            currency="USD",
            status="paid",
            created_at="2026-01-01T00:00:00Z",
        )

    conn = sqlite3.connect(tmp_db)
    count = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE receipt_id='R001'"
    ).fetchone()[0]
    conn.close()
    assert count == 1


def test_insert_order_returns_row_id(tmp_db):
    row_id = insert_order(
        receipt_id="R002",
        listing_id="L001",
        amount_paid=30.00,
        currency="USD",
        status="paid",
        created_at="2026-01-01T00:00:00Z",
    )
    assert isinstance(row_id, int)
    assert row_id > 0


# ---------------------------------------------------------------------------
# Shop stats
# ---------------------------------------------------------------------------

def test_insert_shop_stat_upsert(tmp_db):
    """Inserting the same stat_date twice should result in only 1 row (UNIQUE constraint)."""
    for visits in [100, 200]:
        insert_shop_stat(
            stat_date="2026-01-01",
            visits=visits,
            views=500,
            orders=10,
            revenue=250.0,
        )

    conn = sqlite3.connect(tmp_db)
    count = conn.execute(
        "SELECT COUNT(*) FROM shop_stats WHERE stat_date='2026-01-01'"
    ).fetchone()[0]
    visits = conn.execute(
        "SELECT visits FROM shop_stats WHERE stat_date='2026-01-01'"
    ).fetchone()[0]
    conn.close()

    assert count == 1
    assert visits == 200  # Second upsert should win


# ---------------------------------------------------------------------------
# Agent run tracking
# ---------------------------------------------------------------------------

def test_record_agent_run_success(tmp_db):
    """record_agent_run context manager should create a row with status='success'."""
    with record_agent_run("test_agent") as run_id:
        assert isinstance(run_id, int)

    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT status, finished_at FROM agent_runs WHERE id=?", (run_id,)
    ).fetchone()
    conn.close()

    assert row[0] == "success"
    assert row[1] is not None


def test_record_agent_run_error(tmp_db):
    """record_agent_run should mark status='error' and populate error_detail on exception."""
    run_id = None
    with pytest.raises(ValueError):
        with record_agent_run("failing_agent") as run_id:
            raise ValueError("intentional test error")

    assert run_id is not None
    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT status, error_detail FROM agent_runs WHERE id=?", (run_id,)
    ).fetchone()
    conn.close()

    assert row[0] == "error"
    assert "intentional test error" in row[1]


# ---------------------------------------------------------------------------
# SEO changes
# ---------------------------------------------------------------------------

def test_insert_seo_change(tmp_db):
    """insert_seo_change() should insert and allow retrieval of the row."""
    row_id = insert_seo_change(
        listing_id="L001",
        field="title",
        old_value="Old Title",
        new_value="New SEO Title",
        approved=1,
    )
    assert isinstance(row_id, int)

    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT listing_id, field, old_value, new_value, approved FROM seo_changes WHERE id=?",
        (row_id,),
    ).fetchone()
    conn.close()

    assert row[0] == "L001"
    assert row[1] == "title"
    assert row[2] == "Old Title"
    assert row[3] == "New SEO Title"
    assert row[4] == 1


# ---------------------------------------------------------------------------
# Pricing recommendations
# ---------------------------------------------------------------------------

def test_pricing_recommendation_lifecycle(tmp_db):
    """Full lifecycle: insert -> get_pending -> update_status -> update_applied."""
    # Insert
    rec_id = insert_pricing_recommendation(
        listing_id="L001",
        current_price=25.00,
        recommended_price=27.00,
        rationale="Better margin",
    )
    assert isinstance(rec_id, int)

    # get_pending should include this rec
    pending = get_pending_pricing_recommendations()
    ids = [row["id"] for row in pending]
    assert rec_id in ids

    # update_status to 'approved'
    update_pricing_recommendation_status(rec_id, "approved")
    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT status FROM pricing_recommendations WHERE id=?", (rec_id,)
    ).fetchone()
    conn.close()
    assert row[0] == "approved"

    # update_applied
    update_pricing_recommendation_applied(rec_id)
    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT status FROM pricing_recommendations WHERE id=?", (rec_id,)
    ).fetchone()
    conn.close()
    assert row[0] == "applied"

    # Should no longer be in pending list
    pending_after = get_pending_pricing_recommendations()
    ids_after = [row["id"] for row in pending_after]
    assert rec_id not in ids_after


# ---------------------------------------------------------------------------
# Ad spend
# ---------------------------------------------------------------------------

def test_insert_ad_spend(tmp_db):
    """insert_ad_spend() should store and allow retrieval of the row."""
    row_id = insert_ad_spend(
        listing_id="L001",
        date="2026-01-01",
        spend=12.50,
        clicks=45,
        impressions=1000,
        sales=2,
        roas=1.8,
    )
    assert isinstance(row_id, int)

    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT listing_id, spend, clicks, roas FROM ad_spend WHERE id=?", (row_id,)
    ).fetchone()
    conn.close()

    assert row[0] == "L001"
    assert abs(row[1] - 12.50) < 0.01
    assert row[2] == 45
    assert abs(row[3] - 1.8) < 0.01


# ---------------------------------------------------------------------------
# Social posts
# ---------------------------------------------------------------------------

def test_insert_social_post(tmp_db):
    """insert_social_post() should store and allow retrieval of the row."""
    row_id = insert_social_post(
        listing_id="L001",
        platform="pinterest",
        post_id="PIN123",
        caption="Beautiful sunrise print!",
        utm_params="utm_source=pinterest",
    )
    assert isinstance(row_id, int)

    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT listing_id, platform, post_id, status FROM social_posts WHERE id=?",
        (row_id,),
    ).fetchone()
    conn.close()

    assert row[0] == "L001"
    assert row[1] == "pinterest"
    assert row[2] == "PIN123"
    assert row[3] == "published"


# ---------------------------------------------------------------------------
# Pending approvals
# ---------------------------------------------------------------------------

def test_create_and_resolve_approval(tmp_db):
    """create_pending_approval -> get_pending_approvals -> resolve_approval."""
    payload = {"listing_id": "L001", "new_price": 30.0}
    approval_id = create_pending_approval("finance", "price_change", payload)
    assert isinstance(approval_id, int)

    # Should appear in pending list
    pending = get_pending_approvals()
    ids = [row["id"] for row in pending]
    assert approval_id in ids

    # Resolve as approved
    resolve_approval(approval_id, "approved")

    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT status, resolved_at FROM pending_approvals WHERE id=?",
        (approval_id,),
    ).fetchone()
    conn.close()

    assert row[0] == "approved"
    assert row[1] is not None

    # Should no longer appear in pending list
    pending_after = get_pending_approvals()
    ids_after = [row["id"] for row in pending_after]
    assert approval_id not in ids_after


# ---------------------------------------------------------------------------
# Agent run summary
# ---------------------------------------------------------------------------

def test_get_agent_run_summary(tmp_db):
    """insert 3 runs (2 success, 1 error), verify summary dict."""
    conn = get_conn()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    conn.executemany(
        "INSERT INTO agent_runs (agent_name, started_at, status) VALUES (?, ?, ?)",
        [
            ("analytics", now_str, "success"),
            ("analytics", now_str, "success"),
            ("analytics", now_str, "error"),
        ],
    )
    conn.commit()
    conn.close()

    summary = get_agent_run_summary(hours=24)
    assert "analytics" in summary
    assert summary["analytics"]["runs"] == 3
    assert summary["analytics"]["errors"] == 1
    assert summary["analytics"]["last_run"] is not None


def test_get_agent_run_summary_empty(tmp_db):
    """No runs recorded → empty dict."""
    summary = get_agent_run_summary(hours=24)
    assert isinstance(summary, dict)
