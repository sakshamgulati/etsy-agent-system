"""
InstagramClient — wrapper for the Meta Graph API (Instagram Basic Display / Content Publishing).

Uses a two-step flow: create a media container, then publish it.
Requires a long-lived User Access Token with instagram_content_publish permission.

Note: Meta Graph API requires images to be publicly accessible URLs.
Etsy listing image URLs from get_listing_images() are public and can be passed directly.
"""

import os
import logging
from pathlib import Path

import requests
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

INSTAGRAM_BASE_URL = "https://graph.facebook.com/v19.0"

_ENV_PATH = Path(__file__).parent.parent / ".env"


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class InstagramAPIError(Exception):
    """Raised for failed Meta Graph API calls during Instagram posting."""

    def __init__(self, message: str, status_code: int = 0, body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class InstagramClient:
    """Thin HTTP client for Meta Graph API Instagram Content Publishing."""

    def __init__(self):
        load_dotenv(_ENV_PATH)
        self.access_token = os.environ["INSTAGRAM_ACCESS_TOKEN"]
        self.user_id = os.environ["INSTAGRAM_USER_ID"]
        self._session = requests.Session()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_response(self, resp: requests.Response, context: str) -> dict:
        """Check response status, raise InstagramAPIError on failure."""
        if not resp.ok:
            logger.error(
                "Instagram API error during %s — status=%d body=%s",
                context,
                resp.status_code,
                resp.text,
            )
            raise InstagramAPIError(
                f"Instagram {context} failed ({resp.status_code}): {resp.text}",
                resp.status_code,
                resp.text,
            )
        data = resp.json()
        # Meta Graph API can return 200 with an 'error' key
        if "error" in data:
            err = data["error"]
            msg = err.get("message", str(err))
            logger.error("Instagram API error during %s: %s", context, msg)
            raise InstagramAPIError(
                f"Instagram {context} error: {msg}",
                err.get("code", 0),
                str(err),
            )
        return data

    def _create_container(self, image_url: str, caption: str) -> str:
        """Step 1: Create a media container.

        POST /{user_id}/media
        Returns the creation_id.
        """
        url = f"{INSTAGRAM_BASE_URL}/{self.user_id}/media"
        payload = {
            "image_url": image_url,
            "caption": caption,
            "access_token": self.access_token,
        }

        resp = self._session.post(url, data=payload, timeout=30)
        data = self._check_response(resp, "create_container")

        creation_id = data.get("id")
        if not creation_id:
            raise InstagramAPIError(
                f"Instagram create_container: no 'id' in response: {data}",
                0,
                str(data),
            )

        logger.info("Instagram media container created: creation_id=%s", creation_id)
        return creation_id

    def _publish_container(self, creation_id: str) -> str:
        """Step 2: Publish the media container.

        POST /{user_id}/media_publish
        Returns the post_id.
        """
        url = f"{INSTAGRAM_BASE_URL}/{self.user_id}/media_publish"
        payload = {
            "creation_id": creation_id,
            "access_token": self.access_token,
        }

        resp = self._session.post(url, data=payload, timeout=30)
        data = self._check_response(resp, "publish_container")

        post_id = data.get("id")
        if not post_id:
            raise InstagramAPIError(
                f"Instagram publish_container: no 'id' in response: {data}",
                0,
                str(data),
            )

        logger.info("Instagram post published: post_id=%s", post_id)
        return post_id

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def post_image(self, image_url: str, caption: str) -> str:
        """Post an image to Instagram using the two-step Meta Graph API flow.

        1. Creates a media container with the image URL and caption.
        2. Publishes the container.

        Parameters
        ----------
        image_url : publicly accessible URL of the image (Etsy URLs qualify)
        caption   : post caption text (may include hashtags)

        Returns the post_id string on success.
        Raises InstagramAPIError on failure at either step.
        """
        creation_id = self._create_container(image_url, caption)
        post_id = self._publish_container(creation_id)
        logger.info(
            "Instagram image posted successfully: post_id=%s image_url=%s",
            post_id,
            image_url,
        )
        return post_id
