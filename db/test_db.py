"""
test_db.py — Smoke test for the Etsy Agent database module.

Inserts one row into every table, reads it back, and prints "DB OK" if all pass.
"""

import os
import sys
import time

# Use a temp DB so we don't pollute the real one during tests.
TEST_DB = f"/tmp/etsy_test_{int(time.time())}.db"
os.environ["ETSY_DB_PATH"] = TEST_DB

# Import after setting the env var so database.py picks up the path.
import database as db  # noqa: E402


def main():
    errors = []

    # ------------------------------------------------------------------ #
    # 1. init_db — must be idempotent (call twice)
    # ------------------------------------------------------------------ #
    try:
        db.init_db()
        db.init_db()  # second call must not raise
    except Exception as e:
        errors.append(f"init_db: {e}")

    # ------------------------------------------------------------------ #
    # 2. listing_stats
    # ------------------------------------------------------------------ #
    try:
        rid = db.insert_listing_stat(
            listing_id="LST001",
            title="Test Print",
            price=29.99,
            views=100,
            favorites=10,
            quantity=5,
            state="active",
        )
        assert rid is not None and rid > 0, "insert_listing_stat returned bad id"

        rows = db.get_latest_listing_stats()
        assert len(rows) == 1, f"expected 1 listing row, got {len(rows)}"
        assert rows[0]["listing_id"] == "LST001"
        assert rows[0]["price"] == 29.99
    except Exception as e:
        errors.append(f"listing_stats: {e}")

    # ------------------------------------------------------------------ #
    # 3. orders
    # ------------------------------------------------------------------ #
    try:
        rid = db.insert_order(
            receipt_id="REC001",
            listing_id="LST001",
            amount_paid=29.99,
            currency="USD",
            status="completed",
            created_at="2026-04-16T00:00:00Z",
        )
        assert rid is not None and rid > 0

        # Upsert same receipt_id — should not raise
        rid2 = db.insert_order(
            receipt_id="REC001",
            listing_id="LST001",
            amount_paid=29.99,
            currency="USD",
            status="completed",
            created_at="2026-04-16T00:00:00Z",
        )
        assert rid2 is not None

        with db.get_conn() as conn:
            rows = conn.execute("SELECT * FROM orders WHERE receipt_id = 'REC001'").fetchall()
        assert len(rows) == 1, f"upsert failed: expected 1 row, got {len(rows)}"
    except Exception as e:
        errors.append(f"orders: {e}")

    # ------------------------------------------------------------------ #
    # 4. shop_stats
    # ------------------------------------------------------------------ #
    try:
        rid = db.insert_shop_stat(
            stat_date="2026-04-16",
            visits=500,
            views=1200,
            orders=3,
            revenue=89.97,
        )
        assert rid is not None and rid > 0

        with db.get_conn() as conn:
            row = conn.execute("SELECT * FROM shop_stats WHERE id = ?", (rid,)).fetchone()
        assert row["revenue"] == 89.97
    except Exception as e:
        errors.append(f"shop_stats: {e}")

    # ------------------------------------------------------------------ #
    # 5. agent_runs (context manager)
    # ------------------------------------------------------------------ #
    try:
        with db.record_agent_run("test_agent") as run_id:
            assert run_id is not None and run_id > 0

        with db.get_conn() as conn:
            row = conn.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
        assert row["status"] == "success"
        assert row["finished_at"] is not None
    except Exception as e:
        errors.append(f"agent_runs (success): {e}")

    # agent_runs error path
    try:
        try:
            with db.record_agent_run("test_agent_err") as run_id_err:
                raise ValueError("intentional test error")
        except ValueError:
            pass  # expected

        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM agent_runs WHERE id = ?", (run_id_err,)
            ).fetchone()
        assert row["status"] == "error"
        assert "intentional" in row["error_detail"]
    except Exception as e:
        errors.append(f"agent_runs (error path): {e}")

    # ------------------------------------------------------------------ #
    # 6. seo_changes
    # ------------------------------------------------------------------ #
    try:
        rid = db.insert_seo_change(
            listing_id="LST001",
            field="title",
            old_value="Old Title",
            new_value="New Title",
        )
        assert rid is not None and rid > 0

        with db.get_conn() as conn:
            row = conn.execute("SELECT * FROM seo_changes WHERE id = ?", (rid,)).fetchone()
        assert row["field"] == "title"
        assert row["approved"] == 1
    except Exception as e:
        errors.append(f"seo_changes: {e}")

    # ------------------------------------------------------------------ #
    # 7. pricing_recommendations
    # ------------------------------------------------------------------ #
    try:
        rid = db.insert_pricing_recommendation(
            listing_id="LST001",
            current_price=29.99,
            recommended_price=34.99,
            rationale="Competitor analysis shows higher price point viable.",
        )
        assert rid is not None and rid > 0

        pending = db.get_pending_pricing_recommendations()
        assert len(pending) == 1
        assert pending[0]["id"] == rid

        db.update_pricing_recommendation_status(rid, "approved")
        pending_after = db.get_pending_pricing_recommendations()
        assert len(pending_after) == 0
    except Exception as e:
        errors.append(f"pricing_recommendations: {e}")

    # ------------------------------------------------------------------ #
    # 8. ad_spend
    # ------------------------------------------------------------------ #
    try:
        rid = db.insert_ad_spend(
            listing_id="LST001",
            date="2026-04-16",
            spend=5.00,
            clicks=120,
            impressions=3000,
            sales=2,
            roas=11.99,
        )
        assert rid is not None and rid > 0

        with db.get_conn() as conn:
            row = conn.execute("SELECT * FROM ad_spend WHERE id = ?", (rid,)).fetchone()
        assert row["clicks"] == 120
    except Exception as e:
        errors.append(f"ad_spend: {e}")

    # ------------------------------------------------------------------ #
    # 9. social_posts
    # ------------------------------------------------------------------ #
    try:
        rid = db.insert_social_post(
            listing_id="LST001",
            platform="pinterest",
            post_id="PIN123",
            caption="Check out this amazing art print!",
            utm_params="utm_source=pinterest&utm_medium=social",
        )
        assert rid is not None and rid > 0

        with db.get_conn() as conn:
            row = conn.execute("SELECT * FROM social_posts WHERE id = ?", (rid,)).fetchone()
        assert row["platform"] == "pinterest"
        assert row["status"] == "published"
    except Exception as e:
        errors.append(f"social_posts: {e}")

    # ------------------------------------------------------------------ #
    # 10. pending_approvals
    # ------------------------------------------------------------------ #
    try:
        rid = db.create_pending_approval(
            agent_name="pricing_agent",
            action_type="price_change",
            payload_dict={"listing_id": "LST001", "new_price": 34.99},
        )
        assert rid is not None and rid > 0

        pending = db.get_pending_approvals()
        assert len(pending) == 1
        assert pending[0]["action_type"] == "price_change"

        import json
        payload = json.loads(pending[0]["payload"])
        assert payload["new_price"] == 34.99

        db.resolve_approval(rid, "approved")
        pending_after = db.get_pending_approvals()
        assert len(pending_after) == 0
    except Exception as e:
        errors.append(f"pending_approvals: {e}")

    # ------------------------------------------------------------------ #
    # 11. get_agent_run_summary
    # ------------------------------------------------------------------ #
    try:
        summary = db.get_agent_run_summary(hours=24)
        assert "test_agent" in summary
        assert summary["test_agent"]["runs"] >= 1
        # test_agent_err had one error run
        assert summary["test_agent_err"]["errors"] >= 1
    except Exception as e:
        errors.append(f"get_agent_run_summary: {e}")

    # ------------------------------------------------------------------ #
    # Result
    # ------------------------------------------------------------------ #
    if errors:
        print("FAILURES:")
        for err in errors:
            print(f"  - {err}")
        sys.exit(1)
    else:
        print("DB OK")

    # Clean up temp DB
    try:
        os.remove(TEST_DB)
    except OSError:
        pass


if __name__ == "__main__":
    main()
