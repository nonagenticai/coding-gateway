"""Gap tests for routers/cache.py — covers endpoint edge cases and
branches that the main test files do not exercise.

Tests send real HTTP requests through the ASGI client.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch


def _make_redis_mock():
    """Build a mock Redis shim with .client attribute."""
    redis = MagicMock()
    redis.client = MagicMock()
    redis.client.scan = MagicMock(return_value=(0, []))
    redis.client.get = MagicMock(return_value=None)
    redis.client.delete = MagicMock(return_value=1)
    redis.client.ttl = MagicMock(return_value=-1)
    redis.ttl = MagicMock(return_value=-1)
    redis.expire = MagicMock(return_value=True)
    redis.hgetall = MagicMock(return_value={})
    redis.hset = MagicMock(return_value=1)
    redis.lrange = MagicMock(return_value=[])
    redis.lpush = MagicMock(return_value=1)
    return redis


def _make_cm(redis_mock=None):
    """Build a mock cache manager."""
    cm = AsyncMock()
    rm = redis_mock or _make_redis_mock()
    cm.get_redis = AsyncMock(return_value=rm)
    cm.get_stats = MagicMock(return_value={"hits": 0, "misses": 0, "errors": 0})
    cm.get = AsyncMock(return_value=None)
    cm.set = AsyncMock(return_value=True)
    cm.delete = AsyncMock(return_value=True)
    cm.delete_pattern = AsyncMock(return_value=0)
    cm.get_eviction_history = AsyncMock(return_value={"evictions": [], "total": 0, "summary": {}})
    return cm, rm


# ---------------------------------------------------------------------------
# Container stats — multi-page scan cursor
# ---------------------------------------------------------------------------


class TestContainerStatsScanPagination:
    async def test_multi_batch_scan(self, client):
        """When Redis returns a non-zero cursor, the endpoint should continue scanning."""
        cm, rm = _make_cm()
        # First call returns cursor=42, second returns cursor=0
        rm.client.scan.side_effect = [
            (42, ["k1"]),
            (0, ["k2"]),
        ]
        rm.client.get.return_value = "v"
        with patch("routers.cache.get_cache_manager", return_value=cm):
            resp = await client.get("/api/cache/stats/myapp")
        assert resp.status_code == 200
        body = resp.json()
        assert body["entry_count"] == 2


# ---------------------------------------------------------------------------
# Cache clear — multi-page scan
# ---------------------------------------------------------------------------


class TestClearCacheScanPagination:
    async def test_clear_multi_batch(self, client):
        cm, rm = _make_cm()
        rm.client.scan.side_effect = [
            (42, ["k1", "k2"]),
            (0, ["k3"]),
        ]
        with patch("routers.cache.get_cache_manager", return_value=cm):
            resp = await client.delete("/api/cache/clear")
        assert resp.status_code == 200
        body = resp.json()
        assert body["entries_cleared"] == 3


# ---------------------------------------------------------------------------
# Container cache clear — with patterns
# ---------------------------------------------------------------------------


class TestContainerClearWithPattern:
    async def test_clear_container_deletes_matching_keys(self, client):
        cm, rm = _make_cm()
        rm.client.scan.return_value = (0, ["k1", "k2", "k3"])
        with (
            patch("routers.cache._require_container_exists"),
            patch("routers.cache.get_cache_manager", return_value=cm),
        ):
            resp = await client.delete("/api/cache/clear/myapp")
        assert resp.status_code == 200
        body = resp.json()
        assert body["container_name"] == "myapp"
        assert body["deleted_count"] == 3


# ---------------------------------------------------------------------------
# Cache entry — value type variations
# ---------------------------------------------------------------------------


class TestCacheEntryValueTypes:
    async def test_dict_value_returns_full_entry(self, client):
        cm, rm = _make_cm()
        cm.get.return_value = {"key": "val", "nested": [1, 2, 3]}
        rm.client.ttl.return_value = 600
        with patch("routers.cache.get_cache_manager", return_value=cm):
            resp = await client.get("/api/cache/entry/myapp/analysis")
        assert resp.status_code == 200
        body = resp.json()
        assert body["size"] > 0
        assert body["ttl_remaining"] == 600

    async def test_list_value_returns_size(self, client):
        cm, rm = _make_cm()
        cm.get.return_value = [1, 2, 3]
        rm.client.ttl.return_value = 100
        with patch("routers.cache.get_cache_manager", return_value=cm):
            resp = await client.get("/api/cache/entry/myapp/analysis")
        assert resp.status_code == 200
        assert resp.json()["size"] > 0


# ---------------------------------------------------------------------------
# Warmup — error in set does not crash
# ---------------------------------------------------------------------------


class TestWarmupSetFailure:
    async def test_warmup_set_failure_returns_500(self, client):
        """When cache set fails during warmup, the endpoint returns 500."""
        cm, _ = _make_cm()
        cm.get.return_value = None
        cm.set.side_effect = RuntimeError("redis set failed")
        with patch("routers.cache.get_cache_manager", return_value=cm):
            resp = await client.post(
                "/api/cache/warmup/myapp",
                params={"operations": "graph_stats"},
            )
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# Export — non-JSON parseable values handled gracefully
# ---------------------------------------------------------------------------


class TestExportNonJsonValues:
    async def test_export_plain_string_value(self, client):
        cm, rm = _make_cm()
        rm.client.scan.return_value = (0, ["k1"])
        rm.client.get.return_value = "plain_string_not_json"
        rm.ttl.return_value = -1
        with patch("routers.cache.get_cache_manager", return_value=cm):
            resp = await client.get("/api/cache/export/myapp", params={"format": "json"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["container_name"] == "myapp"


# ---------------------------------------------------------------------------
# Search — with results
# ---------------------------------------------------------------------------


class TestSearchWithResults:
    async def test_search_returns_matching_keys(self, client):
        cm, rm = _make_cm()
        rm.client.scan.return_value = (0, ["analysis:k1", "analysis:k2"])
        with patch("routers.cache.get_cache_manager", return_value=cm):
            resp = await client.get(
                "/api/cache/search/myapp",
                params={"pattern": "analysis*"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert "matches" in body


# ---------------------------------------------------------------------------
# Compare — actual stats in response
# ---------------------------------------------------------------------------


class TestCompareStats:
    async def test_compare_returns_per_container_stats(self, client):
        cm, rm = _make_cm()
        rm.client.scan.return_value = (0, ["k1"])
        rm.client.get.return_value = "val"
        with patch("routers.cache.get_cache_manager", return_value=cm):
            resp = await client.get(
                "/api/cache/compare",
                params={"container_names": "a,b"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["comparison_count"] == 2
        assert "containers" in body


# ---------------------------------------------------------------------------
# Container name validation
# ---------------------------------------------------------------------------


class TestContainerNameValidation:
    """_validate_container_name rejects path traversal and glob injection."""

    async def test_reject_path_traversal(self, client):
        """Container names with .. or / are rejected."""
        resp = await client.get("/api/cache/stats/../../../etc/passwd")
        assert resp.status_code in (400, 404, 422)

    async def test_reject_glob_characters(self, client):
        """Container names with glob chars are rejected."""
        resp = await client.get("/api/cache/stats/test*container")
        assert resp.status_code in (400, 404, 422)

    async def test_valid_container_name_accepted(self, client):
        """Valid container names with dots, hyphens, underscores pass validation."""
        from routers.cache import _validate_container_name

        assert _validate_container_name("my-container") == "my-container"
        assert _validate_container_name("my_container.v2") == "my_container.v2"
        assert _validate_container_name("Container123") == "Container123"

    async def test_invalid_container_name_raises_400(self, client):
        """Invalid container names raise 400."""
        import pytest as _pytest
        from fastapi import HTTPException

        from routers.cache import _validate_container_name

        with _pytest.raises(HTTPException) as exc_info:
            _validate_container_name("../../etc")
        assert exc_info.value.status_code == 400

    async def test_require_container_exists_returns_404(self, client):
        """_require_container_exists raises 404 when coding-analysis has no graph."""
        import httpx as _httpx
        import pytest as _pytest
        from fastapi import HTTPException

        from routers.cache import _require_container_exists

        req = _httpx.Request("GET", "http://coding-analysis/api/analysis/graphs/x")
        fake_client = AsyncMock()
        fake_client.get = AsyncMock(
            return_value=_httpx.Response(404, json={"detail": "not found"}, request=req)
        )
        with patch("routers.proxy.get_proxy_client", AsyncMock(return_value=fake_client)):
            with _pytest.raises(HTTPException) as exc_info:
                await _require_container_exists("nonexistent-container", "tenant-a")
            assert exc_info.value.status_code == 404
            assert "nonexistent-container" in exc_info.value.detail


# ---------------------------------------------------------------------------
# Cache info endpoint for missing file
# ---------------------------------------------------------------------------


class TestCacheInfoEndpoint:
    """GET /api/cache/info validates file_path parameter."""

    async def test_empty_file_path_returns_400(self, client):
        """Empty or null file_path returns 400."""
        resp = await client.get("/api/cache/info?file_path=")
        assert resp.status_code == 400

    async def test_null_file_path_returns_400(self, client):
        """Literal 'null' file_path returns 400."""
        resp = await client.get("/api/cache/info?file_path=null")
        assert resp.status_code == 400

    async def test_valid_file_path_returns_cached_false(self, client):
        """Valid file_path for uncached file returns cached=false."""
        cm, rm = _make_cm()
        cm.get = AsyncMock(return_value=None)
        with patch("routers.cache.get_cache_manager", return_value=cm):
            resp = await client.get("/api/cache/info?file_path=src/main.py")
        assert resp.status_code == 200
        body = resp.json()
        assert body["cached"] is False


# ---------------------------------------------------------------------------
# Bug regression: HTTPException not re-raised in cache keys endpoint
# ---------------------------------------------------------------------------


class TestCacheKeysHTTPExceptionNotSwallowed:
    """Verify that _validate_container_name's 400 is NOT wrapped in 500."""

    async def test_invalid_container_name_returns_400_not_500(self, client):
        """GET /api/cache/keys/{bad_name} with glob chars returns 400, not 500."""
        # Use glob injection chars that pass path routing but fail _validate_container_name
        resp = await client.get("/api/cache/keys/test*container")
        assert resp.status_code == 400, (
            f"Expected 400 for invalid container name, got {resp.status_code}"
        )
