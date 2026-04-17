"""
PinterestClient — wrapper for the Pinterest API v5.

Handles pin creation and board listing for the Etsy AI Agent marketing workflow.
"""

import os
import time
import logging
from pathlib import Path

import requests
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

PINTEREST_BASE_URL = "https://api.pinterest.com/v5"

_ENV_PATH = Path(__file__).parent.parent / ".env"


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class PinterestAPIError(Exception):
    """Raised for non-2xx Pinterest API responses (excluding 401 and handled 429)."""

    def __init__(self, message: str, status_code: int, body: str):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class PinterestClient:
    """Thin HTTP client for Pinterest API v5 with rate-limit backoff."""

    def __init__(self):
        load_dotenv(_ENV_PATH)
        self.access_token = os.environ["PINTEREST_ACCESS_TOKEN"]
        self.board_id = os.environ["PINTEREST_BOARD_ID"]
        self._session = requests.Session()

    # ------------------------------------------------------------------
    # Core request wrapper
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> dict:
        """Make an authenticated request to the Pinterest API.

        Handles:
        - Authorization header
        - 401 → log error and raise PinterestAPIError
        - 429 → exponential backoff, up to 3 retries
        - Other non-2xx → raise PinterestAPIError
        """
        url = f"{PINTEREST_BASE_URL}{path}"
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.access_token}"
        headers["Content-Type"] = "application/json"

        backoff = 1.0
        max_rate_retries = 3

        for rate_attempt in range(max_rate_retries + 1):
            resp = self._session.request(method, url, headers=headers, **kwargs)

            if resp.status_code == 401:
                logger.error(
                    "Pinterest API returned 401 Unauthorized. Check PINTEREST_ACCESS_TOKEN. "
                    "Body: %s",
                    resp.text,
                )
                raise PinterestAPIError(
                    f"Pinterest authentication failed (401): {resp.text}",
                    401,
                    resp.text,
                )

            if resp.status_code == 429:
                if rate_attempt == max_rate_retries:
                    raise PinterestAPIError(
                        f"Pinterest rate limited after {max_rate_retries} retries",
                        429,
                        resp.text,
                    )
                sleep_time = backoff * (2 ** rate_attempt)
                logger.warning(
                    "Pinterest rate limited (429). Sleeping %.1fs before retry %d.",
                    sleep_time,
                    rate_attempt + 1,
                )
                time.sleep(sleep_time)
                continue  # retry

            if not resp.ok:
                raise PinterestAPIError(
                    f"Pinterest API error {resp.status_code}: {resp.text}",
                    resp.status_code,
                    resp.text,
                )

            # Success
            if resp.status_code == 204 or not resp.content:
                return {}
            return resp.json()

    # ------------------------------------------------------------------
    # Pins
    # ------------------------------------------------------------------

    def create_pin(
        self,
        image_url: str,
        title: str,
        description: str,
        link: str,
        alt_text: str = None,
    ) -> str:
        """Create a new pin on the configured board.

        POST /pins

        Returns the pin_id string on success.
        Raises PinterestAPIError on failure.
        """
        payload = {
            "board_id": self.board_id,
            "media_source": {
                "source_type": "image_url",
                "url": image_url,
            },
            "title": title,
            "description": description,
            "link": link,
            "alt_text": alt_text or title,
        }

        data = self._request("POST", "/pins", json=payload)
        pin_id = data.get("id")
        if not pin_id:
            raise PinterestAPIError(
                f"Pinterest create_pin response missing 'id'. Full response: {data}",
                0,
                str(data),
            )

        logger.info("Pinterest pin created: pin_id=%s board_id=%s", pin_id, self.board_id)
        return pin_id

    # ------------------------------------------------------------------
    # Boards
    # ------------------------------------------------------------------

    def get_boards(self) -> list:
        """Return a list of the authenticated user's boards.

        GET /boards

        Useful for setup validation to confirm the access token is working
        and to discover available board IDs.
        """
        data = self._request("GET", "/boards")
        boards = data.get("items", [])
        logger.info("Pinterest get_boards: retrieved %d boards.", len(boards))
        return boards
