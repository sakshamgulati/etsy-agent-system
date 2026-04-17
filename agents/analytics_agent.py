"""
agents/analytics_agent.py — Analytics Agent for the Etsy AI Agent system.

Responsibilities:
- Snapshot all active listings into listing_stats
- Record recent orders (upsert)
- Record shop stats
- Detect anomalies (zero-view listings, order count spikes)
- Write structured JSON logs to logs/analytics.log (30-day rotation)
"""

import os
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv

from integrations.etsy_client import EtsyClient
from db.database import (
    init_db,
    get_conn,
    record_agent_run,
    insert_listing_stat,
    insert_order,
    insert_shop_stat,
)
from agents.logger import get_logger

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"
_LOG_FILE = str(_PROJECT_ROOT / "logs" / "analytics.log")

log = get_logger("analytics", _LOG_FILE)


# ---------------------------------------------------------------------------
# Anomaly detection
# ---------------------------------------------------------------------------

def check_anomalies(db_conn: sqlite3.Connection) -> list[str]:
    """
    Scan the DB for known anomaly patterns.

    Returns a list of human-readable anomaly strings (empty list if none found).

    Checks:
    1. Any listing with 0 views in the last 48 hours.
    2. Order count for today vs. 7-day rolling average is >50% higher.
    """
    anomalies: list[str] = []

    # ------------------------------------------------------------------
    # 1. Listings with 0 views for the last 48 hours
    # ------------------------------------------------------------------
    cutoff_48h = (datetime.now(timezone.utc) - timedelta(hours=48)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    zero_view_rows = db_conn.execute(
        """
        SELECT listing_id, title, MAX(snapshot_at) AS last_seen
        FROM listing_stats
        WHERE snapshot_at >= ?
        GROUP BY listing_id
        HAVING MAX(views) = 0
        """,
        (cutoff_48h,),
    ).fetchall()

    for row in zero_view_rows:
        msg = (
            f"Listing {row['listing_id']} ({row['title']!r}) has 0 views "
            f"since {row['last_seen']}"
        )
        anomalies.append(msg)
        log.warning("anomaly_zero_views", listing_id=row["listing_id"],
                    title=row["title"], last_seen=row["last_seen"])

    # ------------------------------------------------------------------
    # 2. Order spike: today's count > 150% of 7-day average
    # ------------------------------------------------------------------
    today_start = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    today_orders_row = db_conn.execute(
        "SELECT COUNT(*) AS cnt FROM orders WHERE created_at >= ?",
        (today_start,),
    ).fetchone()
    today_count = today_orders_row["cnt"] if today_orders_row else 0

    week_orders_row = db_conn.execute(
        "SELECT COUNT(*) AS cnt FROM orders WHERE created_at >= ? AND created_at < ?",
        (seven_days_ago, today_start),
    ).fetchone()
    week_count = week_orders_row["cnt"] if week_orders_row else 0

    if week_count > 0:
        daily_avg = week_count / 7.0
        if daily_avg > 0 and today_count > daily_avg * 1.5:
            msg = (
                f"Order spike detected: {today_count} orders today vs. "
                f"{daily_avg:.1f} daily average over the past 7 days "
                f"(+{((today_count / daily_avg) - 1) * 100:.0f}%)"
            )
            anomalies.append(msg)
            log.warning(
                "anomaly_order_spike",
                today_orders=today_count,
                seven_day_avg=round(daily_avg, 2),
            )

    return anomalies


# ---------------------------------------------------------------------------
# Main analytics runner
# ---------------------------------------------------------------------------

def run_analytics() -> dict:
    """
    Execute a full analytics snapshot.

    Returns:
        {
            "listings_updated": int,
            "orders_recorded": int,
            "shop_stat_recorded": bool,
            "anomalies": list[str],
            "errors": list[str],
        }
    """
    load_dotenv(_ENV_PATH)
    init_db()

    client = EtsyClient()
    summary = {
        "listings_updated": 0,
        "orders_recorded": 0,
        "shop_stat_recorded": False,
        "anomalies": [],
        "errors": [],
    }

    db_conn = get_conn()
    try:
        with record_agent_run("analytics") as run_id:
            log.info("analytics_run_started", run_id=run_id)

            # ------------------------------------------------------------------
            # 1. Active listings snapshot
            # ------------------------------------------------------------------
            try:
                listings = client.get_listings(state="active")
                log.info("listings_fetched", count=len(listings))

                for listing in listings:
                    listing_id = str(listing.get("listing_id", ""))
                    title = listing.get("title", "")
                    # Price is nested under price.amount / price.divisor on Etsy v3
                    price_obj = listing.get("price", {})
                    if isinstance(price_obj, dict):
                        amount = price_obj.get("amount", 0)
                        divisor = price_obj.get("divisor", 100) or 100
                        price = round(amount / divisor, 2)
                    else:
                        price = float(price_obj or 0)

                    views = int(listing.get("views", 0))
                    favorites = int(listing.get("num_favorers", 0))
                    quantity = int(listing.get("quantity", 0))
                    state = listing.get("state", "active")

                    try:
                        insert_listing_stat(
                            listing_id=listing_id,
                            title=title,
                            price=price,
                            views=views,
                            favorites=favorites,
                            quantity=quantity,
                            state=state,
                        )
                        summary["listings_updated"] += 1
                        log.info(
                            "listing_stat_recorded",
                            listing_id=listing_id,
                            views=views,
                            favorites=favorites,
                            price=price,
                        )
                    except Exception as exc:
                        err = f"Failed to insert listing stat for {listing_id}: {exc}"
                        summary["errors"].append(err)
                        log.error("listing_stat_error", listing_id=listing_id, error=str(exc))

            except Exception as exc:
                err = f"Failed to fetch listings: {exc}"
                summary["errors"].append(err)
                log.error("listings_fetch_error", error=str(exc))

            # ------------------------------------------------------------------
            # 2. Recent orders (last 50)
            # ------------------------------------------------------------------
            try:
                orders = client.get_orders(limit=50, offset=0)
                log.info("orders_fetched", count=len(orders))

                for order in orders:
                    receipt_id = str(order.get("receipt_id", ""))
                    # listing_id may be on the first transaction
                    transactions = order.get("transactions", [])
                    listing_id = str(
                        transactions[0].get("listing_id", "") if transactions else ""
                    )

                    # amount_subtotal or grandtotal
                    grandtotal = order.get("grandtotal", {})
                    if isinstance(grandtotal, dict):
                        amt = grandtotal.get("amount", 0)
                        div = grandtotal.get("divisor", 100) or 100
                        amount_paid = round(amt / div, 2)
                    else:
                        amount_subtotal = order.get("subtotal", {})
                        amt = amount_subtotal.get("amount", 0) if isinstance(amount_subtotal, dict) else 0
                        div = amount_subtotal.get("divisor", 100) if isinstance(amount_subtotal, dict) else 100
                        amount_paid = round(amt / div, 2)

                    currency = order.get("currency_code", "USD")
                    status = order.get("status", "")
                    created_ts = order.get("create_timestamp", 0) or order.get("creation_tsz", 0)
                    if created_ts:
                        created_at = datetime.fromtimestamp(
                            int(created_ts), tz=timezone.utc
                        ).strftime("%Y-%m-%dT%H:%M:%SZ")
                    else:
                        created_at = ""

                    try:
                        insert_order(
                            receipt_id=receipt_id,
                            listing_id=listing_id,
                            amount_paid=amount_paid,
                            currency=currency,
                            status=status,
                            created_at=created_at,
                        )
                        summary["orders_recorded"] += 1
                        log.info(
                            "order_recorded",
                            receipt_id=receipt_id,
                            amount_paid=amount_paid,
                            currency=currency,
                            status=status,
                        )
                    except Exception as exc:
                        err = f"Failed to upsert order {receipt_id}: {exc}"
                        summary["errors"].append(err)
                        log.error("order_upsert_error", receipt_id=receipt_id, error=str(exc))

            except Exception as exc:
                err = f"Failed to fetch orders: {exc}"
                summary["errors"].append(err)
                log.error("orders_fetch_error", error=str(exc))

            # ------------------------------------------------------------------
            # 3. Shop stats
            # ------------------------------------------------------------------
            try:
                shop_stats = client.get_shop_stats(date_granularity="day")
                # Etsy returns daily stats; grab the most recent day's row
                results = shop_stats.get("results", []) if isinstance(shop_stats, dict) else []
                if results:
                    latest = results[-1]
                    stat_date = latest.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
                    visits = int(latest.get("visits", 0))
                    views = int(latest.get("pageviews", 0))
                    orders = int(latest.get("orders", 0))
                    revenue_obj = latest.get("revenue", {})
                    if isinstance(revenue_obj, dict):
                        rev_amt = revenue_obj.get("amount", 0)
                        rev_div = revenue_obj.get("divisor", 100) or 100
                        revenue = round(rev_amt / rev_div, 2)
                    else:
                        revenue = float(revenue_obj or 0)

                    insert_shop_stat(
                        stat_date=stat_date,
                        visits=visits,
                        views=views,
                        orders=orders,
                        revenue=revenue,
                    )
                    summary["shop_stat_recorded"] = True
                    log.info(
                        "shop_stat_recorded",
                        stat_date=stat_date,
                        visits=visits,
                        views=views,
                        orders=orders,
                        revenue=revenue,
                    )
                else:
                    log.info("shop_stat_no_results")

            except Exception as exc:
                err = f"Failed to fetch/store shop stats: {exc}"
                summary["errors"].append(err)
                log.error("shop_stat_error", error=str(exc))

            # ------------------------------------------------------------------
            # 4. Anomaly detection (reuses the single db_conn opened at top)
            # ------------------------------------------------------------------
            try:
                anomalies = check_anomalies(db_conn)
                summary["anomalies"] = anomalies
                log.info("anomaly_check_complete", anomaly_count=len(anomalies))

            except Exception as exc:
                err = f"Anomaly check failed: {exc}"
                summary["errors"].append(err)
                log.error("anomaly_check_error", error=str(exc))

            log.info(
                "analytics_run_complete",
                listings_updated=summary["listings_updated"],
                orders_recorded=summary["orders_recorded"],
                shop_stat_recorded=summary["shop_stat_recorded"],
                anomaly_count=len(summary["anomalies"]),
                error_count=len(summary["errors"]),
            )

    finally:
        db_conn.close()

    return summary


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    result = run_analytics()
    print(result)
