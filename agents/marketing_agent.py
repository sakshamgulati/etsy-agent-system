"""
marketing_agent.py — Social media marketing automation for the Etsy AI Agent system.

Features:
- Rotates through all active listings (oldest-posted-first) to ensure equal coverage
- Generates platform-specific copy via Claude (Haiku)
- Posts to Pinterest and Instagram with UTM-tracked links
- Logs all activity to logs/marketing.log and the social_posts table
- Supports dry_run mode for testing without live API calls
- Respects a pause file: logs/marketing.paused
"""

import json
import os
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

import anthropic
from dotenv import load_dotenv

from integrations.etsy_client import EtsyClient
from integrations.pinterest_client import PinterestClient, PinterestAPIError
from integrations.instagram_client import InstagramClient, InstagramAPIError
from integrations.telegram_client import TelegramClient
from db.database import init_db, insert_social_post, record_agent_run, get_conn
from agents.logger import get_logger

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"
_LOG_FILE = str(_PROJECT_ROOT / "logs" / "marketing.log")
_PAUSE_FILE = _PROJECT_ROOT / "logs" / "marketing.paused"

log = get_logger("marketing", _LOG_FILE)

# ---------------------------------------------------------------------------
# Claude prompt
# ---------------------------------------------------------------------------

MARKETING_PROMPT = """
You are a social media content creator for a fine art print shop on Etsy.

Listing details:
- Title: {title}
- Description: {description}
- Tags: {tags}
- Price: ${price}

Generate engaging content for two platforms. Output valid JSON only:
{{
  "pinterest_title": "...",  // max 100 chars, keyword-rich, art discovery focused
  "pinterest_description": "...",  // max 500 chars, inspirational, include room decor / gift angles
  "instagram_caption": "..."  // 150-200 chars + 5-8 hashtags, storytelling tone, 1-2 emoji
}}
"""


# ---------------------------------------------------------------------------
# Rotation logic
# ---------------------------------------------------------------------------

def _pick_listing_to_feature(listings: list) -> dict:
    """Return the listing with the oldest last social post (or never posted).

    Queries social_posts for the most recent post per listing, then picks the
    listing that was posted least recently (or has never been posted).
    """
    if not listings:
        raise ValueError("No active listings available to feature.")

    listing_ids = [str(l["listing_id"]) for l in listings]

    conn: sqlite3.Connection = get_conn()
    try:
        placeholders = ",".join("?" * len(listing_ids))
        rows = conn.execute(
            f"""
            SELECT listing_id, MAX(posted_at) AS last_posted_at
            FROM social_posts
            WHERE listing_id IN ({placeholders})
            GROUP BY listing_id
            """,
            listing_ids,
        ).fetchall()
    finally:
        conn.close()

    # Build a map of listing_id -> last_posted_at (None = never posted)
    posted_map: dict[str, str | None] = {row["listing_id"]: row["last_posted_at"] for row in rows}

    # Sort listings: never-posted first, then by oldest post date ascending
    def sort_key(listing):
        lid = str(listing["listing_id"])
        last = posted_map.get(lid)
        if last is None:
            return ""  # empty string sorts before any ISO timestamp
        return last

    sorted_listings = sorted(listings, key=sort_key)
    return sorted_listings[0]


# ---------------------------------------------------------------------------
# Content generation
# ---------------------------------------------------------------------------

