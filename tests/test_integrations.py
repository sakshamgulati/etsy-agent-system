"""
test_integrations.py — Tests for integrations: Telegram, Pinterest, Instagram.
"""

import json
import pytest
from unittest.mock import patch, MagicMock

from integrations.telegram_client import TelegramClient, create_webhook_app
from integrations.pinterest_client import PinterestClient, PinterestAPIError
from integrations.instagram_client import InstagramClient, InstagramAPIError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_telegram(mock_env):
    with patch("integrations.telegram_client.load_dotenv"):
        return TelegramClient()


def _make_pinterest(mock_env):
    with patch("integrations.pinterest_client.load_dotenv"):
        return PinterestClient()


def _make_instagram(mock_env):
    with patch("integrations.instagram_client.load_dotenv"):
        return InstagramClient()


# ===========================================================================
# Telegram Client
# ===========================================================================

class TestTelegramClient:

    def test_send_message(self, mock_env, requests_mock):
        """Mock requests.post, verify correct URL + payload."""
        client = _make_telegram(mock_env)
        expected_url = f"https://api.telegram.org/bot123:TEST/sendMessage"
        requests_mock.post(expected_url, json={"ok": True, "result": {}})

        result = client.send_message("Hello World")

        assert requests_mock.called
        req_body = requests_mock.last_request.json()
        assert req_body["chat_id"] == "999"
        assert req_body["text"] == "Hello World"
        assert req_body["parse_mode"] == "Markdown"

    def test_send_alert_adds_prefix(self, mock_env, requests_mock):
        """send_alert should prepend the alert emoji."""
        client = _make_telegram(mock_env)
        expected_url = f"https://api.telegram.org/bot123:TEST/sendMessage"
        requests_mock.post(expected_url, json={"ok": True, "result": {}})

        client.send_alert("Something broke")

        req_body = requests_mock.last_request.json()
        assert req_body["text"].startswith("🚨")
        assert "Something broke" in req_body["text"]

    def test_send_digest_adds_prefix(self, mock_env, requests_mock):
        """send_digest should prepend the chart emoji."""
        client = _make_telegram(mock_env)
        expected_url = f"https://api.telegram.org/bot123:TEST/sendMessage"
        requests_mock.post(expected_url, json={"ok": True, "result": {}})

        client.send_digest("Daily report here")

        req_body = requests_mock.last_request.json()
        assert req_body["text"].startswith("📊")

    def test_reply_to_command(self, mock_env, requests_mock):
        """reply_to_command should include reply_to_message_id."""
        client = _make_telegram(mock_env)
        expected_url = f"https://api.telegram.org/bot123:TEST/sendMessage"
        requests_mock.post(expected_url, json={"ok": True, "result": {}})

        client.reply_to_command(chat_id=999, message_id=42, text="Reply text")

        req_body = requests_mock.last_request.json()
        assert req_body["chat_id"] == 999
        assert req_body["reply_to_message_id"] == 42
        assert req_body["text"] == "Reply text"

    def test_telegram_webhook_routes_command(self, mock_env):
        """Simulate POST to /webhook, verify handler called."""
        handler_mock = MagicMock()
        app = create_webhook_app({"status": handler_mock})

        with app.test_client() as client:
            payload = {
                "message": {
                    "chat": {"id": 999},
                    "message_id": 1,
                    "text": "/status",
                }
            }
            response = client.post(
                "/webhook",
                data=json.dumps(payload),
                content_type="application/json",
            )

        assert response.status_code == 200
        handler_mock.assert_called_once_with(999, 1, [])

    def test_telegram_webhook_unknown_command(self, mock_env):
        """Unknown command should return 200 but not crash."""
        handler_mock = MagicMock()
        app = create_webhook_app({"status": handler_mock})

        with app.test_client() as client:
            payload = {
                "message": {
                    "chat": {"id": 999},
                    "message_id": 1,
                    "text": "/unknowncommand",
                }
            }
            response = client.post(
                "/webhook",
                data=json.dumps(payload),
                content_type="application/json",
            )

        assert response.status_code == 200
        handler_mock.assert_not_called()

    def test_telegram_webhook_no_message(self, mock_env):
        """Webhook with no message field returns 200."""
        app = create_webhook_app({})

        with app.test_client() as client:
            response = client.post(
                "/webhook",
                data=json.dumps({"other_update": {}}),
                content_type="application/json",
            )

        assert response.status_code == 200

    def test_telegram_webhook_command_with_args(self, mock_env):
        """Command with arguments should pass args list to handler."""
        handler_mock = MagicMock()
        app = create_webhook_app({"run": handler_mock})

        with app.test_client() as client:
            payload = {
                "message": {
                    "chat": {"id": 999},
                    "message_id": 1,
                    "text": "/run analytics",
                }
            }
            client.post(
                "/webhook",
                data=json.dumps(payload),
                content_type="application/json",
            )

        handler_mock.assert_called_once_with(999, 1, ["analytics"])

    def test_set_webhook(self, mock_env, requests_mock):
        """set_webhook should POST to setWebhook with the URL."""
        client = _make_telegram(mock_env)
        expected_url = "https://api.telegram.org/bot123:TEST/setWebhook"
        requests_mock.post(expected_url, json={"ok": True})

        result = client.set_webhook("https://myserver.com/webhook")
        assert requests_mock.called


