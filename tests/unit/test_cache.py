"""Tests for cache router endpoints — real HTTP requests via the test client.

Every test sends an actual HTTP request through FastAPI and asserts on both
the status code AND the response body.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# GET /api/cache/health
# ---------------------------------------------------------------------------


class TestCacheHealth:
    async def test_health_returns_healthy_status(self, client, mock_cache_manager):
        """GET /api/cache/health returns 200 with status=healthy and performance stats."""
        with patch("routers.cache.get_cache_manager", return_value=mock_cache_manager):
            resp = await client.get("/api/cache/health")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["database_connected"] is True
        assert "performance" in data

    async def test_health_returns_500_when_cache_unavailable(self, client):
        """GET /api/cache/health returns 500 when cache manager blows up."""
        bad_mgr = AsyncMock()
        bad_mgr.get_stats = MagicMock(side_effect=RuntimeError("redis gone"))
        with patch("routers.cache.get_cache_manager", return_value=bad_mgr):
            resp = await client.get("/api/cache/health")

        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# GET /api/cache/stats
# ---------------------------------------------------------------------------


class TestCacheStats:
    async def test_stats_returns_hit_miss_counts(self, client, mock_cache_manager):
        """GET /api/cache/stats returns stats with hits/misses from cache manager."""
        mock_cache_manager.get_stats = MagicMock(
            return_value={"hits": 42, "misses": 7, "errors": 1}
        )
        with patch("routers.cache.get_cache_manager", return_value=mock_cache_manager):
            resp = await client.get("/api/cache/stats")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["stats"]["hits"] == 42
        assert data["stats"]["misses"] == 7
        assert data["stats"]["errors"] == 1

    async def test_stats_returns_500_on_failure(self, client, mock_cache_manager):
        """GET /api/cache/stats returns 500 when cache manager raises."""
        mock_cache_manager.get_stats = MagicMock(side_effect=RuntimeError("redis down"))
        with patch("routers.cache.get_cache_manager", return_value=mock_cache_manager):
            resp = await client.get("/api/cache/stats")

        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# GET /api/cache/info?file_path=...
# ---------------------------------------------------------------------------


class TestFileCacheInfo:
    async def test_cache_miss_returns_cached_false(self, client, mock_cache_manager):
        """GET /api/cache/info with uncached file returns cached=false."""
        mock_cache_manager.get = AsyncMock(return_value=None)
        with patch("routers.cache.get_cache_manager", return_value=mock_cache_manager):
            resp = await client.get("/api/cache/info?file_path=/src/main.py")

        assert resp.status_code == 200
        body = resp.json()
        assert body["cached"] is False
        assert body["entities_count"] == 0

    async def test_cache_hit_returns_full_data(self, client, mock_cache_manager):
        """GET /api/cache/info with cached file returns all fields."""
        mock_cache_manager.get = AsyncMock(
            return_value={
                "cached": True,
                "language": "python",
                "entities_count": 12,
                "relationships_count": 5,
            }
        )
        with patch("routers.cache.get_cache_manager", return_value=mock_cache_manager):
            resp = await client.get("/api/cache/info?file_path=/src/main.py")

        assert resp.status_code == 200
        body = resp.json()
        assert body["cached"] is True
        assert body["language"] == "python"
        assert body["entities_count"] == 12
        assert body["relationships_count"] == 5

    async def test_null_file_path_returns_400(self, client, mock_cache_manager):
        """GET /api/cache/info?file_path=null returns 400."""
        with patch("routers.cache.get_cache_manager", return_value=mock_cache_manager):
            resp = await client.get("/api/cache/info?file_path=null")

        assert resp.status_code == 400

    async def test_empty_file_path_returns_400(self, client, mock_cache_manager):
        """GET /api/cache/info?file_path= returns 400."""
        with patch("routers.cache.get_cache_manager", return_value=mock_cache_manager):
            resp = await client.get("/api/cache/info?file_path=")

        assert resp.status_code == 400

    async def test_missing_file_path_returns_422(self, client, mock_cache_manager):
        """GET /api/cache/info without file_path returns 422 (validation error)."""
        with patch("routers.cache.get_cache_manager", return_value=mock_cache_manager):
            resp = await client.get("/api/cache/info")

        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# DELETE /api/cache/clear
# ---------------------------------------------------------------------------


class TestClearCache:
    async def test_clear_all_returns_entry_count(self, client, mock_cache_manager):
        """DELETE /api/cache/clear returns number of entries cleared."""
        redis_mock = MagicMock()
        redis_mock.client = MagicMock()
        redis_mock.client.scan = MagicMock(return_value=(0, [b"k1", b"k2", b"k3"]))
        redis_mock.client.delete = MagicMock(return_value=1)
        mock_cache_manager.get_redis = AsyncMock(return_value=redis_mock)

        with patch("routers.cache.get_cache_manager", return_value=mock_cache_manager):
            resp = await client.delete("/api/cache/clear")

        assert resp.status_code == 200
        body = resp.json()
        assert body["entries_cleared"] == 3
        assert body["status"] == "success"

    async def test_clear_by_language_includes_language_in_message(self, client, mock_cache_manager):
        """DELETE /api/cache/clear?language=python mentions language in message."""
        redis_mock = MagicMock()
        redis_mock.client = MagicMock()
        redis_mock.client.scan = MagicMock(return_value=(0, [b"py1"]))
        redis_mock.client.delete = MagicMock(return_value=1)
        mock_cache_manager.get_redis = AsyncMock(return_value=redis_mock)

        with patch("routers.cache.get_cache_manager", return_value=mock_cache_manager):
            resp = await client.delete("/api/cache/clear?language=python")

        assert resp.status_code == 200
        body = resp.json()
        assert body["entries_cleared"] == 1
        assert "python" in body["message"]

    async def test_clear_returns_500_on_redis_error(self, client, mock_cache_manager):
        """DELETE /api/cache/clear returns 500 when Redis blows up."""
        mock_cache_manager.get_redis = AsyncMock(side_effect=RuntimeError("boom"))
        with patch("routers.cache.get_cache_manager", return_value=mock_cache_manager):
            resp = await client.delete("/api/cache/clear")

        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# POST /api/cache/optimize
# ---------------------------------------------------------------------------


class TestOptimizeCache:
    async def test_optimize_returns_stats(self, client, mock_cache_manager):
        """POST /api/cache/optimize returns optimization_stats with entry counts."""
        mock_cache_manager.get_stats = MagicMock(return_value={"total_entries": 100})
        with patch("routers.cache.get_cache_manager", return_value=mock_cache_manager):
            resp = await client.post("/api/cache/optimize")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert body["optimization_stats"]["entries_before"] == 100

    async def test_optimize_returns_500_on_failure(self, client, mock_cache_manager):
        """POST /api/cache/optimize returns 500 on error."""
        mock_cache_manager.get_stats = MagicMock(side_effect=RuntimeError("fail"))
        with patch("routers.cache.get_cache_manager", return_value=mock_cache_manager):
            resp = await client.post("/api/cache/optimize")

        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# GET /api/cache/stats/{container_name}
# ---------------------------------------------------------------------------


class TestContainerCacheStats:
    async def test_returns_container_stats(self, client, mock_cache_manager):
        """GET /api/cache/stats/<name> returns per-container entry_count and size."""
        redis_mock = MagicMock()
        redis_mock.client = MagicMock()
        redis_mock.client.scan = MagicMock(return_value=(0, [b"k1"]))
        redis_mock.client.get = MagicMock(return_value=b"value_bytes")
        mock_cache_manager.get_redis = AsyncMock(return_value=redis_mock)

        with patch("routers.cache.get_cache_manager", return_value=mock_cache_manager):
            resp = await client.get("/api/cache/stats/my-container")

        assert resp.status_code == 200
        body = resp.json()
        assert body["container_name"] == "my-container"
        assert body["entry_count"] == 1
        assert body["size"] == len(b"value_bytes")

    async def test_invalid_container_name_returns_400(self, client, mock_cache_manager):
        """GET /api/cache/stats/<bad> returns 400 for invalid names."""
        with patch("routers.cache.get_cache_manager", return_value=mock_cache_manager):
            resp = await client.get("/api/cache/stats/bad@name!")

        assert resp.status_code == 400

    async def test_redis_error_returns_500(self, client, mock_cache_manager):
        """GET /api/cache/stats/<name> returns 500 on redis failure."""
        mock_cache_manager.get_redis = AsyncMock(side_effect=RuntimeError("redis gone"))
        with patch("routers.cache.get_cache_manager", return_value=mock_cache_manager):
            resp = await client.get("/api/cache/stats/my-container")

        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# _RedisServiceShim basic wiring
# ---------------------------------------------------------------------------


class TestRedisShimWiring:
    def test_redis_url_from_settings(self):
        """_RedisServiceShim.client uses Redis.from_url with the configured URL.

        ``_redis_url`` is now a thunk that resolves Settings at call time so
        that import-time loading does not require ENVIRONMENT/SECRET_KEY to
        already be set (and so that tests can flip the URL without restarting
        the process).
        """
        fake_url = "redis://myhost:6380/2"
        mock_client = MagicMock()

        with (
            patch("services.cache_manager._redis_url", return_value=fake_url),
            patch("redis.Redis.from_url", return_value=mock_client) as mock_from_url,
        ):
            from services.cache_manager import _RedisServiceShim

            shim = _RedisServiceShim()
            result = shim.client

            mock_from_url.assert_called_once_with(fake_url, decode_responses=True)
            assert result is mock_client