def _generate_content(listing: dict) -> dict:
    """Call Claude Haiku to generate Pinterest and Instagram copy.

    Returns a dict with keys: pinterest_title, pinterest_description, instagram_caption.
    """
    title = listing.get("title", "")
    description = listing.get("description", "")[:1000]  # truncate for token budget
    tags_raw = listing.get("tags", [])
    tags = ", ".join(tags_raw) if isinstance(tags_raw, list) else str(tags_raw)
    price_raw = listing.get("price", {})
    if isinstance(price_raw, dict):
        price = price_raw.get("amount", price_raw.get("currency_formatted", "?"))
        # Etsy price is sometimes stored as {amount: 1500, divisor: 100}
        divisor = price_raw.get("divisor", 1)
        if isinstance(price, (int, float)) and divisor:
            price = f"{price / divisor:.2f}"
    else:
        price = str(price_raw)

    prompt = MARKETING_PROMPT.format(
        title=title,
        description=description,
        tags=tags,
        price=price,
    )

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()

    # Strip markdown code fences if present
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])

    try:
        content = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Claude returned invalid JSON: {exc}\nRaw output:\n{raw}") from exc

    return content


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_marketing(dry_run: bool = False) -> dict:
    """Run the marketing agent: feature one listing on Pinterest and Instagram.

    Parameters
    ----------
    dry_run : bool
        If True, generate content but do NOT post to any social platform or
        write to the database. Useful for testing.

    Returns
    -------
    dict with keys:
        listing_featured  : int   — listing_id that was featured
        pinterest_posted  : bool
        instagram_posted  : bool
        errors            : list[str]
    """
    load_dotenv(_ENV_PATH)
    init_db()

    result: dict = {
        "listing_featured": None,
        "pinterest_posted": False,
        "instagram_posted": False,
        "errors": [],
    }

    etsy_client = EtsyClient()
    pinterest_client = PinterestClient()
    instagram_client = InstagramClient()
    telegram_client = TelegramClient()

    with record_agent_run("marketing") as run_id:

        # ------------------------------------------------------------------
        # Pause check
        # ------------------------------------------------------------------
        if _PAUSE_FILE.exists():
            log.info(
                "marketing_agent_paused",
                run_id=run_id,
                pause_file=str(_PAUSE_FILE),
            )
            return result

        # ------------------------------------------------------------------
        # Fetch active listings
        # ------------------------------------------------------------------
        listings = etsy_client.get_listings(state="active")
        if not listings:
            log.warning("no_active_listings", run_id=run_id)
            return result

        log.info("listings_fetched", run_id=run_id, count=len(listings))

        # ------------------------------------------------------------------
        # Pick listing to feature (rotation)
        # ------------------------------------------------------------------
        listing = _pick_listing_to_feature(listings)
        listing_id = listing["listing_id"]
        result["listing_featured"] = listing_id

        log.info(
            "listing_selected",
            run_id=run_id,
            listing_id=listing_id,
            title=listing.get("title", ""),
        )

        # ------------------------------------------------------------------
        # Get listing image
        # ------------------------------------------------------------------
        images = etsy_client.get_listing_images(listing_id)
        if not images:
            err = f"No images found for listing {listing_id}"
            log.error("no_images", run_id=run_id, listing_id=listing_id)
            result["errors"].append(err)
            return result

        image_url = images[0].get("url_fullxfull", "")
        if not image_url:
            err = f"First image for listing {listing_id} has no url_fullxfull"
            log.error("missing_image_url", run_id=run_id, listing_id=listing_id)
            result["errors"].append(err)
            return result

        # ------------------------------------------------------------------
        # Generate content via Claude
        # ------------------------------------------------------------------
        try:
            content = _generate_content(listing)
        except Exception as exc:
            err = f"Claude content generation failed: {exc}"
            log.error("content_generation_failed", run_id=run_id, error=str(exc))
            result["errors"].append(err)
            return result

        pinterest_title = content.get("pinterest_title", listing.get("title", ""))[:100]
        pinterest_description = content.get("pinterest_description", "")[:500]
        instagram_caption = content.get("instagram_caption", "")

        # ------------------------------------------------------------------
        # Build UTM-tracked listing URLs
        # ------------------------------------------------------------------
        base_url = f"https://www.etsy.com/listing/{listing_id}"

        def utm_url(platform: str) -> str:
            return (
                f"{base_url}"
                f"?utm_source={platform}"
                f"&utm_medium=social"
                f"&utm_campaign=etsy_art"
                f"&utm_content={listing_id}"
            )

        pinterest_link = utm_url("pinterest")
        instagram_link = utm_url("instagram")

        log.info(
            "content_generated",
            run_id=run_id,
            listing_id=listing_id,
            dry_run=dry_run,
        )

        if dry_run:
            log.info(
                "dry_run_complete",
                run_id=run_id,
                listing_id=listing_id,
                pinterest_title=pinterest_title,
                instagram_caption=instagram_caption[:80],
            )
            return result

        # ------------------------------------------------------------------
        # Post to Pinterest (failure does not stop Instagram)
        # ------------------------------------------------------------------
        try:
            pin_id = pinterest_client.create_pin(
                image_url=image_url,
                title=pinterest_title,
                description=pinterest_description,
                link=pinterest_link,
                alt_text=pinterest_title,
            )
            insert_social_post(
                listing_id=str(listing_id),
                platform="pinterest",
                post_id=str(pin_id),
                caption=pinterest_description,
                utm_params=pinterest_link,
            )
            result["pinterest_posted"] = True
            log.info(
                "pinterest_posted",
                run_id=run_id,
                listing_id=listing_id,
                pin_id=pin_id,
            )
        except (PinterestAPIError, Exception) as exc:
            err = f"Pinterest post failed: {exc}"
            log.error("pinterest_post_failed", run_id=run_id, listing_id=listing_id, error=str(exc))
            result["errors"].append(err)

        # ------------------------------------------------------------------
        # Post to Instagram (always attempted regardless of Pinterest outcome)
        # ------------------------------------------------------------------
        try:
            ig_post_id = instagram_client.post_image(
                image_url=image_url,
                caption=instagram_caption,
            )
            insert_social_post(
                listing_id=str(listing_id),
                platform="instagram",
                post_id=str(ig_post_id),
                caption=instagram_caption,
                utm_params=instagram_link,
            )
            result["instagram_posted"] = True
            log.info(
                "instagram_posted",
                run_id=run_id,
                listing_id=listing_id,
                post_id=ig_post_id,
            )
        except (InstagramAPIError, Exception) as exc:
            err = f"Instagram post failed: {exc}"
            log.error("instagram_post_failed", run_id=run_id, listing_id=listing_id, error=str(exc))
            result["errors"].append(err)

        # ------------------------------------------------------------------
        # Telegram summary notification
        # ------------------------------------------------------------------
        try:
            status_lines = [
                f"*Marketing Agent Run*",
                f"Listing: `{listing_id}` — {listing.get('title', '')[:60]}",
                f"Pinterest: {'posted' if result['pinterest_posted'] else 'FAILED'}",
                f"Instagram: {'posted' if result['instagram_posted'] else 'FAILED'}",
            ]
            if result["errors"]:
                status_lines.append("Errors: " + " | ".join(result["errors"]))
            telegram_client.send_digest("\n".join(status_lines))
        except Exception as exc:
            log.warning("telegram_notify_failed", error=str(exc))

        log.info(
            "marketing_run_complete",
            run_id=run_id,
            listing_id=listing_id,
            pinterest_posted=result["pinterest_posted"],
            instagram_posted=result["instagram_posted"],
            error_count=len(result["errors"]),
        )

    return result
