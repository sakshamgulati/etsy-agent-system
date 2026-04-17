"""
test_extra_coverage.py — Additional tests to push coverage above 80%.

Focuses on: ceo_agent, software_agent, finance_agent, marketing_agent,
            analytics_agent, seo_agent internal helpers.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

import db.database as db_module
from db.database import get_conn, insert_listing_stat, insert_order, insert_ad_spend


# ===========================================================================
# CEO Agent — internal helpers and handlers
# ===========================================================================

class TestCEOAgentInternals:

    def test_get_weekly_revenue(self, tmp_db, mock_env):
        """_get_weekly_revenue sums orders from last 7 days."""
        conn = get_conn()
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            """INSERT INTO orders (receipt_id, listing_id, amount_paid, currency, status, created_at, recorded_at)
               VALUES ('R1', 'L1', 50.0, 'USD', 'paid', ?, ?)""",
            (now_str, now_str),
        )
        conn.execute(
            """INSERT INTO orders (receipt_id, listing_id, amount_paid, currency, status, created_at, recorded_at)
               VALUES ('R2', 'L1', 30.0, 'USD', 'paid', ?, ?)""",
            (now_str, now_str),
        )
        conn.commit()
        conn.close()

        from agents.ceo_agent import _get_weekly_revenue
        revenue = _get_weekly_revenue()
        assert abs(revenue - 80.0) < 0.01

    def test_get_weekly_ad_spend(self, tmp_db, mock_env):
        """_get_weekly_ad_spend returns total and by_listing breakdown."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        insert_ad_spend("L001", today, 15.0, 100, 5000, 3, 2.0)
        insert_ad_spend("L002", today, 10.0, 50, 2000, 1, 1.5)

        from agents.ceo_agent import _get_weekly_ad_spend
        result = _get_weekly_ad_spend()
        assert abs(result["total"] - 25.0) < 0.01
        assert len(result["by_listing"]) == 2

    def test_format_agent_summary_with_errors(self):
        """_format_agent_summary formats correctly when errors > 0."""
        from agents.ceo_agent import _format_agent_summary
        summary = {
            "analytics": {"runs": 5, "errors": 2, "last_run": "2026-01-01T00:00:00Z"},
        }
        result = _format_agent_summary(summary)
        assert "analytics" in result
        assert "2 errors" in result

    def test_format_listing_stats_with_data(self, tmp_db, mock_env):
        """_format_listing_stats returns listing info."""
        from agents.ceo_agent import _format_listing_stats
        from db.database import get_latest_listing_stats

        conn = get_conn()
        conn.execute(
            """INSERT INTO listing_stats (listing_id, title, price, views, favorites, quantity, state, snapshot_at)
               VALUES ('L001', 'Pretty Print', 25.0, 100, 10, 999, 'active', '2026-01-01T00:00:00Z')"""
        )
        conn.commit()
        conn.close()

        stats = get_latest_listing_stats()
        result = _format_listing_stats(stats)
        assert "L001" in result
        assert "Pretty Print" in result

    def test_format_pending_approvals(self, tmp_db, mock_env):
        """_format_pending_approvals shows pending items."""
        from agents.ceo_agent import _format_pending_approvals
        from db.database import create_pending_approval, get_pending_approvals

        create_pending_approval("finance", "price_change", {"listing_id": "L1"})
        approvals = get_pending_approvals()

        result = _format_pending_approvals(approvals)
        assert "finance" in result
        assert "price_change" in result

    def test_format_pending_approvals_empty(self):
        """_format_pending_approvals with empty list."""
        from agents.ceo_agent import _format_pending_approvals
        result = _format_pending_approvals([])
        assert "None pending" in result

    def test_top_listing(self, tmp_db, mock_env):
        """_top_listing returns listing with most views."""
        from agents.ceo_agent import _top_listing
        from db.database import get_latest_listing_stats

        conn = get_conn()
        conn.executemany(
            """INSERT INTO listing_stats (listing_id, title, price, views, favorites, quantity, state, snapshot_at)
               VALUES (?, ?, 25.0, ?, 5, 999, 'active', '2026-01-01T00:00:00Z')""",
            [("L001", "High Views", 500), ("L002", "Low Views", 10)],
        )
        conn.commit()
        conn.close()

        stats = get_latest_listing_stats()
        result = _top_listing(stats)
        assert "L001" in result or "High Views" in result

    def test_top_listing_empty(self):
        """_top_listing with empty stats returns N/A."""
        from agents.ceo_agent import _top_listing
        result = _top_listing([])
        assert result == "N/A"

    def test_ago_seconds(self):
        """_ago for < 120 seconds shows seconds."""
        from agents.ceo_agent import _ago
        ts = (datetime.now(timezone.utc) - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = _ago(ts)
        assert "s ago" in result

    def test_ago_hours(self):
        """_ago for several hours shows hours."""
        from agents.ceo_agent import _ago
        ts = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = _ago(ts)
        assert "h ago" in result

    def test_ago_days(self):
        """_ago for multiple days shows days."""
        from agents.ceo_agent import _ago
        ts = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = _ago(ts)
        assert "d ago" in result

    def test_handle_status(self, tmp_db, mock_env):
        """handle_status replies with system status."""
        with patch("agents.ceo_agent.TelegramClient") as MockTelegram, \
             patch("agents.ceo_agent.load_dotenv"):
            telegram = MockTelegram.return_value
            telegram.reply_to_command.return_value = {}

            from agents.ceo_agent import handle_status
            handle_status(999, 1, [])

        telegram.reply_to_command.assert_called_once()
        reply_text = telegram.reply_to_command.call_args[0][2]
        assert "Status" in reply_text

    def test_handle_listings(self, tmp_db, mock_env):
        """handle_listings replies with listing data."""
        conn = get_conn()
        conn.execute(
            """INSERT INTO listing_stats (listing_id, title, price, views, favorites, quantity, state, snapshot_at)
               VALUES ('L001', 'Test Print', 25.0, 100, 10, 999, 'active', '2026-01-01T00:00:00Z')"""
        )
        conn.commit()
        conn.close()

        with patch("agents.ceo_agent.TelegramClient") as MockTelegram, \
             patch("agents.ceo_agent.load_dotenv"):
            telegram = MockTelegram.return_value
            telegram.reply_to_command.return_value = {}

            from agents.ceo_agent import handle_listings
            handle_listings(999, 1, [])

        telegram.reply_to_command.assert_called()

    def test_handle_listings_empty(self, tmp_db, mock_env):
        """handle_listings replies 'No listing data yet' when empty."""
        with patch("agents.ceo_agent.TelegramClient") as MockTelegram, \
             patch("agents.ceo_agent.load_dotenv"):
            telegram = MockTelegram.return_value
            telegram.reply_to_command.return_value = {}

            from agents.ceo_agent import handle_listings
            handle_listings(999, 1, [])

        reply_text = telegram.reply_to_command.call_args[0][2]
        assert "No listing" in reply_text

    def test_handle_budget(self, tmp_db, mock_env):
        """handle_budget replies with budget info."""
        with patch("agents.ceo_agent.TelegramClient") as MockTelegram, \
             patch("agents.ceo_agent.load_dotenv"):
            telegram = MockTelegram.return_value
            telegram.reply_to_command.return_value = {}

            from agents.ceo_agent import handle_budget
            handle_budget(999, 1, [])

        telegram.reply_to_command.assert_called_once()
        reply_text = telegram.reply_to_command.call_args[0][2]
        assert "Budget" in reply_text

    def test_handle_pause_no_args(self, tmp_db, mock_env, tmp_path, monkeypatch):
        """handle_pause with no args sends usage message."""
        from agents import ceo_agent
        monkeypatch.setattr(ceo_agent, "_LOGS_DIR", tmp_path)

        with patch("agents.ceo_agent.TelegramClient") as MockTelegram, \
             patch("agents.ceo_agent.load_dotenv"):
            telegram = MockTelegram.return_value
            telegram.reply_to_command.return_value = {}

            from agents.ceo_agent import handle_pause
            handle_pause(999, 1, [])

        reply_text = telegram.reply_to_command.call_args[0][2]
        assert "Usage" in reply_text

    def test_handle_resume_not_paused(self, tmp_db, mock_env, tmp_path, monkeypatch):
        """handle_resume when agent was not paused sends info message."""
        from agents import ceo_agent
        monkeypatch.setattr(ceo_agent, "_LOGS_DIR", tmp_path)

        with patch("agents.ceo_agent.TelegramClient") as MockTelegram, \
             patch("agents.ceo_agent.load_dotenv"):
            telegram = MockTelegram.return_value
            telegram.reply_to_command.return_value = {}

            from agents.ceo_agent import handle_resume
            handle_resume(999, 1, ["seo"])  # seo.paused doesn't exist

        reply_text = telegram.reply_to_command.call_args[0][2]
        assert "not paused" in reply_text

    def test_handle_resume_no_args(self, tmp_db, mock_env, tmp_path, monkeypatch):
        """handle_resume with no args sends usage message."""
        from agents import ceo_agent
        monkeypatch.setattr(ceo_agent, "_LOGS_DIR", tmp_path)

        with patch("agents.ceo_agent.TelegramClient") as MockTelegram, \
             patch("agents.ceo_agent.load_dotenv"):
            telegram = MockTelegram.return_value
            telegram.reply_to_command.return_value = {}

            from agents.ceo_agent import handle_resume
            handle_resume(999, 1, [])

        reply_text = telegram.reply_to_command.call_args[0][2]
        assert "Usage" in reply_text

    def test_handle_run_no_args(self, tmp_db, mock_env):
        """handle_run with no args sends usage message."""
        with patch("agents.ceo_agent.TelegramClient") as MockTelegram, \
             patch("agents.ceo_agent.load_dotenv"):
            telegram = MockTelegram.return_value
            telegram.reply_to_command.return_value = {}

            from agents.ceo_agent import handle_run
            handle_run(999, 1, [])

        reply_text = telegram.reply_to_command.call_args[0][2]
        assert "Usage" in reply_text

    def test_handle_run_invalid_agent(self, tmp_db, mock_env):
        """handle_run with invalid agent name replies with error."""
        with patch("agents.ceo_agent.TelegramClient") as MockTelegram, \
             patch("agents.ceo_agent.load_dotenv"):
            telegram = MockTelegram.return_value
            telegram.reply_to_command.return_value = {}

            from agents.ceo_agent import handle_run
            handle_run(999, 1, ["hacker"])

        reply_text = telegram.reply_to_command.call_args[0][2]
        assert "Unknown agent" in reply_text

    def test_handle_logs_no_file(self, tmp_db, mock_env, tmp_path, monkeypatch):
        """handle_logs when no errors.log file returns info message."""
        from agents import ceo_agent
        monkeypatch.setattr(ceo_agent, "_LOGS_DIR", tmp_path)

        with patch("agents.ceo_agent.TelegramClient") as MockTelegram, \
             patch("agents.ceo_agent.load_dotenv"):
            telegram = MockTelegram.return_value
            telegram.reply_to_command.return_value = {}

            from agents.ceo_agent import handle_logs
            handle_logs(999, 1, [])

        reply_text = telegram.reply_to_command.call_args[0][2]
        assert "No errors.log" in reply_text

    def test_handle_logs_with_file(self, tmp_db, mock_env, tmp_path, monkeypatch):
        """handle_logs with existing errors.log returns last lines."""
        from agents import ceo_agent
        monkeypatch.setattr(ceo_agent, "_LOGS_DIR", tmp_path)

        errors_log = tmp_path / "errors.log"
        errors_log.write_text("\n".join(f"Error line {i}" for i in range(20)))

        with patch("agents.ceo_agent.TelegramClient") as MockTelegram, \
             patch("agents.ceo_agent.load_dotenv"):
            telegram = MockTelegram.return_value
            telegram.reply_to_command.return_value = {}

            from agents.ceo_agent import handle_logs
            handle_logs(999, 1, [])

        telegram.reply_to_command.assert_called_once()

    def test_handle_resume_invalid_agent(self, tmp_db, mock_env, tmp_path, monkeypatch):
        """handle_resume with invalid agent name replies with error."""
        from agents import ceo_agent
        monkeypatch.setattr(ceo_agent, "_LOGS_DIR", tmp_path)

        with patch("agents.ceo_agent.TelegramClient") as MockTelegram, \
             patch("agents.ceo_agent.load_dotenv"):
            telegram = MockTelegram.return_value
            telegram.reply_to_command.return_value = {}

            from agents.ceo_agent import handle_resume
            handle_resume(999, 1, ["badagent"])

        reply_text = telegram.reply_to_command.call_args[0][2]
        assert "Unknown agent" in reply_text or "❌" in reply_text


# ===========================================================================
# Finance Agent — internal helpers
# ===========================================================================

class TestFinanceAgentInternals:

    def test_build_listings_data_empty(self, tmp_db, mock_env):
        """_build_listings_data returns empty list when no listing stats."""
        from agents.finance_agent import _build_listings_data
        conn = get_conn()
        result = _build_listings_data(conn, print_cost=8.0, shipping_cost=5.0)
        conn.close()
        assert result == []

    def test_build_listings_data_with_orders(self, tmp_db, mock_env):
        """_build_listings_data computes financials correctly."""
        from agents.finance_agent import _build_listings_data

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = get_conn()
        conn.execute(
            """INSERT INTO listing_stats (listing_id, title, price, views, favorites, quantity, state, snapshot_at)
               VALUES ('L001', 'Profit Print', 30.0, 200, 15, 999, 'active', ?)""",
            (now_str,),
        )
        # Two orders within last 30 days
        for rid in ("R1", "R2"):
            conn.execute(
                """INSERT INTO orders (receipt_id, listing_id, amount_paid, currency, status, created_at, recorded_at)
                   VALUES (?, 'L001', 30.0, 'USD', 'paid', ?, ?)""",
                (rid, now_str, now_str),
            )
        conn.commit()

        result = _build_listings_data(conn, print_cost=8.0, shipping_cost=5.0)
        conn.close()

        assert len(result) == 1
        item = result[0]
        assert item["listing_id"] == "L001"
        assert item["orders_count"] == 2
        assert abs(item["revenue"] - 60.0) < 0.01
        assert abs(item["cogs"] - 26.0) < 0.01  # 2 * (8+5)
        assert abs(item["profit"] - 34.0) < 0.01

    def test_optimize_ads_unavailable(self, tmp_db, mock_env):
        """_optimize_ads handles 404 gracefully."""
        from agents.finance_agent import _optimize_ads
        from integrations.etsy_client import EtsyAPIError

        mock_client = MagicMock()
        mock_client.shop_id = "TestShop"
        mock_client._request.side_effect = EtsyAPIError("Not found", 404, "Not found")

        conn = get_conn()
        # Should not raise
        _optimize_ads(mock_client, conn)
        conn.close()

    def test_optimize_ads_with_spend_data(self, tmp_db, mock_env):
        """_optimize_ads inserts ad spend when data available."""
        from agents.finance_agent import _optimize_ads

        mock_client = MagicMock()
        mock_client.shop_id = "TestShop"
        mock_client._request.return_value = {
            "results": [
                {"entry_type": "promoted_listing", "reference_id": "L001", "amount": 500},
            ]
        }

        conn = get_conn()
        _optimize_ads(mock_client, conn)
        conn.close()

        # Verify ad spend was recorded
        result_conn = get_conn()
        rows = result_conn.execute("SELECT * FROM ad_spend").fetchall()
        result_conn.close()
        assert len(rows) >= 1


# ===========================================================================
# SEO Agent — internal helpers
# ===========================================================================

class TestSEOAgentInternals:

    def test_format_prompt(self):
        """_format_prompt fills template placeholders correctly."""
        from agents.seo_agent import _format_prompt
        template = "ID:{listing_id} Title:{title} Tags:{tags} Desc:{description}"
        listing = {
            "listing_id": 1001,
            "title": "Pretty Print",
            "tags": ["art", "print"],
            "description": "Nice artwork",
        }
        result = _format_prompt(template, listing)
        assert "1001" in result
        assert "Pretty Print" in result
        assert "art" in result
        assert "Nice artwork" in result

    def test_call_claude_strips_code_fences(self):
        """_call_claude strips markdown fences from response."""
        from agents.seo_agent import _call_claude

        raw_json = json.dumps({
            "title": "Better Title",
            "tags": ["t"] * 13,
            "description": "Good desc",
            "rationale": "SEO",
        })
        fenced_response = f"```json\n{raw_json}\n```"

        mock_msg = MagicMock()
        mock_msg.content = [MagicMock(text=fenced_response)]

        with patch("agents.seo_agent.anthropic.Anthropic") as MockAnthropic:
            MockAnthropic.return_value.messages.create.return_value = mock_msg
            result = _call_claude("test prompt", "fake_key")

        assert result["title"] == "Better Title"

    def test_process_listing_missing_keys(self, tmp_db, mock_env):
        """_process_listing returns error when Claude response missing keys."""
        from agents.seo_agent import _process_listing
        from integrations.etsy_client import EtsyClient

        claude_resp = MagicMock()
        claude_resp.content = [MagicMock(text=json.dumps({"title": "Only title"}))]

        listing = {"listing_id": 1001, "title": "Test", "tags": [], "description": ""}
        template = "ID:{listing_id} Title:{title} Tags:{tags} Desc:{description}"

        with patch("agents.seo_agent.anthropic.Anthropic") as MockAnthropic:
            MockAnthropic.return_value.messages.create.return_value = claude_resp
            mock_etsy = MagicMock(spec=EtsyClient)

            result = _process_listing(listing, template, mock_etsy, "fake_key", dry_run=True)

        assert result["status"] == "error"
        assert "missing keys" in result["error"].lower() or result["changes"] == 0

    def test_process_listing_wrong_tag_count(self, tmp_db, mock_env):
        """_process_listing returns error when tag count != 13."""
        from agents.seo_agent import _process_listing
        from integrations.etsy_client import EtsyClient

        bad_tags_resp = json.dumps({
            "title": "Good Title",
            "tags": ["tag1", "tag2"],  # only 2, not 13
            "description": "Good desc",
            "rationale": "Better",
        })
        claude_resp = MagicMock()
        claude_resp.content = [MagicMock(text=bad_tags_resp)]

        listing = {"listing_id": 1001, "title": "Test", "tags": [], "description": ""}
        template = "ID:{listing_id} Title:{title} Tags:{tags} Desc:{description}"

        with patch("agents.seo_agent.anthropic.Anthropic") as MockAnthropic:
            MockAnthropic.return_value.messages.create.return_value = claude_resp
            mock_etsy = MagicMock(spec=EtsyClient)

            result = _process_listing(listing, template, mock_etsy, "fake_key", dry_run=True)

        assert result["status"] == "error"
        assert result["changes"] == 0

    def test_process_listing_no_changes(self, tmp_db, mock_env):
        """_process_listing returns ok with 0 changes when nothing changed."""
        from agents.seo_agent import _process_listing
        from integrations.etsy_client import EtsyClient

        tags_13 = ["tag1", "tag2", "tag3", "tag4", "tag5", "tag6", "tag7",
                   "tag8", "tag9", "tag10", "tag11", "tag12", "tag13"]
        listing = {
            "listing_id": 1001,
            "title": "Exact Same Title",
            "tags": tags_13,
            "description": "Exact same description",
        }
        same_resp = json.dumps({
            "title": "Exact Same Title",
            "tags": tags_13,
            "description": "Exact same description",
            "rationale": "Already optimal",
        })
        claude_resp = MagicMock()
        claude_resp.content = [MagicMock(text=same_resp)]

        template = "ID:{listing_id} Title:{title} Tags:{tags} Desc:{description}"

        with patch("agents.seo_agent.anthropic.Anthropic") as MockAnthropic:
            MockAnthropic.return_value.messages.create.return_value = claude_resp
            mock_etsy = MagicMock(spec=EtsyClient)

            result = _process_listing(listing, template, mock_etsy, "fake_key", dry_run=True)

        assert result["status"] == "ok"
        assert result["changes"] == 0


# ===========================================================================
# Analytics Agent — additional coverage
# ===========================================================================

class TestAnalyticsAgentExtra:

    def test_run_analytics_handles_empty_listings(self, tmp_db, mock_env):
        """run_analytics works when no listings returned."""
        with patch("agents.analytics_agent.EtsyClient") as MockEtsy, \
             patch("agents.analytics_agent.load_dotenv"), \
             patch("agents.analytics_agent.init_db"):
            client = MockEtsy.return_value
            client.get_listings.return_value = []
            client.get_orders.return_value = []
            client.get_shop_stats.return_value = {"results": []}

            from agents.analytics_agent import run_analytics
            result = run_analytics()

        assert result["listings_updated"] == 0
        assert result["orders_recorded"] == 0
        assert result["shop_stat_recorded"] is False

    def test_run_analytics_handles_listing_with_flat_price(self, tmp_db, mock_env):
        """run_analytics handles listings where price is a flat float."""
        mock_listings = [{
            "listing_id": 2001,
            "title": "Flat Price Print",
            "price": 25.0,  # flat float, not nested dict
            "views": 50,
            "num_favorers": 3,
            "quantity": 10,
            "state": "active",
        }]

        with patch("agents.analytics_agent.EtsyClient") as MockEtsy, \
             patch("agents.analytics_agent.load_dotenv"), \
             patch("agents.analytics_agent.init_db"):
            client = MockEtsy.return_value
            client.get_listings.return_value = mock_listings
            client.get_orders.return_value = []
            client.get_shop_stats.return_value = {"results": []}

            from agents.analytics_agent import run_analytics
            result = run_analytics()

        assert result["listings_updated"] == 1

    def test_run_analytics_handles_order_with_subtotal(self, tmp_db, mock_env):
        """run_analytics handles orders with subtotal fallback."""
        mock_orders = [{
            "receipt_id": "R_SUB",
            "transactions": [],
            "grandtotal": None,  # no grandtotal → use subtotal
            "subtotal": {"amount": 2000, "divisor": 100},
            "currency_code": "USD",
            "status": "paid",
            "create_timestamp": 0,
        }]

        with patch("agents.analytics_agent.EtsyClient") as MockEtsy, \
             patch("agents.analytics_agent.load_dotenv"), \
             patch("agents.analytics_agent.init_db"):
            client = MockEtsy.return_value
            client.get_listings.return_value = []
            client.get_orders.return_value = mock_orders
            client.get_shop_stats.return_value = {"results": []}

            from agents.analytics_agent import run_analytics
            result = run_analytics()

        assert result["orders_recorded"] == 1

    def test_run_analytics_handles_shop_stats_flat_revenue(self, tmp_db, mock_env):
        """run_analytics handles shop stats where revenue is a flat number."""
        mock_shop_stats = {
            "results": [{
                "date": "2026-01-01",
                "visits": 100,
                "pageviews": 500,
                "orders": 5,
                "revenue": 125.0,  # flat float
            }]
        }

        with patch("agents.analytics_agent.EtsyClient") as MockEtsy, \
             patch("agents.analytics_agent.load_dotenv"), \
             patch("agents.analytics_agent.init_db"):
            client = MockEtsy.return_value
            client.get_listings.return_value = []
            client.get_orders.return_value = []
            client.get_shop_stats.return_value = mock_shop_stats

            from agents.analytics_agent import run_analytics
            result = run_analytics()

        assert result["shop_stat_recorded"] is True


# ===========================================================================
# Software Agent — additional helpers
# ===========================================================================

class TestSoftwareAgentExtra:

    def test_read_source_context(self, tmp_path):
        """_read_source_context returns lines around the target line."""
        from software_agent import _read_source_context

        source_file = tmp_path / "test_source.py"
        lines = [f"line_{i}" for i in range(50)]
        source_file.write_text("\n".join(lines))

        result = _read_source_context(str(source_file), 25, context=5)
        assert "line_24" in result
        assert ">>>" in result  # marker for target line

    def test_read_source_context_missing_file(self):
        """_read_source_context handles missing file gracefully."""
        from software_agent import _read_source_context
        result = _read_source_context("/nonexistent/path/file.py", 10)
        assert "could not read file" in result.lower()

    def test_utcnow_returns_datetime(self):
        """_utcnow returns a UTC datetime."""
        from software_agent import _utcnow
        result = _utcnow()
        assert result.tzinfo is not None

    def test_is_duplicate_clears_stale(self):
        """_is_duplicate prunes entries older than 2x dedup window."""
        from software_agent import _seen_errors, _is_duplicate
        from datetime import timedelta

        _seen_errors.clear()
        stale_key = "old_stale_error_xyz"
        _seen_errors[stale_key] = datetime.now(timezone.utc) - timedelta(minutes=20)

        # Triggering any _is_duplicate call should prune stale entries
        _is_duplicate("new_fresh_error_abc")
        assert stale_key not in _seen_errors

    def test_handle_auth_error(self, mock_env, requests_mock):
        """_handle_auth_error refreshes token via EtsyClient."""
        from software_agent import _handle_auth_error

        requests_mock.post(
            "https://api.etsy.com/v3/public/oauth/token",
            json={"access_token": "new", "refresh_token": "new_refresh"},
        )

        with patch("integrations.etsy_client.load_dotenv"), \
             patch("integrations.etsy_client.set_key"):
            # Should not raise
            _handle_auth_error({"action": "EtsyAuthError test"})

    def test_handle_rate_limit(self):
        """_handle_rate_limit doesn't raise."""
        from software_agent import _handle_rate_limit
        _handle_rate_limit({"action": "429 Too Many Requests"})

    def test_handle_network_error(self):
        """_handle_network_error doesn't raise."""
        from software_agent import _handle_network_error
        _handle_network_error({"action": "ConnectionError occurred"})


# ===========================================================================
# Marketing Agent — pick_listing rotation
# ===========================================================================

class TestMarketingAgentRotation:

    def test_pick_listing_no_posts(self, tmp_db, mock_env):
        """When no posts exist, first listing is returned."""
        from agents.marketing_agent import _pick_listing_to_feature

        listings = [
            {"listing_id": 100, "title": "A"},
            {"listing_id": 200, "title": "B"},
        ]
        result = _pick_listing_to_feature(listings)
        # Both are never-posted, so first one in sorted order is returned
        assert result["listing_id"] in (100, 200)

    def test_pick_listing_selects_oldest_posted(self, tmp_db, mock_env):
        """Listing with oldest last post is selected."""
        from agents.marketing_agent import _pick_listing_to_feature
        from db.database import insert_social_post

        insert_social_post("100", "pinterest", "P1", "cap", "utm")
        import time; time.sleep(0.01)
        insert_social_post("200", "pinterest", "P2", "cap", "utm")

        listings = [
            {"listing_id": 100, "title": "A"},
            {"listing_id": 200, "title": "B"},
        ]
        result = _pick_listing_to_feature(listings)
        # 100 was posted earlier, so it should be selected
        assert result["listing_id"] == 100

    def test_pick_listing_empty_raises(self, tmp_db, mock_env):
        """Empty listings list raises ValueError."""
        from agents.marketing_agent import _pick_listing_to_feature

        with pytest.raises(ValueError, match="No active listings"):
            _pick_listing_to_feature([])

    def test_generate_content_strips_fences(self, mock_env):
        """_generate_content strips markdown code fences from Claude response."""
        content = {
            "pinterest_title": "Beautiful Art",
            "pinterest_description": "Great for your home.",
            "instagram_caption": "Check this out! #art",
        }
        fenced = f"```json\n{json.dumps(content)}\n```"
        claude_resp = MagicMock()
        claude_resp.content = [MagicMock(text=fenced)]

        listing = {
            "listing_id": 1001,
            "title": "Art Print",
            "description": "Nice art.",
            "tags": ["art"],
            "price": {"amount": 2500, "divisor": 100},
        }

        with patch("agents.marketing_agent.anthropic.Anthropic") as MockAnthropic:
            MockAnthropic.return_value.messages.create.return_value = claude_resp
            from agents.marketing_agent import _generate_content
            result = _generate_content(listing)

        assert result["pinterest_title"] == "Beautiful Art"

    def test_generate_content_invalid_json_raises(self, mock_env):
        """_generate_content raises ValueError on invalid JSON."""
        claude_resp = MagicMock()
        claude_resp.content = [MagicMock(text="NOT JSON AT ALL")]

        listing = {
            "listing_id": 1001,
            "title": "Art Print",
            "description": "Nice art.",
            "tags": [],
            "price": 25.0,
        }

        with patch("agents.marketing_agent.anthropic.Anthropic") as MockAnthropic:
            MockAnthropic.return_value.messages.create.return_value = claude_resp
            from agents.marketing_agent import _generate_content
            with pytest.raises(ValueError, match="invalid JSON"):
                _generate_content(listing)


# ===========================================================================
# Software Agent — expanded coverage
# ===========================================================================

class TestSoftwareAgentClassifiers:
    """Test error classification helpers in software_agent."""

    def test_classify_auth_error(self):
        """Line containing 'EtsyAuthError' is classified as auth."""
        from software_agent import _is_auth_error
        assert _is_auth_error("EtsyAuthError: token expired") is True

    def test_classify_auth_error_401(self):
        """Line containing '401 Unauthorized' is auth."""
        from software_agent import _is_auth_error
        assert _is_auth_error("401 Unauthorized") is True

    def test_classify_not_auth_error(self):
        """Random line is NOT auth error."""
        from software_agent import _is_auth_error
        assert _is_auth_error("ConnectionError: timeout") is False

    def test_classify_rate_limit(self):
        """Line containing '429' is rate_limit."""
        from software_agent import _is_rate_limit_error
        assert _is_rate_limit_error("HTTP 429 Too Many Requests") is True

    def test_classify_not_rate_limit(self):
        """Random line is NOT rate limit."""
        from software_agent import _is_rate_limit_error
        assert _is_rate_limit_error("ConnectionError occurred") is False

    def test_classify_network_error_connection(self):
        """Line containing 'ConnectionError' is network error."""
        from software_agent import _is_network_error
        assert _is_network_error("ConnectionError: failed to connect") is True

    def test_classify_network_error_timeout(self):
        """Line containing 'TimeoutError' is network error."""
        from software_agent import _is_network_error
        assert _is_network_error("TimeoutError: read timed out") is True

    def test_classify_not_network_error(self):
        """Random error line is NOT network error."""
        from software_agent import _is_network_error
        assert _is_network_error("ValueError: invalid literal") is False

    def test_classify_traceback(self):
        """Line with 'Traceback (most recent call last)' is classified as traceback."""
        from software_agent import _has_python_traceback
        assert _has_python_traceback("Traceback (most recent call last):") is True

    def test_classify_no_traceback(self):
        """Random error line without traceback marker is not traceback."""
        from software_agent import _has_python_traceback
        assert _has_python_traceback("Some random error") is False


class TestSoftwareAgentPatchSafety:
    """Test _is_safe_patch function in software_agent."""

    def test_is_safe_patch_valid_py_file(self):
        """Patch touching a .py file within the project is safe."""
        from software_agent import _is_safe_patch
        patch_content = (
            "--- agents/seo_agent.py\n"
            "+++ agents/seo_agent.py\n"
            "@@ -1,3 +1,3 @@\n"
            " def foo():\n"
            "-    pass\n"
            "+    return True\n"
        )
        assert _is_safe_patch(patch_content) is True

    def test_is_safe_patch_rejects_sh(self):
        """Patch touching a .sh file is rejected."""
        from software_agent import _is_safe_patch
        patch_content = (
            "--- deploy.sh\n"
            "+++ deploy.sh\n"
            "@@ -1 +1 @@\n"
            "-echo hello\n"
            "+rm -rf /\n"
        )
        assert _is_safe_patch(patch_content) is False

    def test_is_safe_patch_rejects_traversal(self):
        """Patch with path traversal (../../) is rejected."""
        from software_agent import _is_safe_patch
        patch_content = (
            "--- ../../etc/passwd\n"
            "+++ ../../etc/passwd\n"
            "@@ -1 +1 @@\n"
            "-root:x:0:0\n"
            "+hacked\n"
        )
        assert _is_safe_patch(patch_content) is False

    def test_is_safe_patch_dev_null(self):
        """Patch with /dev/null is safe (new file creation)."""
        from software_agent import _is_safe_patch
        patch_content = (
            "--- /dev/null\n"
            "+++ agents/new_agent.py\n"
            "@@ -0,0 +1,3 @@\n"
            "+def run():\n"
            "+    pass\n"
        )
        assert _is_safe_patch(patch_content) is True

    def test_is_safe_patch_empty(self):
        """Empty patch content is safe."""
        from software_agent import _is_safe_patch
        assert _is_safe_patch("") is True


class TestSoftwareAgentDedup:
    """Test deduplication logic."""

    def test_dedup_recent_errors_second_ignored(self):
        """Same error twice within 5 min — second call returns True (duplicate)."""
        from software_agent import _seen_errors, _is_duplicate
        _seen_errors.clear()
        error_key = "test_dedup_unique_error_12345"
        # First call — not a duplicate
        result1 = _is_duplicate(error_key)
        # Second call within 5 minutes — duplicate
        result2 = _is_duplicate(error_key)
        assert result1 is False
        assert result2 is True

    def test_dedup_different_errors_not_duplicates(self):
        """Different errors are not duplicates of each other."""
        from software_agent import _seen_errors, _is_duplicate
        _seen_errors.clear()
        assert _is_duplicate("error_alpha_unique") is False
        assert _is_duplicate("error_beta_unique") is False


class TestSoftwareAgentParseTraceback:
    """Test _parse_traceback function."""

    def test_parse_traceback_valid(self):
        """_parse_traceback extracts file and line from a real traceback."""
        from software_agent import _parse_traceback
        tb = (
            'Traceback (most recent call last):\n'
            '  File "/Users/user/etsy/agents/seo_agent.py", line 42, in run_seo\n'
            '    result = do_something()\n'
            '  File "/Users/user/etsy/agents/seo_agent.py", line 15, in do_something\n'
            '    raise ValueError("bad")\n'
            'ValueError: bad\n'
        )
        file_path, line_num = _parse_traceback(tb)
        assert file_path == "/Users/user/etsy/agents/seo_agent.py"
        assert line_num == 15

    def test_parse_traceback_no_match(self):
        """_parse_traceback returns (None, None) when no traceback pattern found."""
        from software_agent import _parse_traceback
        file_path, line_num = _parse_traceback("Just a random error string")
        assert file_path is None
        assert line_num is None


class TestSoftwareAgentHandlers:
    """Test error handler functions."""

    def test_handle_unknown_error_calls_telegram(self, mock_env):
        """_handle_unknown_error sends a Telegram alert."""
        from software_agent import _handle_unknown_error
        with patch("software_agent._get_telegram") as mock_get_tg:
            mock_tg = MagicMock()
            mock_get_tg.return_value = mock_tg
            log_entry = {"action": "Some weird error occurred", "agent": "analytics", "ts": "2026-01-01T00:00:00Z"}
            _handle_unknown_error(log_entry, "raw line")
        mock_tg.send_alert.assert_called_once()

    def test_handle_unknown_error_telegram_fails_gracefully(self, mock_env):
        """_handle_unknown_error doesn't raise when Telegram fails."""
        from software_agent import _handle_unknown_error
        with patch("software_agent._get_telegram") as mock_get_tg:
            mock_get_tg.return_value.send_alert.side_effect = Exception("Telegram down")
            # Should not raise
            _handle_unknown_error({"action": "error", "agent": "seo"}, "raw")

    def test_handle_rate_limit_logs_only(self):
        """_handle_rate_limit runs without error."""
        from software_agent import _handle_rate_limit
        _handle_rate_limit({"action": "429 error"})

    def test_handle_network_error_logs_only(self):
        """_handle_network_error runs without error."""
        from software_agent import _handle_network_error
        _handle_network_error({"action": "ConnectionError"})

    def test_handle_auth_error_refresh_called(self, mock_env):
        """_handle_auth_error calls EtsyClient._refresh_token."""
        from software_agent import _handle_auth_error
        with patch("integrations.etsy_client.EtsyClient") as MockEtsy:
            mock_client = MockEtsy.return_value
            _handle_auth_error({"action": "EtsyAuthError test"})
        mock_client._refresh_token.assert_called_once()

    def test_handle_auth_error_refresh_fails_sends_alert(self, mock_env):
        """_handle_auth_error sends alert when refresh fails."""
        from software_agent import _handle_auth_error
        with patch("integrations.etsy_client.EtsyClient") as MockEtsy, \
             patch("software_agent._get_telegram") as mock_get_tg:
            MockEtsy.return_value._refresh_token.side_effect = Exception("refresh failed")
            mock_tg = MagicMock()
            mock_get_tg.return_value = mock_tg
            _handle_auth_error({"action": "EtsyAuthError"})
        mock_tg.send_alert.assert_called_once()


class TestSoftwareAgentCheckPatches:
    """Test _check_and_apply_patches."""

    def test_check_and_apply_patches_no_flags(self, tmp_path, monkeypatch):
        """When no .flag files exist, nothing happens."""
        import software_agent
        monkeypatch.setattr(software_agent, "_PATCHES_DIR", tmp_path)
        # Should not raise
        software_agent._check_and_apply_patches()

    def test_check_and_apply_patches_valid_patch(self, tmp_path, monkeypatch, mock_env):
        """Valid .py patch + flag file applies the patch via subprocess."""
        import software_agent
        monkeypatch.setattr(software_agent, "_PATCHES_DIR", tmp_path)

        patch_id = "20260101_120000"
        patch_content = (
            "--- agents/seo_agent.py\n"
            "+++ agents/seo_agent.py\n"
            "@@ -1 +1 @@\n"
            "-pass\n"
            "+return True\n"
        )
        # Write patch file and flag
        (tmp_path / f"patch_{patch_id}.txt").write_text(patch_content)
        (tmp_path / f"apply_{patch_id}.flag").touch()

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = ""

        with patch("software_agent.subprocess.run", return_value=mock_result) as mock_run, \
             patch("software_agent._get_telegram") as mock_tg, \
             patch("software_agent._restart_affected_agent"):
            software_agent._check_and_apply_patches()

        mock_run.assert_called_once()
        # Flag file should be removed
        assert not (tmp_path / f"apply_{patch_id}.flag").exists()

    def test_check_and_apply_patches_unsafe_rejected(self, tmp_path, monkeypatch, mock_env):
        """Unsafe patch (non-.py file) is rejected without calling subprocess."""
        import software_agent
        monkeypatch.setattr(software_agent, "_PATCHES_DIR", tmp_path)

        patch_id = "20260101_999999"
        unsafe_patch = (
            "--- deploy.sh\n"
            "+++ deploy.sh\n"
            "@@ -1 +1 @@\n"
            "-echo hi\n"
            "+rm -rf /\n"
        )
        (tmp_path / f"patch_{patch_id}.txt").write_text(unsafe_patch)
        (tmp_path / f"apply_{patch_id}.flag").touch()

        with patch("software_agent.subprocess.run") as mock_run, \
             patch("software_agent._get_telegram"):
            software_agent._check_and_apply_patches()

        mock_run.assert_not_called()

    def test_check_and_apply_patches_missing_patch_file(self, tmp_path, monkeypatch, mock_env):
        """Flag file exists but patch file missing — cleans up flag."""
        import software_agent
        monkeypatch.setattr(software_agent, "_PATCHES_DIR", tmp_path)

        patch_id = "20260101_000001"
        (tmp_path / f"apply_{patch_id}.flag").touch()
        # No patch file created

        with patch("software_agent._get_telegram"):
            software_agent._check_and_apply_patches()

        # Flag should be removed
        assert not (tmp_path / f"apply_{patch_id}.flag").exists()

    def test_check_and_apply_patches_subprocess_fails(self, tmp_path, monkeypatch, mock_env):
        """Failed subprocess (non-zero returncode) sends error alert."""
        import software_agent
        monkeypatch.setattr(software_agent, "_PATCHES_DIR", tmp_path)

        patch_id = "20260101_000002"
        patch_content = (
            "--- agents/seo_agent.py\n"
            "+++ agents/seo_agent.py\n"
            "@@ -1 +1 @@\n"
            "-pass\n"
            "+return True\n"
        )
        (tmp_path / f"patch_{patch_id}.txt").write_text(patch_content)
        (tmp_path / f"apply_{patch_id}.flag").touch()

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "patch failed: already applied"

        with patch("software_agent.subprocess.run", return_value=mock_result), \
             patch("software_agent._get_telegram") as mock_get_tg:
            mock_tg = MagicMock()
            mock_get_tg.return_value = mock_tg
            software_agent._check_and_apply_patches()

        mock_tg.send_alert.assert_called_once()


class TestSoftwareAgentRestartAgent:
    """Test _restart_affected_agent."""

    def test_restart_affected_agent_writes_flag(self, tmp_path, monkeypatch, mock_env):
        """_restart_affected_agent writes a restart_requested.flag."""
        import software_agent
        monkeypatch.setattr(software_agent, "_PROJECT_ROOT", tmp_path)
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()

        meta = {
            "patch_id": "test123",
            "diagnosis": {"file_path": str(tmp_path / "agents" / "seo_agent.py")},
        }
        meta_file = tmp_path / "patch_test123.meta.json"
        meta_file.write_text(json.dumps(meta))

        with patch("software_agent._get_telegram") as mock_get_tg:
            mock_get_tg.return_value.send_alert = MagicMock()
            software_agent._restart_affected_agent(meta_file)

        restart_flag = logs_dir / "restart_requested.flag"
        assert restart_flag.exists()

    def test_restart_affected_agent_missing_meta(self, tmp_path):
        """_restart_affected_agent handles missing meta file gracefully."""
        import software_agent
        missing = tmp_path / "nonexistent.meta.json"
        # Should not raise
        software_agent._restart_affected_agent(missing)


class TestSoftwareAgentSaveLoadPrompt:
    """Test _save_patch and _load_prompt_template."""

    def test_save_patch_creates_files(self, tmp_path, monkeypatch):
        """_save_patch writes patch and meta files."""
        import software_agent
        monkeypatch.setattr(software_agent, "_PATCHES_DIR", tmp_path)

        diagnosis = {"diagnosis": "null ptr", "fix_description": "add check", "patch": "diff...", "confidence": "high"}
        patch_file = software_agent._save_patch("20260101_001", "diff content", "NullPointerError", diagnosis)

        assert patch_file.exists()
        meta_file = tmp_path / "patch_20260101_001.meta.json"
        assert meta_file.exists()
        meta = json.loads(meta_file.read_text())
        assert meta["patch_id"] == "20260101_001"


# ===========================================================================
# CEO Agent — extra coverage
# ===========================================================================

class TestCEOAgentExtra:
    """Additional tests for ceo_agent to push coverage above 85%."""

    def test_truncate_long_message(self):
        """_truncate clips messages over 4096 chars."""
        from agents.ceo_agent import _truncate
        long_msg = "x" * 5000
        result = _truncate(long_msg)
        assert len(result) <= 4096
        assert "truncated" in result

    def test_truncate_short_message(self):
        """_truncate leaves short messages untouched."""
        from agents.ceo_agent import _truncate
        short_msg = "Hello world"
        assert _truncate(short_msg) == short_msg

    def test_get_weekly_revenue_empty(self, tmp_db, mock_env):
        """_get_weekly_revenue returns 0.0 when no orders."""
        from agents.ceo_agent import _get_weekly_revenue
        result = _get_weekly_revenue()
        assert result == 0.0

    def test_handle_budget_no_spend(self, tmp_db, mock_env):
        """handle_budget with empty ad_spend table shows $0."""
        with patch("agents.ceo_agent.TelegramClient") as MockTelegram, \
             patch("agents.ceo_agent.load_dotenv"):
            telegram = MockTelegram.return_value
            telegram.reply_to_command.return_value = {}

            from agents.ceo_agent import handle_budget
            handle_budget(999, 1, [])

        reply_text = telegram.reply_to_command.call_args[0][2]
        assert "$0.00" in reply_text or "0.00" in reply_text

    def test_handle_logs_reads_errors_log(self, tmp_db, mock_env, tmp_path, monkeypatch):
        """handle_logs with existing file reads and sends content."""
        from agents import ceo_agent
        monkeypatch.setattr(ceo_agent, "_LOGS_DIR", tmp_path)

        errors_log = tmp_path / "errors.log"
        errors_log.write_text("\n".join(f"Error line {i}" for i in range(5)))

        with patch("agents.ceo_agent.TelegramClient") as MockTelegram, \
             patch("agents.ceo_agent.load_dotenv"):
            telegram = MockTelegram.return_value
            telegram.reply_to_command.return_value = {}

            from agents.ceo_agent import handle_logs
            handle_logs(999, 1, [])

        reply_text = telegram.reply_to_command.call_args[0][2]
        assert "Error line" in reply_text

    def test_handle_pause_valid_agent(self, tmp_db, mock_env, tmp_path, monkeypatch):
        """handle_pause with valid agent name creates pause flag."""
        from agents import ceo_agent
        monkeypatch.setattr(ceo_agent, "_LOGS_DIR", tmp_path)

        with patch("agents.ceo_agent.TelegramClient") as MockTelegram, \
             patch("agents.ceo_agent.load_dotenv"):
            telegram = MockTelegram.return_value
            telegram.reply_to_command.return_value = {}

            from agents.ceo_agent import handle_pause
            handle_pause(999, 1, ["analytics"])

        # Pause flag should exist
        assert (tmp_path / "analytics.paused").exists()
        reply_text = telegram.reply_to_command.call_args[0][2]
        assert "paused" in reply_text

    def test_handle_pause_invalid_agent(self, tmp_db, mock_env, tmp_path, monkeypatch):
        """handle_pause with invalid agent name sends error."""
        from agents import ceo_agent
        monkeypatch.setattr(ceo_agent, "_LOGS_DIR", tmp_path)

        with patch("agents.ceo_agent.TelegramClient") as MockTelegram, \
             patch("agents.ceo_agent.load_dotenv"):
            telegram = MockTelegram.return_value
            telegram.reply_to_command.return_value = {}

            from agents.ceo_agent import handle_pause
            handle_pause(999, 1, ["hacker"])

        reply_text = telegram.reply_to_command.call_args[0][2]
        assert "Unknown agent" in reply_text or "❌" in reply_text

    def test_handle_resume_existing_pause(self, tmp_db, mock_env, tmp_path, monkeypatch):
        """handle_resume removes existing pause flag."""
        from agents import ceo_agent
        monkeypatch.setattr(ceo_agent, "_LOGS_DIR", tmp_path)

        # Create the pause flag
        (tmp_path / "analytics.paused").touch()

        with patch("agents.ceo_agent.TelegramClient") as MockTelegram, \
             patch("agents.ceo_agent.load_dotenv"):
            telegram = MockTelegram.return_value
            telegram.reply_to_command.return_value = {}

            from agents.ceo_agent import handle_resume
            handle_resume(999, 1, ["analytics"])

        assert not (tmp_path / "analytics.paused").exists()
        reply_text = telegram.reply_to_command.call_args[0][2]
        assert "resumed" in reply_text

    def test_format_agent_summary_no_errors(self):
        """_format_agent_summary shows OK when no errors."""
        from agents.ceo_agent import _format_agent_summary
        summary = {
            "analytics": {"runs": 3, "errors": 0, "last_run": "2026-01-01T00:00:00Z"},
        }
        result = _format_agent_summary(summary)
        assert "OK" in result
        assert "analytics" in result

    def test_format_agent_summary_empty(self):
        """_format_agent_summary with empty dict returns no-data message."""
        from agents.ceo_agent import _format_agent_summary
        result = _format_agent_summary({})
        assert "No agent runs" in result

    def test_ago_minutes(self):
        """_ago for a few minutes shows minutes."""
        from agents.ceo_agent import _ago
        ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = _ago(ts)
        assert "m ago" in result

    def test_ago_none(self):
        """_ago with None returns 'never'."""
        from agents.ceo_agent import _ago
        assert _ago(None) == "never"

    def test_get_command_handlers(self):
        """get_command_handlers returns all expected commands."""
        from agents.ceo_agent import get_command_handlers
        handlers = get_command_handlers()
        expected = {"status", "listings", "budget", "run", "pause", "resume", "logs", "report"}
        assert set(handlers.keys()) == expected


# ===========================================================================
# Finance Agent — extra coverage
# ===========================================================================

class TestFinanceAgentExtra:
    """Additional tests for finance_agent to push coverage above 85%."""

    def test_utcnow(self):
        """_utcnow returns a string in the expected format."""
        from agents.finance_agent import _utcnow
        result = _utcnow()
        assert "T" in result
        assert result.endswith("Z")

    def test_thirty_days_ago(self):
        """_thirty_days_ago returns a timestamp 30 days in the past."""
        from agents.finance_agent import _thirty_days_ago
        result = _thirty_days_ago()
        assert "T" in result
        assert result.endswith("Z")

    def test_today_str(self):
        """_today_str returns a date string in YYYY-MM-DD format."""
        from agents.finance_agent import _today_str
        result = _today_str()
        assert len(result) == 10
        assert result[4] == "-"

    def test_build_listings_data_empty_db(self, tmp_db, mock_env):
        """_build_listings_data returns empty list on empty DB."""
        from agents.finance_agent import _build_listings_data
        conn = get_conn()
        result = _build_listings_data(conn, print_cost=8.0, shipping_cost=5.0)
        conn.close()
        assert result == []

    def test_build_listings_data_no_orders(self, tmp_db, mock_env):
        """_build_listings_data handles listing with no orders (0 margin)."""
        from agents.finance_agent import _build_listings_data
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = get_conn()
        conn.execute(
            """INSERT INTO listing_stats (listing_id, title, price, views, favorites, quantity, state, snapshot_at)
               VALUES ('L999', 'No Orders Print', 20.0, 50, 2, 999, 'active', ?)""",
            (now_str,),
        )
        conn.commit()
        result = _build_listings_data(conn, print_cost=8.0, shipping_cost=5.0)
        conn.close()
        assert len(result) == 1
        assert result[0]["listing_id"] == "L999"
        assert result[0]["orders_count"] == 0
        assert result[0]["margin_pct"] is None

    def test_optimize_ads_no_api_error(self, tmp_db, mock_env):
        """_optimize_ads handles non-404 EtsyAPIError and logs error."""
        from agents.finance_agent import _optimize_ads
        from integrations.etsy_client import EtsyAPIError

        mock_client = MagicMock()
        mock_client.shop_id = "TestShop"
        mock_client._request.side_effect = EtsyAPIError("Server Error", 500, "Internal Server Error")

        conn = get_conn()
        # Should not raise
        _optimize_ads(mock_client, conn)
        conn.close()

    def test_optimize_ads_generic_exception(self, tmp_db, mock_env):
        """_optimize_ads handles generic exceptions gracefully."""
        from agents.finance_agent import _optimize_ads

        mock_client = MagicMock()
        mock_client.shop_id = "TestShop"
        mock_client._request.side_effect = RuntimeError("network failure")

        conn = get_conn()
        # Should not raise
        _optimize_ads(mock_client, conn)
        conn.close()

    def test_run_finance_no_listings(self, tmp_db, mock_env):
        """run_finance with empty listings returns summary with 0 analyzed."""
        from agents.finance_agent import run_finance

        with patch("agents.finance_agent.EtsyClient") as MockEtsy, \
             patch("agents.finance_agent.load_dotenv"), \
             patch("agents.finance_agent.init_db"):
            mock_client = MockEtsy.return_value
            mock_client.shop_id = "TestShop"
            mock_client._request.side_effect = Exception("Ads unavailable")

            result = run_finance(dry_run=True)

        assert result["listings_analyzed"] == 0
        assert result["recommendations_created"] == 0
        assert result["auto_applied"] == 0

    def test_run_finance_with_listings_claude_pricing(self, tmp_db, mock_env):
        """run_finance calls Claude pricing when listings exist."""
        from agents.finance_agent import run_finance
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = get_conn()
        conn.execute(
            """INSERT INTO listing_stats (listing_id, title, price, views, favorites, quantity, state, snapshot_at)
               VALUES ('L500', 'Test Print', 25.0, 100, 5, 999, 'active', ?)""",
            (now_str,),
        )
        conn.commit()
        conn.close()

        recommendations = [
            {
                "listing_id": "L500",
                "current_price": 25.0,
                "recommended_price": 27.0,
                "change_pct": 8.0,
                "rationale": "test",
                "priority": "medium",
            }
        ]
        claude_response = json.dumps(recommendations)
        mock_msg = MagicMock()
        mock_msg.content = [MagicMock(text=claude_response)]

        with patch("agents.finance_agent.EtsyClient") as MockEtsy, \
             patch("agents.finance_agent.load_dotenv"), \
             patch("agents.finance_agent.init_db"), \
             patch("agents.finance_agent.anthropic.Anthropic") as MockAnthropic:
            mock_client = MockEtsy.return_value
            mock_client.shop_id = "TestShop"
            mock_client._request.side_effect = Exception("Ads unavailable")
            MockAnthropic.return_value.messages.create.return_value = mock_msg

            result = run_finance(dry_run=True)

        assert result["listings_analyzed"] == 1
        assert result["recommendations_created"] == 1

    def test_run_finance_large_price_change_pending(self, tmp_db, mock_env):
        """run_finance routes large price changes to pending approvals."""
        from agents.finance_agent import run_finance
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = get_conn()
        conn.execute(
            """INSERT INTO listing_stats (listing_id, title, price, views, favorites, quantity, state, snapshot_at)
               VALUES ('L501', 'Big Change Print', 20.0, 100, 5, 999, 'active', ?)""",
            (now_str,),
        )
        conn.commit()
        conn.close()

        # 30% price change exceeds 15% threshold → pending approval
        recommendations = [
            {
                "listing_id": "L501",
                "current_price": 20.0,
                "recommended_price": 26.0,
                "change_pct": 30.0,
                "rationale": "big change",
                "priority": "high",
            }
        ]
        mock_msg = MagicMock()
        mock_msg.content = [MagicMock(text=json.dumps(recommendations))]

        with patch("agents.finance_agent.EtsyClient") as MockEtsy, \
             patch("agents.finance_agent.load_dotenv"), \
             patch("agents.finance_agent.init_db"), \
             patch("agents.finance_agent.anthropic.Anthropic") as MockAnthropic:
            mock_client = MockEtsy.return_value
            mock_client.shop_id = "TestShop"
            mock_client._request.side_effect = Exception("Ads unavailable")
            MockAnthropic.return_value.messages.create.return_value = mock_msg

            result = run_finance(dry_run=True)

        assert result["pending_approval"] == 1

    def test_call_claude_pricing_strips_fences(self, mock_env):
        """_call_claude_pricing strips markdown fences from response."""
        from agents.finance_agent import _call_claude_pricing

        recs = [{"listing_id": "L1", "current_price": 20.0, "recommended_price": 22.0,
                  "change_pct": 10.0, "rationale": "good", "priority": "low"}]
        raw = f"```json\n{json.dumps(recs)}\n```"
        mock_msg = MagicMock()
        mock_msg.content = [MagicMock(text=raw)]

        with patch("agents.finance_agent.anthropic.Anthropic") as MockAnthropic, \
             patch("agents.finance_agent._PROMPT_PATH") as MockPath:
            MockPath.read_text.return_value = "Analyze {listings_data}"
            MockAnthropic.return_value.messages.create.return_value = mock_msg
            result = _call_claude_pricing([{"listing_id": "L1"}])

        assert result[0]["listing_id"] == "L1"


# ===========================================================================
# CEO Agent — deeper coverage
# ===========================================================================

class TestCEOAgentDeepCoverage:
    """Tests for uncovered lines in ceo_agent."""

    def test_utcnow_in_ceo_agent(self):
        """_utcnow in ceo_agent returns formatted string."""
        from agents.ceo_agent import _utcnow
        result = _utcnow()
        assert "T" in result
        assert result.endswith("Z")

    def test_call_claude(self, mock_env):
        """_call_claude calls anthropic and returns text."""
        mock_msg = MagicMock()
        mock_msg.content = [MagicMock(text="  digest text  ")]

        with patch("agents.ceo_agent.anthropic.Anthropic") as MockAnthropic:
            MockAnthropic.return_value.messages.create.return_value = mock_msg
            from agents.ceo_agent import _call_claude
            result = _call_claude("test prompt")

        assert result == "digest text"

    def test_run_ceo_success(self, tmp_db, mock_env):
        """run_ceo sends digest and returns digest_sent=True."""
        mock_msg = MagicMock()
        mock_msg.content = [MagicMock(text="Daily digest summary")]

        with patch("agents.ceo_agent.TelegramClient") as MockTelegram, \
             patch("agents.ceo_agent.load_dotenv"), \
             patch("agents.ceo_agent.init_db"), \
             patch("agents.ceo_agent.anthropic.Anthropic") as MockAnthropic, \
             patch("agents.ceo_agent._PROMPT_TEMPLATE_PATH") as MockPath:
            MockPath.read_text.return_value = (
                "Date:{date} Agents:{agent_summary} Listings:{listing_stats} "
                "Revenue:{weekly_revenue} Spend:{weekly_spend} "
                "Approvals:{pending_approvals} Top:{top_listing}"
            )
            MockAnthropic.return_value.messages.create.return_value = mock_msg
            telegram = MockTelegram.return_value
            telegram.send_digest.return_value = {}

            from agents.ceo_agent import run_ceo
            result = run_ceo()

        assert result["digest_sent"] is True
        telegram.send_digest.assert_called_once()

    def test_run_ceo_build_digest_fails_raises(self, tmp_db, mock_env):
        """run_ceo propagates exception when _build_digest fails."""
        with patch("agents.ceo_agent.TelegramClient") as MockTelegram, \
             patch("agents.ceo_agent.load_dotenv"), \
             patch("agents.ceo_agent.init_db"), \
             patch("agents.ceo_agent._build_digest", side_effect=RuntimeError("digest failed")):
            telegram = MockTelegram.return_value
            from agents.ceo_agent import run_ceo
            with pytest.raises(RuntimeError, match="digest failed"):
                run_ceo()

    def test_handle_status_error_path(self, tmp_db, mock_env):
        """handle_status error path sends error message."""
        with patch("agents.ceo_agent.TelegramClient") as MockTelegram, \
             patch("agents.ceo_agent.load_dotenv"), \
             patch("agents.ceo_agent.get_agent_run_summary", side_effect=RuntimeError("DB error")):
            telegram = MockTelegram.return_value
            telegram.reply_to_command.return_value = {}

            from agents.ceo_agent import handle_status
            handle_status(999, 1, [])

        reply_text = telegram.reply_to_command.call_args[0][2]
        assert "❌" in reply_text or "error" in reply_text.lower()

    def test_handle_listings_error_path(self, tmp_db, mock_env):
        """handle_listings error path sends error message."""
        with patch("agents.ceo_agent.TelegramClient") as MockTelegram, \
             patch("agents.ceo_agent.load_dotenv"), \
             patch("agents.ceo_agent.get_latest_listing_stats", side_effect=RuntimeError("DB error")):
            telegram = MockTelegram.return_value
            telegram.reply_to_command.return_value = {}

            from agents.ceo_agent import handle_listings
            handle_listings(999, 1, [])

        reply_text = telegram.reply_to_command.call_args[0][2]
        assert "❌" in reply_text or "error" in reply_text.lower()

    def test_handle_budget_error_path(self, tmp_db, mock_env):
        """handle_budget error path sends error message."""
        with patch("agents.ceo_agent.TelegramClient") as MockTelegram, \
             patch("agents.ceo_agent.load_dotenv"), \
             patch("agents.ceo_agent._get_weekly_ad_spend", side_effect=RuntimeError("DB error")):
            telegram = MockTelegram.return_value
            telegram.reply_to_command.return_value = {}

            from agents.ceo_agent import handle_budget
            handle_budget(999, 1, [])

        reply_text = telegram.reply_to_command.call_args[0][2]
        assert "❌" in reply_text or "error" in reply_text.lower()

    def test_handle_report_success(self, tmp_db, mock_env):
        """handle_report sends report successfully."""
        mock_msg = MagicMock()
        mock_msg.content = [MagicMock(text="weekly report")]

        with patch("agents.ceo_agent.TelegramClient") as MockTelegram, \
             patch("agents.ceo_agent.load_dotenv"), \
             patch("agents.ceo_agent.anthropic.Anthropic") as MockAnthropic, \
             patch("agents.ceo_agent._PROMPT_TEMPLATE_PATH") as MockPath:
            MockPath.read_text.return_value = (
                "Date:{date} Agents:{agent_summary} Listings:{listing_stats} "
                "Revenue:{weekly_revenue} Spend:{weekly_spend} "
                "Approvals:{pending_approvals} Top:{top_listing}"
            )
            MockAnthropic.return_value.messages.create.return_value = mock_msg
            telegram = MockTelegram.return_value
            telegram.reply_to_command.return_value = {}

            from agents.ceo_agent import handle_report
            handle_report(999, 1, [])

        # Called twice: once for "Generating report..." and once for the digest
        assert telegram.reply_to_command.call_count >= 2

    def test_handle_report_error_path(self, tmp_db, mock_env):
        """handle_report error path sends error message."""
        with patch("agents.ceo_agent.TelegramClient") as MockTelegram, \
             patch("agents.ceo_agent.load_dotenv"), \
             patch("agents.ceo_agent._build_digest", side_effect=RuntimeError("failed")):
            telegram = MockTelegram.return_value
            telegram.reply_to_command.return_value = {}

            from agents.ceo_agent import handle_report
            handle_report(999, 1, [])

        reply_texts = [call[0][2] for call in telegram.reply_to_command.call_args_list]
        assert any("❌" in t or "error" in t.lower() for t in reply_texts)

    def test_handle_run_valid_agent(self, tmp_db, mock_env):
        """handle_run with valid agent dispatches thread and replies."""
        with patch("agents.ceo_agent.TelegramClient") as MockTelegram, \
             patch("agents.ceo_agent.load_dotenv"), \
             patch("agents.analytics_agent.EtsyClient") if False else patch("agents.ceo_agent.load_dotenv"):
            pass

        with patch("agents.ceo_agent.TelegramClient") as MockTelegram, \
             patch("agents.ceo_agent.load_dotenv"), \
             patch("threading.Thread") as MockThread:
            telegram = MockTelegram.return_value
            telegram.reply_to_command.return_value = {}
            mock_thread_instance = MagicMock()
            MockThread.return_value = mock_thread_instance

            from agents.ceo_agent import handle_run
            handle_run(999, 1, ["analytics"])

        reply_text = telegram.reply_to_command.call_args[0][2]
        assert "Running" in reply_text or "analytics" in reply_text

    def test_handle_logs_error_path(self, tmp_db, mock_env, tmp_path, monkeypatch):
        """handle_logs error path sends error message when exception occurs."""
        from agents import ceo_agent
        monkeypatch.setattr(ceo_agent, "_LOGS_DIR", tmp_path)

        with patch("agents.ceo_agent.TelegramClient") as MockTelegram, \
             patch("agents.ceo_agent.load_dotenv"):
            telegram = MockTelegram.return_value
            telegram.reply_to_command.side_effect = [None, None]

            # Make read_text fail
            errors_log = tmp_path / "errors.log"
            errors_log.write_text("some errors")

            # Patch Path.read_text to fail
            with patch.object(Path, "read_text", side_effect=IOError("Permission denied")):
                from agents.ceo_agent import handle_logs
                handle_logs(999, 1, [])

        reply_text = telegram.reply_to_command.call_args[0][2]
        assert "❌" in reply_text or "error" in reply_text.lower()

    def test_handle_budget_with_spend(self, tmp_db, mock_env):
        """handle_budget shows by_listing breakdown when ad spend exists."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        insert_ad_spend("L001", today, 15.0, 100, 5000, 3, 2.0)

        with patch("agents.ceo_agent.TelegramClient") as MockTelegram, \
             patch("agents.ceo_agent.load_dotenv"):
            telegram = MockTelegram.return_value
            telegram.reply_to_command.return_value = {}

            from agents.ceo_agent import handle_budget
            handle_budget(999, 1, [])

        reply_text = telegram.reply_to_command.call_args[0][2]
        assert "L001" in reply_text or "15.00" in reply_text
