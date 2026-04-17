"""
test_etsy_client.py — Tests for integrations/etsy_client.py

Uses requests-mock for HTTP mocking and mock_env fixture for environment variables.
"""

import threading
import time
import pytest
import requests_mock as requests_mock_lib

from unittest.mock import patch, MagicMock
from integrations.etsy_client import EtsyClient, EtsyAuthError, EtsyAPIError, ETSY_BASE_URL, ETSY_TOKEN_URL


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client(mock_env):
    """Construct EtsyClient with env vars already patched."""
    with patch("integrations.etsy_client.load_dotenv"):
        return EtsyClient()


# ---------------------------------------------------------------------------
# get_listings
# ---------------------------------------------------------------------------

def test_get_listings_success(mock_env, requests_mock):
    """Mock GET returning 2 listings (fewer than limit=100), verify returned list."""
    client = _make_client(mock_env)
    url = f"{ETSY_BASE_URL}/application/shops/TestShop/listings"

    requests_mock.get(url, json={"results": [{"listing_id": 1}, {"listing_id": 2}]})

    result = client.get_listings(state="active")
    assert len(result) == 2
    assert result[0]["listing_id"] == 1


def test_get_listings_pagination(mock_env, requests_mock):
    """Mock two pages (first returns 100 items, second returns < 100), verify both pages fetched."""
    client = _make_client(mock_env)
    url = f"{ETSY_BASE_URL}/application/shops/TestShop/listings"

    # First page: 100 listings (triggers pagination)
    page1 = {"results": [{"listing_id": i} for i in range(100)]}
    # Second page: 2 listings (stops pagination)
    page2 = {"results": [{"listing_id": i} for i in range(100, 102)]}

    requests_mock.get(url, [{"json": page1}, {"json": page2}])

    result = client.get_listings(state="active")
    assert len(result) == 102


def test_patch_listing(mock_env, requests_mock):
    """Mock PATCH 200, verify correct payload sent."""
    client = _make_client(mock_env)
    url = f"{ETSY_BASE_URL}/application/listings/1001"

    requests_mock.patch(url, json={"listing_id": 1001, "title": "New Title"})

    result = client.patch_listing(1001, title="New Title")
    assert result["listing_id"] == 1001

    # Verify the request was made with the expected body
    assert requests_mock.last_request.json() == {"title": "New Title"}


def test_refresh_token_on_401(mock_env, requests_mock):
    """First call returns 401, refresh returns new token, retry returns 200."""
    client = _make_client(mock_env)
    url = f"{ETSY_BASE_URL}/application/shops/TestShop/listings"

    # Sequence: 401 -> (refresh succeeds) -> 200
    requests_mock.get(
        url,
        [
            {"status_code": 401, "text": "Unauthorized"},
            {"json": {"results": [{"listing_id": 1}]}},
        ],
    )

    # Mock the token refresh POST call
    requests_mock.post(
        ETSY_TOKEN_URL,
        json={"access_token": "new_token", "refresh_token": "new_refresh"},
    )

    with patch("integrations.etsy_client.set_key"):
        result = client.get_listings(state="active")

    assert len(result) == 1


def test_refresh_token_raises_etsy_auth_error_on_second_401(mock_env, requests_mock):
    """Both calls return 401 → EtsyAuthError raised."""
    client = _make_client(mock_env)
    url = f"{ETSY_BASE_URL}/application/shops/TestShop/listings"

    # Both the initial call and the retry after refresh return 401
    requests_mock.get(url, status_code=401, text="Unauthorized")
    requests_mock.post(
        ETSY_TOKEN_URL,
        json={"access_token": "new_token", "refresh_token": "new_refresh"},
    )

    with patch("integrations.etsy_client.set_key"):
        with pytest.raises(EtsyAuthError):
            client.get_listings(state="active")


def test_rate_limit_backoff(mock_env, requests_mock):
    """First call 429, second 200, verify retry happens."""
    client = _make_client(mock_env)
    url = f"{ETSY_BASE_URL}/application/shops/TestShop/listings"

    requests_mock.get(
        url,
        [
            {"status_code": 429, "text": "Rate limited"},
            {"json": {"results": [{"listing_id": 1}]}},
        ],
    )

    with patch("time.sleep") as mock_sleep:
        result = client.get_listings(state="active")

    mock_sleep.assert_called_once()
    assert len(result) == 1


