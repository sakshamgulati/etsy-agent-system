"""
agents/finance_agent.py — Finance Agent for the Etsy AI Agent system.

Responsibilities:
- Calculate revenue, COGS, profit, and margin per listing (last 30 days)
- Call Claude with pricing rules to generate recommendations
- Auto-apply small price changes; route large changes to pending_approvals
- Attempt to pull Etsy Ads data and log recommendations to ad_spend table
- Write structured JSON logs to logs/finance.log (30-day rotation)
"""

import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from integrations.etsy_client import EtsyClient, EtsyAPIError
from db.database import (
    init_db,
    get_conn,
    record_agent_run,
    insert_pricing_recommendation,
    update_pricing_recommendation_applied,
    insert_ad_spend,
    create_pending_approval,
)
from agents.logger import get_logger

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"
_LOG_FILE = str(_PROJECT_ROOT / "logs" / "finance.log")
_PROMPT_PATH = _PROJECT_ROOT / "prompts" / "finance_analyzer.md"

log = get_logger("finance", _LOG_FILE)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _thirty_days_ago() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=30)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Revenue / profit calculation
# ---------------------------------------------------------------------------

def _build_listings_data(
    db_conn: sqlite3.Connection,
    print_cost: float,
    shipping_cost: float,
) -> list[dict]:
    """
    Query the DB for the last 30 days of listing_stats + orders and compute
    per-listing financials. Returns a list of dicts ready for the Claude prompt.
    """
    cutoff = _thirty_days_ago()

    # Latest stat snapshot per listing (for price, views, title, days_active)
    stats_rows = db_conn.execute(
        """
        SELECT ls.listing_id, ls.title, ls.price, ls.views, ls.snapshot_at,
               ls.state
        FROM listing_stats ls
        INNER JOIN (
            SELECT listing_id, MAX(snapshot_at) AS max_snap
            FROM listing_stats
            GROUP BY listing_id
        ) latest ON ls.listing_id = latest.listing_id
               AND ls.snapshot_at = latest.max_snap
        WHERE ls.state = 'active'
        """
    ).fetchall()

    # First snapshot per listing (to estimate days_active)
    first_seen_rows = db_conn.execute(
        """
        SELECT listing_id, MIN(snapshot_at) AS first_snap
        FROM listing_stats
        GROUP BY listing_id
        """
    ).fetchall()
    first_seen = {row["listing_id"]: row["first_snap"] for row in first_seen_rows}

    # Order revenue per listing in last 30 days
    order_rows = db_conn.execute(
        """
        SELECT listing_id,
               COUNT(*) AS orders_count,
               SUM(amount_paid) AS total_revenue
        FROM orders
        WHERE created_at >= ?
        GROUP BY listing_id
        """,
        (cutoff,),
    ).fetchall()
    order_map: dict[str, dict] = {
        row["listing_id"]: {
            "orders_count": row["orders_count"],
            "total_revenue": row["total_revenue"] or 0.0,
        }
        for row in order_rows
    }

    listings_data = []
    for row in stats_rows:
        lid = row["listing_id"]
        orders_count = order_map.get(lid, {}).get("orders_count", 0)
        revenue = order_map.get(lid, {}).get("total_revenue", 0.0)

        cogs = orders_count * (print_cost + shipping_cost)
        profit = revenue - cogs
        margin_pct = round((profit / revenue * 100), 2) if revenue > 0 else None

        views = row["views"]
        conversion_rate_pct = (
            round(orders_count / views * 100, 2) if views > 0 else None
        )

        # Days active: from first snapshot to now
        first_snap = first_seen.get(lid, row["snapshot_at"])
        try:
            first_dt = datetime.strptime(first_snap, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
            days_active = (datetime.now(timezone.utc) - first_dt).days
        except Exception:
            days_active = 0

        listings_data.append(
            {
                "listing_id": lid,
                "title": row["title"],
                "current_price": row["price"],
                "views": views,
                "orders_count": orders_count,
                "conversion_rate_pct": conversion_rate_pct,
                "revenue": round(revenue, 2),
                "cogs": round(cogs, 2),
                "profit": round(profit, 2),
                "margin_pct": margin_pct,
                "days_active": days_active,
            }
        )

    return listings_data


# ---------------------------------------------------------------------------
# Claude — pricing recommendations
# ---------------------------------------------------------------------------

def _call_claude_pricing(listings_data: list[dict]) -> list[dict]:
    """
    Load the finance_analyzer.md prompt, inject listings_data, and call Claude.
    Returns a list of recommendation dicts.
    """
    prompt_template = _PROMPT_PATH.read_text()
    prompt = prompt_template.replace("{listings_data}", json.dumps(listings_data, indent=2))

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()

    # Strip markdown fences if Claude wraps the JSON
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    return json.loads(raw)


# ---------------------------------------------------------------------------
# Etsy Ads optimizer
# ---------------------------------------------------------------------------

def _optimize_ads(
    client: EtsyClient,
    db_conn: sqlite3.Connection,
) -> None:
    """
    Attempt to retrieve Etsy Ads / ledger data.

    If the endpoint is unavailable (404 / unsupported), log a warning and return.
    Otherwise apply rules:
    - spend > $5 with 0 sales → log recommendation to pause ads
    - positive ROAS (>2.0) → log recommendation for budget increase
    - Record all ad spend in the ad_spend table
    """
    shop_id = client.shop_id
    today = _today_str()

    try:
        # Etsy does not expose a public ads-management API; the closest
        # is the ledger / payment-account entries endpoint.
        ledger_data = client._request(
            "GET",
            f"/application/shops/{shop_id}/payment-account/ledger-entries",
            params={"limit": 100, "offset": 0},
        )
        entries = ledger_data.get("results", []) if isinstance(ledger_data, dict) else []
        log.info("ads_ledger_fetched", entry_count=len(entries))

        # Aggregate ad-related entries per listing (best-effort)
        ad_by_listing: dict[str, dict] = {}
        for entry in entries:
            entry_type = entry.get("entry_type", "")
            if "promoted" not in entry_type.lower() and "ads" not in entry_type.lower():
                continue
            ref = entry.get("reference_id", "") or ""
            amount = abs(entry.get("amount", 0)) / 100.0  # cents → dollars
            ad_by_listing.setdefault(ref, {"spend": 0.0, "sales": 0})
            ad_by_listing[ref]["spend"] += amount

        for lid, data in ad_by_listing.items():
            spend = data["spend"]
            sales = data["sales"]
            roas = 0.0  # can't compute without revenue split

            insert_ad_spend(
                listing_id=lid,
                date=today,
                spend=spend,
                clicks=0,
                impressions=0,
                sales=sales,
                roas=roas,
            )

            if spend > 5.0 and sales == 0:
                log.warning(
                    "ads_pause_recommendation",
                    listing_id=lid,
                    spend=spend,
                    sales=sales,
                    recommendation="pause_ads",
                )
            elif roas > 2.0:
                log.info(
                    "ads_budget_increase_recommendation",
                    listing_id=lid,
                    roas=roas,
                    recommendation="increase_budget",
                )

    except EtsyAPIError as exc:
        if exc.status_code in (404, 403, 501):
            log.warning(
                "ads_api_unavailable",
                status_code=exc.status_code,
                message="Ads API not available — manual review needed",
            )
        else:
            log.error("ads_api_error", status_code=exc.status_code, error=str(exc))
    except Exception as exc:
        log.warning(
            "ads_api_unavailable",
            error=str(exc),
            message="Ads API not available — manual review needed",
        )


# ---------------------------------------------------------------------------
# Main finance runner
# ---------------------------------------------------------------------------

def run_finance(dry_run: bool = False) -> dict:
    """
    Execute a full finance analysis run.

    Parameters
    ----------
    dry_run : bool
        When True, pricing changes are logged but NOT applied to Etsy.

    Returns
    -------
    dict with keys:
        listings_analyzed, recommendations_created, auto_applied,
        pending_approval, errors
    """
    load_dotenv(_ENV_PATH)

    # -- Config from env --
    print_cost = float(os.environ.get("COGS_PRINT_COST", 8.0))
    shipping_cost = float(os.environ.get("COGS_SHIPPING_COST", 5.0))
    weekly_ad_budget_cap = float(os.environ.get("WEEKLY_AD_BUDGET_CAP", 100.0))
    approval_threshold = float(
        os.environ.get("PRICE_CHANGE_APPROVAL_THRESHOLD", 0.15)
    )

    init_db()
    client = EtsyClient()

    summary = {
        "listings_analyzed": 0,
        "recommendations_created": 0,
        "auto_applied": 0,
        "pending_approval": 0,
        "errors": [],
    }

    with record_agent_run("finance") as run_id:
        log.info(
            "finance_run_started",
            run_id=run_id,
            dry_run=dry_run,
            print_cost=print_cost,
            shipping_cost=shipping_cost,
            approval_threshold=approval_threshold,
        )

        db_conn = get_conn()
        try:
            # ------------------------------------------------------------------
            # 1. Build listings data from DB
            # ------------------------------------------------------------------
            try:
                listings_data = _build_listings_data(db_conn, print_cost, shipping_cost)
                summary["listings_analyzed"] = len(listings_data)
                log.info("listings_data_built", count=len(listings_data))
            except Exception as exc:
                err = f"Failed to build listings data: {exc}"
                summary["errors"].append(err)
                log.error("listings_data_error", error=str(exc))
                listings_data = []

            # ------------------------------------------------------------------
            # 2. Pricing recommendations via Claude
            # ------------------------------------------------------------------
            if listings_data:
                try:
                    recommendations = _call_claude_pricing(listings_data)
                    log.info(
                        "claude_recommendations_received",
                        count=len(recommendations),
                    )
                except Exception as exc:
                    err = f"Claude pricing call failed: {exc}"
                    summary["errors"].append(err)
                    log.error("claude_pricing_error", error=str(exc))
                    recommendations = []

                for rec in recommendations:
                    listing_id = str(rec.get("listing_id", ""))
                    current_price = float(rec.get("current_price", 0.0))
                    recommended_price = float(rec.get("recommended_price", current_price))
                    change_pct_raw = rec.get("change_pct", 0.0)
                    # Normalise: Claude returns percent (e.g. 20.0), threshold is fraction (e.g. 0.15)
                    change_pct_fraction = abs(float(change_pct_raw)) / 100.0
                    rationale = rec.get("rationale", "")
                    priority = rec.get("priority", "low")

                    try:
                        if change_pct_fraction > approval_threshold:
                            # Large change → requires human approval
                            payload = {
                                "listing_id": listing_id,
                                "current_price": current_price,
                                "recommended_price": recommended_price,
                                "change_pct": change_pct_raw,
                                "rationale": rationale,
                                "priority": priority,
                            }
                            create_pending_approval("finance", "price_change", payload)
                            summary["pending_approval"] += 1
                            log.info(
                                "price_change_pending_approval",
                                listing_id=listing_id,
                                current_price=current_price,
                                recommended_price=recommended_price,
                                change_pct=change_pct_raw,
                                priority=priority,
                            )
                        else:
                            # Small change → auto-approved
                            rec_id = insert_pricing_recommendation(
                                listing_id=listing_id,
                                current_price=current_price,
                                recommended_price=recommended_price,
                                rationale=rationale,
                            )
                            summary["recommendations_created"] += 1
                            log.info(
                                "pricing_recommendation_created",
                                listing_id=listing_id,
                                current_price=current_price,
                                recommended_price=recommended_price,
                                change_pct=change_pct_raw,
                                priority=priority,
                            )

                            # Apply via API if change is non-zero and not dry_run
                            if recommended_price != current_price and not dry_run:
                                try:
                                    client.patch_listing(
                                        int(listing_id),
                                        price=recommended_price,
                                    )
                                    update_pricing_recommendation_applied(rec_id)
                                    summary["auto_applied"] += 1
                                    log.info(
                                        "price_applied",
                                        listing_id=listing_id,
                                        new_price=recommended_price,
                                    )
                                except Exception as patch_exc:
                                    err = (
                                        f"Failed to patch listing {listing_id}: {patch_exc}"
                                    )
                                    summary["errors"].append(err)
                                    log.error(
                                        "price_apply_error",
                                        listing_id=listing_id,
                                        error=str(patch_exc),
                                    )
                            elif dry_run and recommended_price != current_price:
                                log.info(
                                    "price_apply_skipped_dry_run",
                                    listing_id=listing_id,
                                    new_price=recommended_price,
                                )

                    except Exception as rec_exc:
                        err = f"Failed to process recommendation for {listing_id}: {rec_exc}"
                        summary["errors"].append(err)
                        log.error(
                            "recommendation_processing_error",
                            listing_id=listing_id,
                            error=str(rec_exc),
                        )

            # ------------------------------------------------------------------
            # 3. Etsy Ads optimizer
            # ------------------------------------------------------------------
            try:
                _optimize_ads(client, db_conn)
            except Exception as exc:
                err = f"Ads optimizer failed: {exc}"
                summary["errors"].append(err)
                log.error("ads_optimizer_error", error=str(exc))

        finally:
            db_conn.close()

        log.info(
            "finance_run_complete",
            listings_analyzed=summary["listings_analyzed"],
            recommendations_created=summary["recommendations_created"],
            auto_applied=summary["auto_applied"],
            pending_approval=summary["pending_approval"],
            error_count=len(summary["errors"]),
            dry_run=dry_run,
        )

    return summary


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run the Etsy Finance Agent")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log recommendations without applying price changes to Etsy",
    )
    args = parser.parse_args()

    result = run_finance(dry_run=args.dry_run)
    print(json.dumps(result, indent=2))
