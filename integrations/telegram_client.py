"""
TelegramClient — wrapper for the Telegram Bot API.

Handles outbound messages (send_message, send_alert, send_digest, reply_to_command)
and provides a Flask webhook factory for inbound command routing.
"""

import os
import logging

import requests
from flask import Flask, request as flask_request, jsonify
from dotenv import load_dotenv
from pathlib import Path

_ENV_PATH = Path(__file__).parent.parent / ".env"

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/{method}"


class TelegramClient:
    """Thin wrapper around the Telegram Bot API for the Etsy AI Agent system."""

    def __init__(self):
        load_dotenv(_ENV_PATH)
        self.bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
        self.chat_id = os.environ["TELEGRAM_CHAT_ID"]

    # ------------------------------------------------------------------
    # Internal request helper
    # ------------------------------------------------------------------

    def _post(self, method: str, payload: dict) -> dict:
        """POST to the Telegram Bot API. Returns the response JSON."""
        url = TELEGRAM_API_BASE.format(token=self.bot_token, method=method)
        resp = requests.post(url, json=payload, timeout=15)
        if not resp.ok:
            logger.error("Telegram API error %s: %s", resp.status_code, resp.text)
            resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Outbound message methods
    # ------------------------------------------------------------------

    def send_message(self, text: str, parse_mode: str = "Markdown") -> dict:
        """Send a plain message to the configured chat."""
        return self._post("sendMessage", {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
        })

    def send_alert(self, text: str) -> dict:
        """Send an alert message prefixed with the alert emoji."""
        return self.send_message(f"🚨 {text}", parse_mode="Markdown")

    def send_digest(self, text: str) -> dict:
        """Send a digest message prefixed with the chart emoji."""
        return self.send_message(f"📊 {text}", parse_mode="Markdown")

    def reply_to_command(self, chat_id: str | int, message_id: int, text: str,
                         parse_mode: str = "Markdown") -> dict:
        """Reply to a specific message by chat_id and message_id."""
        return self._post("sendMessage", {
            "chat_id": chat_id,
            "text": text,
            "reply_to_message_id": message_id,
            "parse_mode": parse_mode,
        })

    # ------------------------------------------------------------------
    # Webhook setup
    # ------------------------------------------------------------------

    def set_webhook(self, url: str) -> dict:
        """Register a webhook URL with Telegram."""
        result = self._post("setWebhook", {"url": url})
        logger.info("Webhook set to %s: %s", url, result)
        return result


# ---------------------------------------------------------------------------
# Flask webhook factory
# ---------------------------------------------------------------------------

def create_webhook_app(command_handlers: dict) -> Flask:
    """
    Build and return a Flask app with a /webhook POST endpoint.

    Parameters
    ----------
    command_handlers : dict
        Mapping of command string (without slash) to callable.
        Each callable receives (chat_id: str|int, message_id: int, args: list[str]).

        Example:
            {
                "status": handle_status,
                "report": handle_report,
            }

    Registered commands: /status /report /listings /budget /run /pause /resume /logs
    """
    app = Flask(__name__)

    @app.route("/webhook", methods=["POST"])
    def webhook():
        update = flask_request.get_json(force=True, silent=True) or {}

        message = update.get("message") or update.get("edited_message")
        if not message:
            # Telegram may send other update types (callback_query, etc.) — ignore
            return jsonify({"ok": True}), 200

        chat_id = message.get("chat", {}).get("id")
        message_id = message.get("message_id")
        text = message.get("text", "").strip()

        if text.startswith("/"):
            # Split off the command and any @bot_name suffix
            parts = text.split()
            raw_command = parts[0].lstrip("/").split("@")[0].lower()
            args = parts[1:]

            handler = command_handlers.get(raw_command)
            if handler:
                try:
                    handler(chat_id, message_id, args)
                except Exception:
                    logger.exception(
                        "Error in handler for /%s (chat=%s)", raw_command, chat_id
                    )
            else:
                logger.debug("No handler registered for command: /%s", raw_command)

        # Always return 200 — Telegram will retry on non-2xx
        return jsonify({"ok": True}), 200

    return app
