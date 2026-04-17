"""
agents/seo_agent.py — SEO Optimisation Agent for the Etsy AI Agent system.

Responsibilities:
- Fetch all active Etsy listings
- Send each listing's title, tags, and description to Claude for SEO analysis
- Apply the optimised fields back to Etsy via PATCH (unless dry_run=True)
- Record every field change in the seo_changes DB table
- Write structured JSON logs to logs/seo.log (30-day rotation)
"""

import json
import os
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from integrations.etsy_client import EtsyClient, EtsyAPIError
from db.database import init_db, record_agent_run, insert_seo_change
from agents.logger import get_logger

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"
_LOG_FILE = str(_PROJECT_ROOT / "logs" / "seo.log")
_PROMPT_TEMPLATE_PATH = _PROJECT_ROOT / "prompts" / "seo_optimizer.md"

log = get_logger("seo", _LOG_FILE)


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------

def _load_prompt_template() -> str:
    """Read the SEO optimiser prompt template from disk."""
    return _PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")


def _format_prompt(template: str, listing: dict) -> str:
    """
    Fill the prompt template with this listing's current values.

    Parameters
    ----------
    template : the raw markdown prompt template string
    listing  : a listing dict from EtsyClient.get_listings()
    """
    listing_id = str(listing.get("listing_id", ""))
    title = listing.get("title", "") or ""
    description = listing.get("description", "") or ""

    # Tags come as a list of strings from the Etsy API
    raw_tags = listing.get("tags", []) or []
    tags_formatted = json.dumps(raw_tags, ensure_ascii=False)

    return template.format(
        listing_id=listing_id,
        title=title,
        tags=tags_formatted,
        description=description,
    )


# ---------------------------------------------------------------------------
# Claude API call
# ---------------------------------------------------------------------------

def _call_claude(prompt: str, api_key: str) -> dict:
    """
    Send the prompt to Claude and return the parsed JSON response.

    Raises:
        ValueError  — if the response body is not valid JSON
        anthropic.* — propagated on API-level errors
    """
    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    raw_text = message.content[0].text.strip()

    # Strip markdown code fences if the model wrapped the JSON anyway
    if raw_text.startswith("```"):
        lines = raw_text.splitlines()
        # Remove opening fence (```json or ```) and closing fence (```)
        inner_lines = []
        in_fence = False
        for line in lines:
            if line.startswith("```") and not in_fence:
                in_fence = True
                continue
            if line.startswith("```") and in_fence:
                in_fence = False
                continue
            if in_fence:
                inner_lines.append(line)
        raw_text = "\n".join(inner_lines).strip()

    return json.loads(raw_text)


# ---------------------------------------------------------------------------
# Core logic per listing
# ---------------------------------------------------------------------------

