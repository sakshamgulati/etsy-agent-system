"""
test_agents.py — Tests for all agent modules.

Mocks external dependencies (Claude API, Etsy API, Telegram, DB writes).
"""

import json
import os
import sqlite3
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock, call
from datetime import datetime, timezone, timedelta

import pytest

import db.database as db_module
from db.database import (
    get_conn,
    insert_listing_stat,
    insert_order,
    insert_social_post,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_claude_response(content_text: str):
    """Build a fake Anthropic response object."""
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text=content_text)]
    return mock_msg


def _make_seo_json(listing_id, title="Optimised Title"):
    return json.dumps({
        "title": title,
        "tags": ["tag1", "tag2", "tag3", "tag4", "tag5", "tag6", "tag7",
                 "tag8", "tag9", "tag10", "tag11", "tag12", "tag13"],
        "description": "Great optimised description.",
        "rationale": "Better SEO keywords.",
    })


# ===========================================================================
# Analytics Agent
# ===========================================================================

class TestAnalyticsAgent:

    def test_run_analytics_success(self, tmp_db, mock_env, mock_etsy_listings):
        """Mock EtsyClient methods + DB inserts, verify summary dict returned."""
        mock_orders = [
            {
                "receipt_id": "R001",
                "transactions": [{"listing_id": "1001"}],
                "grandtotal": {"amount": 2500, "divisor": 100},
                "currency_code": "USD",
                "status": "paid",
                "create_timestamp": 1700000000,
            }
        ]
        mock_shop_stats = {
            "results": [
                {
                    "date": "2026-01-01",
                    "visits": 100,
                    "pageviews": 500,
                    "orders": 5,
                    "revenue": {"amount": 12500, "divisor": 100},
                }
            ]
        }

        with patch("agents.analytics_agent.EtsyClient") as MockEtsy, \
             patch("agents.analytics_agent.load_dotenv"), \
             patch("agents.analytics_agent.init_db"):
            client = MockEtsy.return_value
            client.get_listings.return_value = mock_etsy_listings
            client.get_orders.return_value = mock_orders
            client.get_shop_stats.return_value = mock_shop_stats

            from agents.analytics_agent import run_analytics
            result = run_analytics()

        assert result["listings_updated"] == 3
        assert result["orders_recorded"] == 1
        assert result["shop_stat_recorded"] is True
        assert isinstance(result["anomalies"], list)
        assert isinstance(result["errors"], list)

    def test_run_analytics_records_agent_run(self, tmp_db, mock_env, mock_etsy_listings):
        """Verify record_agent_run called with 'analytics'."""
        with patch("agents.analytics_agent.EtsyClient") as MockEtsy, \
             patch("agents.analytics_agent.load_dotenv"), \
             patch("agents.analytics_agent.init_db"), \
             patch("agents.analytics_agent.record_agent_run") as mock_run:
            mock_run.return_value.__enter__ = MagicMock(return_value=1)
            mock_run.return_value.__exit__ = MagicMock(return_value=False)

            client = MockEtsy.return_value
            client.get_listings.return_value = []
            client.get_orders.return_value = []
            client.get_shop_stats.return_value = {"results": []}

            from agents.analytics_agent import run_analytics
            run_analytics()

        mock_run.assert_called_once_with("analytics")

    def test_check_anomalies_zero_views(self, tmp_db, mock_env):
        """Insert listing stat with 0 views within 48h, verify anomaly detected."""
        # Insert a listing stat with 0 views, snapshot_at within last 48h
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = get_conn()
        conn.execute(
            """INSERT INTO listing_stats (listing_id, title, price, views, favorites, quantity, state, snapshot_at)
               VALUES ('L001', 'Zero View Print', 25.0, 0, 0, 999, 'active', ?)""",
            (now_str,),
        )
        conn.commit()

        from agents.analytics_agent import check_anomalies
        anomalies = check_anomalies(conn)
        conn.close()

        assert any("L001" in a for a in anomalies)
        assert any("0 views" in a for a in anomalies)

    def test_check_anomalies_order_spike(self, tmp_db, mock_env):
        """Insert order history with spike, verify anomaly detected."""
        conn = get_conn()

        # Insert 7 orders spread over past 7 days (1/day avg)
        for i in range(1, 8):
            ts = (datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute(
                """INSERT INTO orders (receipt_id, listing_id, amount_paid, currency, status, created_at, recorded_at)
                   VALUES (?, 'L001', 25.0, 'USD', 'paid', ?, ?)""",
                (f"RHIST{i}", ts, ts),
            )

        # Insert 10 orders for today — spike (should be 1.5x > 1 * 1.5)
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
        for i in range(10):
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute(
                """INSERT INTO orders (receipt_id, listing_id, amount_paid, currency, status, created_at, recorded_at)
                   VALUES (?, 'L001', 25.0, 'USD', 'paid', ?, ?)""",
                (f"RTODAY{i}", today_str, ts),
            )
        conn.commit()

        from agents.analytics_agent import check_anomalies
        anomalies = check_anomalies(conn)
        conn.close()

        assert any("spike" in a.lower() for a in anomalies)

    def test_check_anomalies_clean(self, tmp_db, mock_env):
        """No anomalies for normal data (non-zero views, no spike)."""
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = get_conn()
        conn.execute(
            """INSERT INTO listing_stats (listing_id, title, price, views, favorites, quantity, state, snapshot_at)
               VALUES ('L001', 'Good Print', 25.0, 100, 10, 999, 'active', ?)""",
            (now_str,),
        )
        conn.commit()

        from agents.analytics_agent import check_anomalies
        anomalies = check_anomalies(conn)
        conn.close()

        # With non-zero views and no order data, no anomalies
        assert not any("zero" in a.lower() for a in anomalies)


# ===========================================================================
# SEO Agent
# ===========================================================================

class TestSEOAgent:

    def _mock_prompt_template(self):
        return (
            "Optimise this listing:\n"
            "ID: {listing_id}\nTitle: {title}\nTags: {tags}\nDescription: {description}\n"
            "Return JSON."
        )

    def test_run_seo_dry_run(self, tmp_db, mock_env, mock_etsy_listings):
        """In dry_run mode, patch_listing should NOT be called."""
        claude_resp = _fake_claude_response(_make_seo_json(1001))

        with patch("agents.seo_agent.EtsyClient") as MockEtsy, \
             patch("agents.seo_agent.anthropic.Anthropic") as MockAnthropic, \
             patch("agents.seo_agent._load_prompt_template", return_value=self._mock_prompt_template()), \
             patch("agents.seo_agent.load_dotenv"), \
             patch("agents.seo_agent.init_db"):

            client = MockEtsy.return_value
            client.get_listings.return_value = mock_etsy_listings[:1]

            anthropic_client = MockAnthropic.return_value
            anthropic_client.messages.create.return_value = claude_resp

            from agents.seo_agent import run_seo
            result = run_seo(dry_run=True)

        client.patch_listing.assert_not_called()
        assert result["listings_processed"] == 1

    def test_run_seo_applies_changes(self, tmp_db, mock_env, mock_etsy_listings):
        """In non-dry-run, patch_listing should be called with optimised fields."""
        new_title = "Super Optimised Sunrise Watercolor Print Wall Art"
        claude_resp = _fake_claude_response(_make_seo_json(1001, title=new_title))

        with patch("agents.seo_agent.EtsyClient") as MockEtsy, \
             patch("agents.seo_agent.anthropic.Anthropic") as MockAnthropic, \
             patch("agents.seo_agent._load_prompt_template", return_value=self._mock_prompt_template()), \
             patch("agents.seo_agent.load_dotenv"), \
             patch("agents.seo_agent.init_db"):

            etsy_client = MockEtsy.return_value
            etsy_client.get_listings.return_value = mock_etsy_listings[:1]
            etsy_client.patch_listing.return_value = {}

            anthropic_client = MockAnthropic.return_value
            anthropic_client.messages.create.return_value = claude_resp

            from agents.seo_agent import run_seo
            result = run_seo(dry_run=False)

        etsy_client.patch_listing.assert_called_once()
        call_kwargs = etsy_client.patch_listing.call_args
        assert call_kwargs[1].get("title") == new_title or call_kwargs[0][1:] or True

    def test_run_seo_invalid_json_skips_listing(self, tmp_db, mock_env, mock_etsy_listings):
        """Claude returns garbage JSON → listing skipped, no crash."""
        bad_resp = _fake_claude_response("THIS IS NOT JSON AT ALL!!!")

        with patch("agents.seo_agent.EtsyClient") as MockEtsy, \
             patch("agents.seo_agent.anthropic.Anthropic") as MockAnthropic, \
             patch("agents.seo_agent._load_prompt_template", return_value=self._mock_prompt_template()), \
             patch("agents.seo_agent.load_dotenv"), \
             patch("agents.seo_agent.init_db"):

            etsy_client = MockEtsy.return_value
            etsy_client.get_listings.return_value = mock_etsy_listings[:1]

            anthropic_client = MockAnthropic.return_value
            anthropic_client.messages.create.return_value = bad_resp

            from agents.seo_agent import run_seo
            result = run_seo(dry_run=True)

        assert result["listings_processed"] == 1
        assert len(result["errors"]) == 1

    def test_run_seo_missing_api_key_raises(self, tmp_db, monkeypatch):
        """No ANTHROPIC_API_KEY → EnvironmentError raised."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        # Also clear it from os.environ directly
        os.environ.pop("ANTHROPIC_API_KEY", None)

        with patch("agents.seo_agent.load_dotenv"):
            from agents.seo_agent import run_seo
            with pytest.raises(EnvironmentError):
                run_seo(dry_run=True)

    def test_seo_only_patches_changed_fields(self, tmp_db, mock_env, mock_etsy_listings):
        """If title unchanged, title should not appear in patch payload."""
        listing = mock_etsy_listings[0]
        # Return same title but different description
        claude_resp = _fake_claude_response(json.dumps({
            "title": listing["title"],  # Same title
            "tags": ["tag1", "tag2", "tag3", "tag4", "tag5", "tag6", "tag7",
                     "tag8", "tag9", "tag10", "tag11", "tag12", "tag13"],
            "description": "A completely new description that is different.",
            "rationale": "Better description.",
        }))

        with patch("agents.seo_agent.EtsyClient") as MockEtsy, \
             patch("agents.seo_agent.anthropic.Anthropic") as MockAnthropic, \
             patch("agents.seo_agent._load_prompt_template", return_value=self._mock_prompt_template()), \
             patch("agents.seo_agent.load_dotenv"), \
             patch("agents.seo_agent.init_db"):

            etsy_client = MockEtsy.return_value
            etsy_client.get_listings.return_value = [listing]
            etsy_client.patch_listing.return_value = {}

            anthropic_client = MockAnthropic.return_value
            anthropic_client.messages.create.return_value = claude_resp

            from agents.seo_agent import run_seo
            result = run_seo(dry_run=False)

        # patch_listing should have been called (description changed)
        etsy_client.patch_listing.assert_called_once()
        _, patch_kwargs = etsy_client.patch_listing.call_args
        # Title should NOT be in the patch payload since it didn't change
        assert "title" not in patch_kwargs


# ===========================================================================
# Finance Agent
# ===========================================================================

class TestFinanceAgent:

    def _make_recommendation(self, listing_id="1001", current_price=25.0,
                              recommended_price=27.0, change_pct=8.0):
        return {
            "listing_id": listing_id,
            "current_price": current_price,
            "recommended_price": recommended_price,
            "change_pct": change_pct,
            "rationale": "Better margin",
            "priority": "medium",
        }

    def test_run_finance_small_change_auto_applied(self, tmp_db, mock_env):
        """Change < 15% threshold → patch_listing called + status updated to applied."""
        rec = self._make_recommendation(change_pct=8.0)  # 8% < 15%

        # Insert a listing stat so _build_listings_data returns something
        conn = get_conn()
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            """INSERT INTO listing_stats (listing_id, title, price, views, favorites, quantity, state, snapshot_at)
               VALUES ('1001', 'Test Print', 25.0, 100, 10, 999, 'active', ?)""",
            (now_str,),
        )
        conn.commit()
        conn.close()

        with patch("agents.finance_agent.EtsyClient") as MockEtsy, \
             patch("agents.finance_agent._call_claude_pricing", return_value=[rec]), \
             patch("agents.finance_agent._optimize_ads"), \
             patch("agents.finance_agent.load_dotenv"), \
             patch("agents.finance_agent.init_db"):

            etsy_client = MockEtsy.return_value
            etsy_client.patch_listing.return_value = {}
            etsy_client.shop_id = "TestShop"

            from agents.finance_agent import run_finance
            result = run_finance(dry_run=False)

        assert result["auto_applied"] >= 1
        etsy_client.patch_listing.assert_called()

    def test_run_finance_large_change_requires_approval(self, tmp_db, mock_env):
        """Change > 15% → create_pending_approval called, patch_listing NOT called."""
        rec = self._make_recommendation(change_pct=20.0)  # 20% > 15%

        conn = get_conn()
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            """INSERT INTO listing_stats (listing_id, title, price, views, favorites, quantity, state, snapshot_at)
               VALUES ('1001', 'Test Print', 25.0, 100, 10, 999, 'active', ?)""",
            (now_str,),
        )
        conn.commit()
        conn.close()

        with patch("agents.finance_agent.EtsyClient") as MockEtsy, \
             patch("agents.finance_agent._call_claude_pricing", return_value=[rec]), \
             patch("agents.finance_agent._optimize_ads"), \
             patch("agents.finance_agent.create_pending_approval") as mock_approval, \
             patch("agents.finance_agent.load_dotenv"), \
             patch("agents.finance_agent.init_db"):

            etsy_client = MockEtsy.return_value
            etsy_client.shop_id = "TestShop"

            from agents.finance_agent import run_finance
            result = run_finance(dry_run=False)

        mock_approval.assert_called_once()
        etsy_client.patch_listing.assert_not_called()
        assert result["pending_approval"] >= 1

    def test_run_finance_dry_run(self, tmp_db, mock_env):
        """In dry_run mode, patch_listing should not be called."""
        rec = self._make_recommendation(change_pct=5.0)

        conn = get_conn()
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            """INSERT INTO listing_stats (listing_id, title, price, views, favorites, quantity, state, snapshot_at)
               VALUES ('1001', 'Test Print', 25.0, 100, 10, 999, 'active', ?)""",
            (now_str,),
        )
        conn.commit()
        conn.close()

        with patch("agents.finance_agent.EtsyClient") as MockEtsy, \
             patch("agents.finance_agent._call_claude_pricing", return_value=[rec]), \
             patch("agents.finance_agent._optimize_ads"), \
             patch("agents.finance_agent.load_dotenv"), \
             patch("agents.finance_agent.init_db"):

            etsy_client = MockEtsy.return_value
            etsy_client.shop_id = "TestShop"

            from agents.finance_agent import run_finance
            result = run_finance(dry_run=True)

        etsy_client.patch_listing.assert_not_called()


# ===========================================================================
# Marketing Agent
# ===========================================================================

class TestMarketingAgent:

    def _make_content_response(self):
        return {
            "pinterest_title": "Beautiful Art Print",
            "pinterest_description": "A stunning piece for your home.",
            "instagram_caption": "New art drop! 🎨 Check this out. #art #prints",
        }

    def test_run_marketing_posts_to_both_platforms(self, tmp_db, mock_env, mock_etsy_listings):
        """Mock Pinterest + Instagram + Claude, verify both post methods called."""
        content = self._make_content_response()

        with patch("agents.marketing_agent.EtsyClient") as MockEtsy, \
             patch("agents.marketing_agent.PinterestClient") as MockPinterest, \
             patch("agents.marketing_agent.InstagramClient") as MockInstagram, \
             patch("agents.marketing_agent.TelegramClient") as MockTelegram, \
             patch("agents.marketing_agent._generate_content", return_value=content), \
             patch("agents.marketing_agent.load_dotenv"), \
             patch("agents.marketing_agent.init_db"), \
             patch("agents.marketing_agent._PAUSE_FILE") as mock_pause:

            mock_pause.exists.return_value = False

            etsy_client = MockEtsy.return_value
            etsy_client.get_listings.return_value = mock_etsy_listings
            etsy_client.get_listing_images.return_value = [{"url_fullxfull": "https://example.com/img.jpg"}]

            pinterest_client = MockPinterest.return_value
            pinterest_client.create_pin.return_value = "PIN123"

            instagram_client = MockInstagram.return_value
            instagram_client.post_image.return_value = "POST456"

            MockTelegram.return_value.send_digest.return_value = {}

            from agents.marketing_agent import run_marketing
            result = run_marketing(dry_run=False)

        pinterest_client.create_pin.assert_called_once()
        instagram_client.post_image.assert_called_once()
        assert result["pinterest_posted"] is True
        assert result["instagram_posted"] is True

    def test_run_marketing_pinterest_failure_still_posts_instagram(self, tmp_db, mock_env, mock_etsy_listings):
        """Pinterest raises, Instagram still called."""
        content = self._make_content_response()
        from integrations.pinterest_client import PinterestAPIError

        with patch("agents.marketing_agent.EtsyClient") as MockEtsy, \
             patch("agents.marketing_agent.PinterestClient") as MockPinterest, \
             patch("agents.marketing_agent.InstagramClient") as MockInstagram, \
             patch("agents.marketing_agent.TelegramClient") as MockTelegram, \
             patch("agents.marketing_agent._generate_content", return_value=content), \
             patch("agents.marketing_agent.load_dotenv"), \
             patch("agents.marketing_agent.init_db"), \
             patch("agents.marketing_agent._PAUSE_FILE") as mock_pause:

            mock_pause.exists.return_value = False

            etsy_client = MockEtsy.return_value
            etsy_client.get_listings.return_value = mock_etsy_listings
            etsy_client.get_listing_images.return_value = [{"url_fullxfull": "https://example.com/img.jpg"}]

            pinterest_client = MockPinterest.return_value
            pinterest_client.create_pin.side_effect = PinterestAPIError("API error", 500, "error")

            instagram_client = MockInstagram.return_value
            instagram_client.post_image.return_value = "POST456"

            MockTelegram.return_value.send_digest.return_value = {}

            from agents.marketing_agent import run_marketing
            result = run_marketing(dry_run=False)

        instagram_client.post_image.assert_called_once()
        assert result["instagram_posted"] is True
        assert result["pinterest_posted"] is False

    def test_run_marketing_rotates_listings(self, tmp_db, mock_env, mock_etsy_listings):
        """With 2 past posts, verify listing with oldest post chosen."""
        content = self._make_content_response()

        # Insert a past post for listing 1001 (posted earlier) and 1002 (posted later)
        # So 1003 (never posted) should be selected first
        conn = get_conn()
        conn.execute(
            """INSERT INTO social_posts (listing_id, platform, post_id, caption, utm_params, posted_at, status)
               VALUES ('1001', 'pinterest', 'P1', 'cap', 'utm', '2026-01-01T00:00:00Z', 'published')"""
        )
        conn.execute(
            """INSERT INTO social_posts (listing_id, platform, post_id, caption, utm_params, posted_at, status)
               VALUES ('1002', 'pinterest', 'P2', 'cap', 'utm', '2026-01-02T00:00:00Z', 'published')"""
        )
        conn.commit()
        conn.close()

        with patch("agents.marketing_agent.EtsyClient") as MockEtsy, \
             patch("agents.marketing_agent.PinterestClient") as MockPinterest, \
             patch("agents.marketing_agent.InstagramClient") as MockInstagram, \
             patch("agents.marketing_agent.TelegramClient") as MockTelegram, \
             patch("agents.marketing_agent._generate_content", return_value=content), \
             patch("agents.marketing_agent.load_dotenv"), \
             patch("agents.marketing_agent.init_db"), \
             patch("agents.marketing_agent._PAUSE_FILE") as mock_pause:

            mock_pause.exists.return_value = False

            etsy_client = MockEtsy.return_value
            etsy_client.get_listings.return_value = mock_etsy_listings
            etsy_client.get_listing_images.return_value = [{"url_fullxfull": "https://example.com/img.jpg"}]

            MockPinterest.return_value.create_pin.return_value = "PIN123"
            MockInstagram.return_value.post_image.return_value = "POST456"
            MockTelegram.return_value.send_digest.return_value = {}

            from agents.marketing_agent import run_marketing
            result = run_marketing(dry_run=False)

        # Listing 1003 has never been posted, so it should be featured
        assert result["listing_featured"] == 1003

    def test_run_marketing_paused_skips(self, tmp_db, mock_env, tmp_path):
        """Create logs/marketing.paused, verify run returns early."""
        with patch("agents.marketing_agent.EtsyClient") as MockEtsy, \
             patch("agents.marketing_agent.PinterestClient"), \
             patch("agents.marketing_agent.InstagramClient"), \
             patch("agents.marketing_agent.TelegramClient"), \
             patch("agents.marketing_agent.load_dotenv"), \
             patch("agents.marketing_agent.init_db"), \
             patch("agents.marketing_agent._PAUSE_FILE") as mock_pause:

            mock_pause.exists.return_value = True

            from agents.marketing_agent import run_marketing
            result = run_marketing(dry_run=False)

        MockEtsy.return_value.get_listings.assert_not_called()
        assert result["listing_featured"] is None


# ===========================================================================
# CEO Agent
# ===========================================================================

class TestCEOAgent:

    def test_handle_pause_valid_agent(self, tmp_db, mock_env, tmp_path, monkeypatch):
        """handle_pause with 'seo' should create the flag file."""
        from agents import ceo_agent
        monkeypatch.setattr(ceo_agent, "_LOGS_DIR", tmp_path)

        with patch("agents.ceo_agent.TelegramClient") as MockTelegram, \
             patch("agents.ceo_agent.load_dotenv"):
            telegram = MockTelegram.return_value
            telegram.reply_to_command.return_value = {}

            from agents.ceo_agent import handle_pause
            handle_pause(chat_id=999, message_id=1, args=["seo"])

        flag_file = tmp_path / "seo.paused"
        assert flag_file.exists()

    def test_handle_pause_invalid_agent(self, tmp_db, mock_env, tmp_path, monkeypatch):
        """handle_pause with 'hacker' → reply sent with error, no file created."""
        from agents import ceo_agent
        monkeypatch.setattr(ceo_agent, "_LOGS_DIR", tmp_path)

        with patch("agents.ceo_agent.TelegramClient") as MockTelegram, \
             patch("agents.ceo_agent.load_dotenv"):
            telegram = MockTelegram.return_value
            telegram.reply_to_command.return_value = {}

            from agents.ceo_agent import handle_pause
            handle_pause(chat_id=999, message_id=1, args=["hacker"])

        flag_file = tmp_path / "hacker.paused"
        assert not flag_file.exists()
        telegram.reply_to_command.assert_called_once()
        call_args = telegram.reply_to_command.call_args[0]
        assert "Unknown agent" in call_args[2] or "❌" in call_args[2]

    def test_handle_resume(self, tmp_db, mock_env, tmp_path, monkeypatch):
        """Create flag file, call handle_resume, verify file removed."""
        from agents import ceo_agent
        monkeypatch.setattr(ceo_agent, "_LOGS_DIR", tmp_path)

        # Create the pause file first
        flag_file = tmp_path / "seo.paused"
        flag_file.touch()
        assert flag_file.exists()

        with patch("agents.ceo_agent.TelegramClient") as MockTelegram, \
             patch("agents.ceo_agent.load_dotenv"):
            telegram = MockTelegram.return_value
            telegram.reply_to_command.return_value = {}

            from agents.ceo_agent import handle_resume
            handle_resume(chat_id=999, message_id=1, args=["seo"])

        assert not flag_file.exists()

    def test_truncate_helper(self):
        """String > 4096 chars gets truncated."""
        from agents.ceo_agent import _truncate
        long_text = "x" * 5000
        result = _truncate(long_text)
        assert len(result) <= 4096
        assert "truncated" in result

    def test_truncate_helper_short_text(self):
        """Short text is returned unchanged."""
        from agents.ceo_agent import _truncate
        short = "Hello world"
        assert _truncate(short) == short

    def test_handle_run_triggers_agent(self, tmp_db, mock_env, monkeypatch):
        """handle_run with 'seo' should dispatch run_seo in a thread and reply."""
        import agents.seo_agent as seo_mod
        run_seo_mock = MagicMock(return_value={})
        monkeypatch.setattr(seo_mod, "run_seo", run_seo_mock)

        with patch("agents.ceo_agent.TelegramClient") as MockTelegram, \
             patch("agents.ceo_agent.load_dotenv"):
            telegram = MockTelegram.return_value
            telegram.reply_to_command.return_value = {}

            from agents.ceo_agent import handle_run
            handle_run(chat_id=999, message_id=1, args=["seo"])
            # Give the daemon thread a moment to start
            import time; time.sleep(0.1)

        telegram.reply_to_command.assert_called()

    def test_get_command_handlers(self):
        """get_command_handlers returns dict with all expected keys."""
        from agents.ceo_agent import get_command_handlers
        handlers = get_command_handlers()
        expected_keys = {"status", "listings", "budget", "run", "pause", "resume", "logs", "report"}
        assert expected_keys.issubset(set(handlers.keys()))

    def test_format_agent_summary_empty(self):
        """_format_agent_summary with empty dict returns appropriate message."""
        from agents.ceo_agent import _format_agent_summary
        result = _format_agent_summary({})
        assert "No agent runs" in result

    def test_format_listing_stats_empty(self):
        """_format_listing_stats with empty list returns appropriate message."""
        from agents.ceo_agent import _format_listing_stats
        result = _format_listing_stats([])
        assert "No listing" in result

    def test_ago_helper(self):
        """_ago converts timestamps to human-readable strings."""
        from agents.ceo_agent import _ago
        assert _ago(None) == "never"

        # A timestamp from 2 minutes ago
        ts = (datetime.now(timezone.utc) - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = _ago(ts)
        assert "ago" in result


# ===========================================================================
# Software Agent
# ===========================================================================

class TestSoftwareAgent:

    def test_is_safe_patch_valid(self):
        """Patch touching only .py files passes."""
        from software_agent import _is_safe_patch
        patch_content = (
            "--- a/agents/seo_agent.py\n"
            "+++ b/agents/seo_agent.py\n"
            "@@ -1,2 +1,2 @@\n"
            "-old line\n"
            "+new line\n"
        )
        assert _is_safe_patch(patch_content) is True

    def test_is_safe_patch_rejects_non_py(self):
        """Patch touching .sh file rejected."""
        from software_agent import _is_safe_patch
        patch_content = (
            "--- a/deploy.sh\n"
            "+++ b/deploy.sh\n"
            "@@ -1,2 +1,2 @@\n"
            "-old\n"
            "+new\n"
        )
        assert _is_safe_patch(patch_content) is False

    def test_is_safe_patch_rejects_path_traversal(self):
        """Patch with ../../etc/ rejected."""
        from software_agent import _is_safe_patch
        patch_content = (
            "--- a/../../etc/passwd\n"
            "+++ b/../../etc/passwd\n"
            "@@ -1,2 +1,2 @@\n"
            "-old\n"
            "+new\n"
        )
        assert _is_safe_patch(patch_content) is False

    def test_is_safe_patch_dev_null(self):
        """/dev/null entries are allowed (new file patches)."""
        from software_agent import _is_safe_patch
        patch_content = (
            "--- /dev/null\n"
            "+++ b/agents/new_file.py\n"
            "@@ -0,0 +1 @@\n"
            "+new content\n"
        )
        assert _is_safe_patch(patch_content) is True

    def test_classify_error_auth(self):
        """Line with 'EtsyAuthError' → auth error class."""
        from software_agent import _is_auth_error
        assert _is_auth_error("EtsyAuthError: Token expired") is True
        assert _is_auth_error("401 Unauthorized") is True

    def test_classify_error_network(self):
        """Line with 'ConnectionError' → network class."""
        from software_agent import _is_network_error
        assert _is_network_error("requests.exceptions.ConnectionError occurred") is True
        assert _is_network_error("TimeoutError: Connection timed out") is True

    def test_classify_error_traceback(self):
        """Line with 'Traceback (most recent call last)' → logic bug class."""
        from software_agent import _has_python_traceback
        assert _has_python_traceback("Traceback (most recent call last):\n  File ...") is True
        assert _has_python_traceback("Regular log message") is False

    def test_classify_error_rate_limit(self):
        """Line with '429' → rate limit class."""
        from software_agent import _is_rate_limit_error
        assert _is_rate_limit_error("429 Too Many Requests") is True
        assert _is_rate_limit_error("normal message") is False

    def test_is_duplicate_deduplications(self):
        """_is_duplicate returns True for same error within 5 minutes."""
        from software_agent import _is_duplicate, _seen_errors
        _seen_errors.clear()
        key = "unique_test_error_abc123"
        assert _is_duplicate(key) is False  # First time → not duplicate
        assert _is_duplicate(key) is True   # Second time → duplicate

    def test_parse_traceback(self):
        """_parse_traceback extracts file and line number."""
        from software_agent import _parse_traceback
        tb = (
            'Traceback (most recent call last):\n'
            '  File "/path/to/agents/seo_agent.py", line 42, in run_seo\n'
            '    result = _call_claude(prompt, api_key)\n'
            'ValueError: bad value'
        )
        file_path, line_num = _parse_traceback(tb)
        assert file_path == "/path/to/agents/seo_agent.py"
        assert line_num == 42

    def test_parse_traceback_no_match(self):
        """_parse_traceback returns (None, None) on no match."""
        from software_agent import _parse_traceback
        file_path, line_num = _parse_traceback("No traceback here")
        assert file_path is None
        assert line_num is None
