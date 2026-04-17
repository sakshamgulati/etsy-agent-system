"""
EtsyClient — OAuth 2.0 wrapper for Etsy API v3.

Handles token refresh, rate limiting, and all endpoints used by the agents.
"""

import os
import time
import logging
import threading
from pathlib import Path

import requests
from dotenv import load_dotenv, set_key

logger = logging.getLogger(__name__)

ETSY_BASE_URL = "https://openapi.etsy.com/v3"
ETSY_TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"

# Path to the project .env file (two levels up from this file: integrations/ → etsy/)
_ENV_PATH = Path(__file__).parent.parent / ".env"

# Fix #12: module-level lock to prevent concurrent token refresh race conditions
_token_refresh_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class EtsyAuthError(Exception):
    """Raised when OAuth token refresh fails."""


class EtsyAPIError(Exception):
    """Raised for non-2xx Etsy API responses (excluding 401 after retry)."""

    def __init__(self, message: str, status_code: int, body: str):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class EtsyClient:
    """Thin HTTP client for Etsy API v3 with auto token refresh and rate-limit handling."""

    def __init__(self):
        load_dotenv(_ENV_PATH)
        self.api_key = os.environ["ETSY_API_KEY"]
        self.api_secret = os.environ.get("ETSY_API_SECRET", "")
        self.access_token = os.environ["ETSY_ACCESS_TOKEN"]
        self._refresh_token_str = os.environ["ETSY_REFRESH_TOKEN"]
        self.shop_id = os.environ["ETSY_SHOP_ID"]

        self._session = requests.Session()

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    def _refresh_token(self) -> None:
        """Exchange the refresh token for a new access + refresh token pair.

        Updates self.access_token / self._refresh_token_str and persists them to .env.
        Raises EtsyAuthError on failure.
        """
        with _token_refresh_lock:
            payload = {
                "grant_type": "refresh_token",
                "client_id": self.api_key,
                "refresh_token": self._refresh_token_str,
            }
            resp = requests.post(ETSY_TOKEN_URL, data=payload, timeout=30)
            if not resp.ok:
                raise EtsyAuthError(
                    f"Token refresh failed ({resp.status_code}): {resp.text}"
                )

            data = resp.json()
            self.access_token = data["access_token"]
            self._refresh_token_str = data["refresh_token"]

            # Persist to .env file in-place
            env_file = str(_ENV_PATH)
            set_key(env_file, "ETSY_ACCESS_TOKEN", self.access_token)
            set_key(env_file, "ETSY_REFRESH_TOKEN", self._refresh_token_str)
            logger.info("Etsy tokens refreshed and saved to %s", env_file)

    def refresh_token(self) -> None:
        """Public method to refresh OAuth tokens. Called by software_agent for auto-healing."""
        self._refresh_token()

    # ------------------------------------------------------------------
    # Core request wrapper
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, _retry_auth: bool = True, **kwargs):
        """Make an authenticated request to the Etsy API.

        Handles:
        - Authorization + x-api-key headers
        - 401 → token refresh + single retry
        - 429 → exponential backoff, up to 3 retries
        - Other non-2xx → EtsyAPIError
        """
        url = f"{ETSY_BASE_URL}{path}"
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.access_token}"
        headers["x-api-key"] = self.api_key

        backoff = 1.0
        max_rate_retries = 3

        for rate_attempt in range(max_rate_retries + 1):
            resp = self._session.request(method, url, headers=headers, **kwargs)

            if resp.status_code == 401 and _retry_auth:
                logger.warning("Received 401 — refreshing token and retrying.")
                self._refresh_token()
                headers["Authorization"] = f"Bearer {self.access_token}"
                resp = self._session.request(method, url, headers=headers, **kwargs)
                # If still 401 after refresh, raise EtsyAuthError immediately
                if resp.status_code == 401:
                    raise EtsyAuthError("Auth failed even after token refresh. Status: 401")
                _retry_auth = False  # don't loop on auth again

            if resp.status_code == 429:
                if rate_attempt == max_rate_retries:
                    raise EtsyAPIError(
                        f"Rate limited after {max_rate_retries} retries",
                        429,
                        resp.text,
                    )
                sleep_time = backoff * (2 ** rate_attempt)
                logger.warning("Rate limited (429). Sleeping %.1fs before retry.", sleep_time)
                time.sleep(sleep_time)
                continue  # retry

            if not resp.ok:
                raise EtsyAPIError(
                    f"Etsy API error {resp.status_code}: {resp.text}",
                    resp.status_code,
                    resp.text,
                )

            # Success
            if resp.status_code == 204 or not resp.content:
                return {}
            return resp.json()

    # ------------------------------------------------------------------
    # Listings
    # ------------------------------------------------------------------

    def get_listings(self, state: str = "active") -> list:
        """Return all listings for the shop (paginates automatically).

        GET /v3/application/shops/{shop_id}/listings
        """
        path = f"/application/shops/{self.shop_id}/listings"
        limit = 100
        offset = 0
        all_listings = []

        while True:
            data = self._request(
                "GET", path, params={"state": state, "limit": limit, "offset": offset}
            )
            results = data.get("results", [])
            all_listings.extend(results)
            if len(results) < limit:
                break
            offset += limit

        return all_listings

    def get_listing(self, listing_id: int) -> dict:
        """Return a single listing.

        GET /v3/application/listings/{listing_id}
        """
        return self._request("GET", f"/application/listings/{listing_id}")

    def patch_listing(self, listing_id: int, **fields) -> dict:
        """Update one or more listing fields (title, description, tags, price).

        PATCH /v3/application/listings/{listing_id}
        """
        return self._request(
            "PATCH",
            f"/application/listings/{listing_id}",
            json=fields,
        )

    def get_listing_images(self, listing_id: int) -> list:
        """Return image dicts (each has a ``url_fullxfull`` key).

        GET /v3/application/listings/{listing_id}/images
        """
        data = self._request("GET", f"/application/listings/{listing_id}/images")
        return data.get("results", data) if isinstance(data, dict) else data

    # ------------------------------------------------------------------
    # Orders / receipts
    # ------------------------------------------------------------------

    def get_orders(self, limit: int = 25, offset: int = 0) -> list:
        """Return shop receipts (orders).

        GET /v3/application/shops/{shop_id}/receipts
        """
        data = self._request(
            "GET",
            f"/application/shops/{self.shop_id}/receipts",
            params={"limit": limit, "offset": offset},
        )
        return data.get("results", [])

    def get_receipt(self, receipt_id: int) -> dict:
        """Return a single receipt.

        GET /v3/application/shops/{shop_id}/receipts/{receipt_id}
        """
        return self._request(
            "GET", f"/application/shops/{self.shop_id}/receipts/{receipt_id}"
        )

    # ------------------------------------------------------------------
    # Shop stats
    # ------------------------------------------------------------------

    def get_shop_stats(self, date_granularity: str = "day", end_date=None) -> dict:
        """Return shop stats.

        GET /v3/application/shops/{shop_id}/stats
        """
        params = {"date_granularity": date_granularity}
        if end_date is not None:
            params["end_date"] = end_date
        return self._request(
            "GET", f"/application/shops/{self.shop_id}/stats", params=params
        )

    # ------------------------------------------------------------------
    # Inventory / ads helpers
    # ------------------------------------------------------------------

    def get_listing_inventory(self, listing_id: int) -> dict:
        """Return inventory/offering data for a listing.

        GET /v3/application/listings/{listing_id}/inventory
        """
        return self._request("GET", f"/application/listings/{listing_id}/inventory")

    def get_shop_listings_active_count(self) -> int:
        """Return the count of active listings for the shop."""
        path = f"/application/shops/{self.shop_id}/listings"
        data = self._request(
            "GET", path, params={"state": "active", "limit": 1, "offset": 0}
        )
        return data.get("count", 0)