# ===========================================================================
# Pinterest Client
# ===========================================================================

class TestPinterestClient:

    def test_pinterest_create_pin(self, mock_env, requests_mock):
        """Mock POST 201, verify pin_id returned."""
        client = _make_pinterest(mock_env)
        url = "https://api.pinterest.com/v5/pins"

        requests_mock.post(url, status_code=201, json={"id": "PIN123", "board_id": "test_board"})

        pin_id = client.create_pin(
            image_url="https://example.com/img.jpg",
            title="Sunrise Print",
            description="Beautiful sunrise.",
            link="https://etsy.com/listing/1001",
        )

        assert pin_id == "PIN123"

    def test_pinterest_rate_limit_retry(self, mock_env, requests_mock):
        """429 then 201, verify retry happens."""
        client = _make_pinterest(mock_env)
        url = "https://api.pinterest.com/v5/pins"

        requests_mock.post(
            url,
            [
                {"status_code": 429, "text": "Rate limited"},
                {"status_code": 201, "json": {"id": "PIN456"}},
            ],
        )

        with patch("time.sleep"):
            pin_id = client.create_pin(
                image_url="https://example.com/img.jpg",
                title="Art Print",
                description="Nice art.",
                link="https://etsy.com/listing/1002",
            )

        assert pin_id == "PIN456"

    def test_pinterest_auth_error(self, mock_env, requests_mock):
        """401 response should raise PinterestAPIError."""
        client = _make_pinterest(mock_env)
        url = "https://api.pinterest.com/v5/pins"

        requests_mock.post(url, status_code=401, text="Unauthorized")

        with pytest.raises(PinterestAPIError) as exc_info:
            client.create_pin(
                image_url="https://example.com/img.jpg",
                title="Art",
                description="desc",
                link="https://etsy.com",
            )

        assert exc_info.value.status_code == 401

    def test_pinterest_create_pin_missing_id(self, mock_env, requests_mock):
        """Response without 'id' raises PinterestAPIError."""
        client = _make_pinterest(mock_env)
        url = "https://api.pinterest.com/v5/pins"

        requests_mock.post(url, status_code=201, json={"message": "created"})

        with pytest.raises(PinterestAPIError):
            client.create_pin(
                image_url="https://example.com/img.jpg",
                title="Art",
                description="desc",
                link="https://etsy.com",
            )

    def test_pinterest_get_boards(self, mock_env, requests_mock):
        """get_boards() returns list of boards."""
        client = _make_pinterest(mock_env)
        url = "https://api.pinterest.com/v5/boards"

        requests_mock.get(url, json={"items": [{"id": "B1"}, {"id": "B2"}]})

        boards = client.get_boards()
        assert len(boards) == 2

    def test_pinterest_500_error(self, mock_env, requests_mock):
        """500 response should raise PinterestAPIError."""
        client = _make_pinterest(mock_env)
        url = "https://api.pinterest.com/v5/pins"

        requests_mock.post(url, status_code=500, text="Server Error")

        with pytest.raises(PinterestAPIError) as exc_info:
            client.create_pin(
                image_url="https://example.com/img.jpg",
                title="Art",
                description="desc",
                link="https://etsy.com",
            )

        assert exc_info.value.status_code == 500


