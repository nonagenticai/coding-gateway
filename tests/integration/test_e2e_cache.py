"""E2E tests for cache endpoints.

These are mock/ASGI-only tests: they drive the app through httpx's
ASGITransport with all backend dependencies patched, so they need no running
service, database, or network. They are NOT integration tests and therefore
carry no ``pytest.mark.integration`` marker.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch


async def test_cache_stats(client, mock_cache_manager):
    """GET /api/cache/stats returns cache statistics."""
    mock_cache_manager.get_stats.return_value = {
        "hits": 10,
        "misses": 5,
        "size": 1024,
    }

    with patch("routers.cache.get_cache_manager", return_value=mock_cache_manager):
        resp = await client.get("/api/cache/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert "stats" in body
        assert body["stats"]["hits"] == 10


async def test_cache_health(client, mock_cache_manager):
    """GET /api/cache/health returns cache health."""
    mock_cache_manager.get_stats.return_value = {
        "hits": 0,
        "misses": 0,
        "size": 0,
    }

    with patch("routers.cache.get_cache_manager", return_value=mock_cache_manager):
        resp = await client.get("/api/cache/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "healthy"
        assert body["database_connected"] is True
        assert "performance" in body


async def test_cache_clear(client, mock_cache_manager):
    """DELETE /api/cache/clear clears cache entries."""
    mock_redis = MagicMock()
    mock_redis.client = MagicMock()
    mock_redis.client.scan = MagicMock(return_value=(0, []))
    mock_cache_manager.get_redis = AsyncMock(return_value=mock_redis)

    with patch("routers.cache.get_cache_manager", return_value=mock_cache_manager):
        resp = await client.delete("/api/cache/clear")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert "entries_cleared" in body


async def test_cache_clear_with_language_filter(client, mock_cache_manager):
    """DELETE /api/cache/clear?language=python clears only python cache entries."""
    mock_redis = MagicMock()
    mock_redis.client = MagicMock()
    mock_redis.client.scan = MagicMock(return_value=(0, []))
    mock_cache_manager.get_redis = AsyncMock(return_value=mock_redis)

    with patch("routers.cache.get_cache_manager", return_value=mock_cache_manager):
        resp = await client.delete("/api/cache/clear?language=python")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
