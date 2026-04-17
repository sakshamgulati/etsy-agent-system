#!/usr/bin/env python3
"""
auth_setup.py — One-time OAuth 2.0 PKCE flow for Etsy API v3.

Run this script once to obtain initial access_token + refresh_token and
save them into .env.

Usage:
    python auth_setup.py

Requirements:
    ETSY_API_KEY must already be set in .env (or as an env var).
    Flask and requests must be installed (both are in requirements.txt).
"""

import base64
import hashlib
import os
import secrets
import threading
import urllib.parse
import webbrowser
from pathlib import Path

import requests
from dotenv import load_dotenv, set_key
from flask import Flask, request as flask_request

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_ENV_PATH = Path(__file__).parent / ".env"

ETSY_AUTH_URL = "https://www.etsy.com/oauth/connect"
ETSY_TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"
REDIRECT_URI = "http://localhost:8899/callback"
SCOPES = "listings_r listings_w shops_r transactions_r billing_r"
PORT = 8899

# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------


def _generate_code_verifier(length: int = 64) -> str:
    """Generate a cryptographically random code_verifier (RFC 7636)."""
    return secrets.token_urlsafe(length)[:128]  # max 128 chars per spec


def _generate_code_challenge(verifier: str) -> str:
    """Derive S256 code_challenge from verifier."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------


def main():
    load_dotenv(_ENV_PATH)
    api_key = os.environ.get("ETSY_API_KEY")
    if not api_key:
        raise SystemExit(
            "ETSY_API_KEY not found. Set it in .env before running this script."
        )

    code_verifier = _generate_code_verifier()
    code_challenge = _generate_code_challenge(code_verifier)
    state = secrets.token_urlsafe(16)

    params = {
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "client_id": api_key,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    auth_url = ETSY_AUTH_URL + "?" + urllib.parse.urlencode(params)

    # Shared container for the auth code received by the Flask callback
    result: dict = {}
    shutdown_event = threading.Event()

    # -----------------------------------------------------------------------
    # Minimal Flask server to catch the redirect
    # -----------------------------------------------------------------------
    app = Flask(__name__)
    log = app.logger
    # Silence Flask request logs to keep the terminal clean
    import logging
    log.setLevel(logging.ERROR)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    @app.route("/callback")
    def callback():
        error = flask_request.args.get("error")
        if error:
            result["error"] = error
            shutdown_event.set()
            return f"<h2>Auth error: {error}</h2><p>You can close this tab.</p>", 400

        received_state = flask_request.args.get("state")
        if received_state != state:
            result["error"] = "state_mismatch"
            shutdown_event.set()
            return "<h2>State mismatch — possible CSRF.</h2>", 400

        code = flask_request.args.get("code")
        result["code"] = code
        shutdown_event.set()
        return (
            "<h2>Authorization successful!</h2>"
            "<p>Tokens are being saved. You can close this tab.</p>"
        )

    server_thread = threading.Thread(
        target=lambda: app.run(port=PORT, use_reloader=False, threaded=True),
        daemon=True,
    )
    server_thread.start()

    print(f"\nOpening Etsy authorization URL in your browser...")
    print(f"If the browser doesn't open, navigate to:\n\n  {auth_url}\n")
    webbrowser.open(auth_url)

    print("Waiting for authorization callback on http://localhost:8899/callback ...")
    shutdown_event.wait(timeout=300)  # 5-minute timeout

    if "error" in result:
        raise SystemExit(f"Authorization failed: {result['error']}")
    if "code" not in result:
        raise SystemExit("Timed out waiting for authorization code.")

    auth_code = result["code"]
    print("Authorization code received. Exchanging for tokens...")

    # -----------------------------------------------------------------------
    # Token exchange
    # -----------------------------------------------------------------------
    token_payload = {
        "grant_type": "authorization_code",
        "client_id": api_key,
        "redirect_uri": REDIRECT_URI,
        "code": auth_code,
        "code_verifier": code_verifier,
    }
    resp = requests.post(ETSY_TOKEN_URL, data=token_payload, timeout=30)
    if not resp.ok:
        raise SystemExit(
            f"Token exchange failed ({resp.status_code}): {resp.text}"
        )

    tokens = resp.json()
    access_token = tokens["access_token"]
    refresh_token = tokens["refresh_token"]

    # -----------------------------------------------------------------------
    # Persist tokens to .env
    # -----------------------------------------------------------------------
    env_file = str(_ENV_PATH)
    set_key(env_file, "ETSY_ACCESS_TOKEN", access_token)
    set_key(env_file, "ETSY_REFRESH_TOKEN", refresh_token)

    print("\nAuth complete! Tokens saved to .env")
    print(f"  ETSY_ACCESS_TOKEN: {access_token[:20]}...")
    print(f"  ETSY_REFRESH_TOKEN: {refresh_token[:20]}...")


if __name__ == "__main__":
    main()
