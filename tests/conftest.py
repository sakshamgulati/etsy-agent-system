"""
conftest.py — Shared pytest fixtures for the Etsy AI Agent test suite.
"""

import os
import tempfile
import shutil
import pytest


# ---------------------------------------------------------------------------
# Environment fixture
# ---------------------------------------------------------------------------

FAKE_ENV = {
    "ETSY_API_KEY": "test_key",
    "ETSY_API_SECRET": "test_secret",
    "ETSY_ACCESS_TOKEN": "test_token",
    "ETSY_REFRESH_TOKEN": "test_refresh",
    "ETSY_SHOP_ID": "TestShop",
    "ANTHROPIC_API_KEY": "test_anthropic",
    "TELEGRAM_BOT_TOKEN": "123:TEST",
    "TELEGRAM_CHAT_ID": "999",
    "PINTEREST_ACCESS_TOKEN": "test_pinterest",
    "PINTEREST_BOARD_ID": "test_board",
    "INSTAGRAM_ACCESS_TOKEN": "test_instagram",
    "INSTAGRAM_USER_ID": "test_ig_user",
    "COGS_PRINT_COST": "8.00",
    "COGS_SHIPPING_COST": "5.00",
    "WEEKLY_AD_BUDGET_CAP": "100.00",
    "PRICE_CHANGE_APPROVAL_THRESHOLD": "0.15",
}


@pytest.fixture
def mock_env(monkeypatch):
    """Patch os.environ with all required env vars (fake values)."""
    for key, value in FAKE_ENV.items():
        monkeypatch.setenv(key, value)
    return FAKE_ENV


# ---------------------------------------------------------------------------
# Temporary database fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """
    Create a fresh SQLite DB in a temp dir, call init_db(), set ETSY_DB_PATH env var,
    yield the path, then clean up.
    """
    db_file = tmp_path / "test_etsy_agent.db"
    monkeypatch.setenv("ETSY_DB_PATH", str(db_file))

    # Patch the DB_PATH in the database module so it uses our temp DB
    import db.database as db_module
    monkeypatch.setattr(db_module, "DB_PATH", str(db_file))

    db_module.init_db()
    yield str(db_file)


# ---------------------------------------------------------------------------
# Fake listing dicts
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_etsy_listings():
    """Return a list of 3 fake listing dicts matching Etsy API v3 shape."""
    return [
        {
            "listing_id": 1001,
            "title": "Watercolor Sunrise Print",
            "description": "A beautiful watercolor sunrise wall art print.",
            "tags": ["watercolor", "sunrise", "wall art", "print", "home decor",
                     "gift", "art print", "nature", "landscape", "modern", "boho", "colorful", "abstract"],
            "price": {"amount": 2500, "divisor": 100, "currency_code": "USD"},
            "views": 150,
            "num_favorers": 12,
            "quantity": 999,
            "state": "active",
        },
        {
            "listing_id": 1002,
            "title": "Mountain Landscape Art",
            "description": "Stunning mountain landscape digital art print.",
            "tags": ["mountain", "landscape", "art", "print", "digital", "nature",
                     "wall decor", "home", "gift", "poster", "scenic", "outdoors", "adventure"],
            "price": {"amount": 3000, "divisor": 100, "currency_code": "USD"},
            "views": 200,
            "num_favorers": 20,
            "quantity": 999,
            "state": "active",
        },
        {
            "listing_id": 1003,
            "title": "Botanical Garden Print",
            "description": "Elegant botanical garden illustration.",
            "tags": ["botanical", "garden", "print", "floral", "plant", "nature",
                     "green", "wall art", "home", "decor", "illustration", "vintage", "boho"],
            "price": {"amount": 2000, "divisor": 100, "currency_code": "USD"},
            "views": 80,
            "num_favorers": 5,
            "quantity": 999,
            "state": "active",
        },
    ]