# ===========================================================================
# Instagram Client
# ===========================================================================

class TestInstagramClient:

    def test_instagram_post_two_step(self, mock_env, requests_mock):
        """Mock container create + publish, verify post_id returned."""
        client = _make_instagram(mock_env)

        # Step 1: Create container
        container_url = f"https://graph.facebook.com/v19.0/test_ig_user/media"
        requests_mock.post(container_url, json={"id": "CONTAINER_123"})

        # Step 2: Publish
        publish_url = f"https://graph.facebook.com/v19.0/test_ig_user/media_publish"
        requests_mock.post(publish_url, json={"id": "POST_456"})

        post_id = client.post_image(
            image_url="https://example.com/img.jpg",
            caption="Beautiful art print! #art",
        )

        assert post_id == "POST_456"
        assert requests_mock.call_count == 2

    def test_instagram_container_failure_raises(self, mock_env, requests_mock):
        """Failure at container creation step raises InstagramAPIError."""
        client = _make_instagram(mock_env)
        container_url = f"https://graph.facebook.com/v19.0/test_ig_user/media"

        requests_mock.post(container_url, status_code=400, text="Bad Request")

        with pytest.raises(InstagramAPIError):
            client.post_image(
                image_url="https://example.com/img.jpg",
                caption="Test caption",
            )

    def test_instagram_publish_failure_raises(self, mock_env, requests_mock):
        """Failure at publish step raises InstagramAPIError."""
        client = _make_instagram(mock_env)

        container_url = f"https://graph.facebook.com/v19.0/test_ig_user/media"
        requests_mock.post(container_url, json={"id": "CONTAINER_123"})

        publish_url = f"https://graph.facebook.com/v19.0/test_ig_user/media_publish"
        requests_mock.post(publish_url, status_code=500, text="Server Error")

        with pytest.raises(InstagramAPIError):
            client.post_image(
                image_url="https://example.com/img.jpg",
                caption="Test caption",
            )

    def test_instagram_api_error_in_response_body(self, mock_env, requests_mock):
        """200 response with 'error' key in body raises InstagramAPIError."""
        client = _make_instagram(mock_env)
        container_url = f"https://graph.facebook.com/v19.0/test_ig_user/media"

        requests_mock.post(container_url, json={
            "error": {"message": "Invalid token", "code": 190}
        })

        with pytest.raises(InstagramAPIError) as exc_info:
            client.post_image(
                image_url="https://example.com/img.jpg",
                caption="Test caption",
            )

        assert "Invalid token" in str(exc_info.value)

    def test_instagram_missing_container_id_raises(self, mock_env, requests_mock):
        """Response without 'id' at container step raises InstagramAPIError."""
        client = _make_instagram(mock_env)
        container_url = f"https://graph.facebook.com/v19.0/test_ig_user/media"

        requests_mock.post(container_url, json={"status": "ok"})  # no 'id'

        with pytest.raises(InstagramAPIError):
            client.post_image(
                image_url="https://example.com/img.jpg",
                caption="Test caption",
            )

    def test_instagram_missing_post_id_raises(self, mock_env, requests_mock):
        """Response without 'id' at publish step raises InstagramAPIError."""
        client = _make_instagram(mock_env)

        container_url = f"https://graph.facebook.com/v19.0/test_ig_user/media"
        requests_mock.post(container_url, json={"id": "CONTAINER_123"})

        publish_url = f"https://graph.facebook.com/v19.0/test_ig_user/media_publish"
        requests_mock.post(publish_url, json={"status": "published"})  # no 'id'

        with pytest.raises(InstagramAPIError):
            client.post_image(
                image_url="https://example.com/img.jpg",
                caption="Test caption",
            )