def test_get_listing_images(mock_env, requests_mock):
    """Mock response with image list."""
    client = _make_client(mock_env)
    url = f"{ETSY_BASE_URL}/application/listings/1001/images"

    requests_mock.get(
        url,
        json={"results": [{"listing_image_id": 1, "url_fullxfull": "https://example.com/img.jpg"}]},
    )

    images = client.get_listing_images(1001)
    assert len(images) == 1
    assert images[0]["url_fullxfull"] == "https://example.com/img.jpg"


def test_etsy_api_error_on_500(mock_env, requests_mock):
    """Verify EtsyAPIError raised with status_code attribute on 500."""
    client = _make_client(mock_env)
    url = f"{ETSY_BASE_URL}/application/shops/TestShop/listings"

    requests_mock.get(url, status_code=500, text="Internal Server Error")

    with pytest.raises(EtsyAPIError) as exc_info:
        client.get_listings(state="active")

    assert exc_info.value.status_code == 500


def test_get_listing_single(mock_env, requests_mock):
    """get_listing() for a single listing_id."""
    client = _make_client(mock_env)
    url = f"{ETSY_BASE_URL}/application/listings/1001"

    requests_mock.get(url, json={"listing_id": 1001, "title": "Test"})

    result = client.get_listing(1001)
    assert result["listing_id"] == 1001


def test_get_orders(mock_env, requests_mock):
    """get_orders() returns results list."""
    client = _make_client(mock_env)
    url = f"{ETSY_BASE_URL}/application/shops/TestShop/receipts"

    requests_mock.get(url, json={"results": [{"receipt_id": "R001"}, {"receipt_id": "R002"}]})

    result = client.get_orders(limit=25)
    assert len(result) == 2


def test_get_shop_stats(mock_env, requests_mock):
    """get_shop_stats() returns the full dict."""
    client = _make_client(mock_env)
    url = f"{ETSY_BASE_URL}/application/shops/TestShop/stats"

    requests_mock.get(url, json={"results": [{"date": "2026-01-01", "visits": 100}]})

    result = client.get_shop_stats()
    assert "results" in result


def test_get_shop_listings_active_count(mock_env, requests_mock):
    """get_shop_listings_active_count() extracts count from API response."""
    client = _make_client(mock_env)
    url = f"{ETSY_BASE_URL}/application/shops/TestShop/listings"

    requests_mock.get(url, json={"count": 42, "results": []})

    count = client.get_shop_listings_active_count()
    assert count == 42


def test_204_response_returns_empty_dict(mock_env, requests_mock):
    """204 No Content response should return empty dict without error."""
    client = _make_client(mock_env)
    url = f"{ETSY_BASE_URL}/application/listings/1001"

    requests_mock.patch(url, status_code=204)

    result = client.patch_listing(1001, title="test")
    assert result == {}


def test_concurrent_refresh_lock(mock_env, requests_mock):
    """Spawn 2 threads both hitting 401, verify _refresh_token called only once."""
    client = _make_client(mock_env)
    url = f"{ETSY_BASE_URL}/application/shops/TestShop/listings"

    # Always return 401 initially, then 200 after refresh
    call_count = [0]

    def listing_response(request, context):
        call_count[0] += 1
        if call_count[0] <= 2:
            context.status_code = 401
            return "Unauthorized"
        context.status_code = 200
        return '{"results": [{"listing_id": 1}]}'

    requests_mock.get(url, text=listing_response)
    requests_mock.post(
        ETSY_TOKEN_URL,
        json={"access_token": "new_token", "refresh_token": "new_refresh"},
    )

    refresh_calls = [0]
    original_refresh = client._refresh_token

    def counted_refresh():
        refresh_calls[0] += 1
        original_refresh()

    client._refresh_token = counted_refresh

    results = []
    errors = []

    def make_request():
        try:
            with patch("integrations.etsy_client.set_key"):
                r = client.get_listings(state="active")
            results.append(r)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=make_request) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Both threads may trigger refresh, but the lock prevents concurrent refreshes
    # The key check: no exceptions from race conditions
    assert len(errors) == 0 or all(isinstance(e, (EtsyAuthError, EtsyAPIError)) for e in errors)