def _process_listing(
    listing: dict,
    template: str,
    etsy: EtsyClient,
    api_key: str,
    dry_run: bool,
) -> dict:
    """
    Run SEO optimisation for a single listing.

    Returns a result dict:
        {
            "listing_id": str,
            "status": "ok" | "error",
            "changes": int,          # number of fields changed
            "error": str | None,
        }
    """
    listing_id = str(listing.get("listing_id", "unknown"))

    log.info(
        "seo_listing_start",
        listing_id=listing_id,
        title=listing.get("title", "")[:60],
        dry_run=dry_run,
    )

    # 1. Format prompt and call Claude
    try:
        prompt = _format_prompt(template, listing)
        optimised = _call_claude(prompt, api_key)
    except json.JSONDecodeError as exc:
        log.error(
            "seo_invalid_json",
            listing_id=listing_id,
            error=str(exc),
        )
        return {"listing_id": listing_id, "status": "error", "changes": 0, "error": f"Invalid JSON from Claude: {exc}"}
    except Exception as exc:
        log.error(
            "seo_claude_error",
            listing_id=listing_id,
            error=str(exc),
        )
        return {"listing_id": listing_id, "status": "error", "changes": 0, "error": f"Claude API error: {exc}"}

    # 2. Validate response structure
    required_keys = {"title", "tags", "description", "rationale"}
    missing = required_keys - optimised.keys()
    if missing:
        msg = f"Claude response missing keys: {missing}"
        log.error("seo_missing_keys", listing_id=listing_id, missing=list(missing))
        return {"listing_id": listing_id, "status": "error", "changes": 0, "error": msg}

    if not isinstance(optimised["tags"], list) or len(optimised["tags"]) != 13:
        msg = f"Claude returned {len(optimised.get('tags', []))} tags — expected 13"
        log.error("seo_wrong_tag_count", listing_id=listing_id, count=len(optimised.get("tags", [])))
        return {"listing_id": listing_id, "status": "error", "changes": 0, "error": msg}

    # 3. Build change set — only include fields that actually differ
    old_title = listing.get("title", "") or ""
    old_tags = listing.get("tags", []) or []
    old_description = listing.get("description", "") or ""

    new_title = optimised["title"]
    new_tags = optimised["tags"]
    new_description = optimised["description"]

    patch_fields: dict = {}
    field_changes: list[tuple] = []  # (field, old_value, new_value)

    if new_title != old_title:
        patch_fields["title"] = new_title
        field_changes.append(("title", old_title, new_title))

    if new_tags != old_tags:
        patch_fields["tags"] = new_tags
        field_changes.append(("tags", json.dumps(old_tags), json.dumps(new_tags)))

    if new_description != old_description:
        patch_fields["description"] = new_description
        field_changes.append(("description", old_description[:500], new_description[:500]))

    if not patch_fields:
        log.info("seo_no_changes", listing_id=listing_id)
        return {"listing_id": listing_id, "status": "ok", "changes": 0, "error": None}

    # 4. Apply changes to Etsy (unless dry_run)
    if not dry_run:
        try:
            etsy.patch_listing(int(listing_id), **patch_fields)
            log.info(
                "seo_patch_applied",
                listing_id=listing_id,
                fields=list(patch_fields.keys()),
            )
        except EtsyAPIError as exc:
            log.error(
                "seo_patch_failed",
                listing_id=listing_id,
                status_code=exc.status_code,
                error=str(exc),
            )
            return {"listing_id": listing_id, "status": "error", "changes": 0, "error": f"Etsy PATCH failed ({exc.status_code}): {exc}"}
        except Exception as exc:
            log.error("seo_patch_error", listing_id=listing_id, error=str(exc))
            return {"listing_id": listing_id, "status": "error", "changes": 0, "error": f"Etsy PATCH error: {exc}"}

    # 5. Record each change in the DB
    for field, old_val, new_val in field_changes:
        approved = 0 if dry_run else 1
        insert_seo_change(
            listing_id=listing_id,
            field=field,
            old_value=old_val,
            new_value=new_val,
            approved=approved,
        )

    log.info(
        "seo_listing_done",
        listing_id=listing_id,
        fields_changed=list(patch_fields.keys()),
        rationale=optimised.get("rationale", "")[:200],
        dry_run=dry_run,
    )

    return {"listing_id": listing_id, "status": "ok", "changes": len(field_changes), "error": None}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_seo(dry_run: bool = False) -> dict:
    """
    Run the SEO optimisation agent over all active listings.

    Parameters
    ----------
    dry_run : if True, Claude is called and changes are logged to the DB
              (approved=0) but no PATCH request is sent to Etsy.

    Returns
    -------
    {
        "listings_processed": int,
        "changes_applied": int,
        "errors": list[str],
    }
    """
    # 1. Load environment variables
    load_dotenv(_ENV_PATH)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise EnvironmentError(
            "ANTHROPIC_API_KEY is not set. Add it to your .env file before running the SEO agent."
        )

    # 2. Initialise Etsy client and database
    etsy = EtsyClient()
    init_db()

    # 3. Load prompt template once (shared across all listings)
    template = _load_prompt_template()

    listings_processed = 0
    changes_applied = 0
    errors: list[str] = []

    # 4. Wrap entire run in an agent_run record
    with record_agent_run("seo") as run_id:
        log.info("seo_run_start", run_id=run_id, dry_run=dry_run)

        # 5. Fetch all active listings
        try:
            listings = etsy.get_listings(state="active")
        except Exception as exc:
            log.error("seo_fetch_failed", error=str(exc))
            raise  # Let record_agent_run mark this as error

        log.info("seo_listings_fetched", count=len(listings))

        # 6. Process each listing
        for listing in listings:
            result = _process_listing(
                listing=listing,
                template=template,
                etsy=etsy,
                api_key=api_key,
                dry_run=dry_run,
            )

            listings_processed += 1

            if result["status"] == "ok":
                changes_applied += result["changes"]
            else:
                errors.append(f"listing_id={result['listing_id']}: {result['error']}")

        summary = (
            f"Processed {listings_processed} listings, "
            f"applied {changes_applied} field changes, "
            f"{len(errors)} errors."
        )
        log.info(
            "seo_run_complete",
            run_id=run_id,
            listings_processed=listings_processed,
            changes_applied=changes_applied,
            error_count=len(errors),
            dry_run=dry_run,
        )

    return {
        "listings_processed": listings_processed,
        "changes_applied": changes_applied,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# CLI convenience
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run the Etsy SEO optimisation agent.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Analyse listings and log changes but do NOT update Etsy.",
    )
    args = parser.parse_args()

    result = run_seo(dry_run=args.dry_run)
    print(json.dumps(result, indent=2))
