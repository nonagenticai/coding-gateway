"""Tests for cache router — advanced endpoints: keys, TTL, entry details,
search, export, warmup, compare, evictions, container info/clear.

All tests send real HTTP requests and assert on response bodies.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch


def _mock_redis():
    """Return a mock redis shim with scan/get/delete/ttl/expire."""
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


def _mock_cache_mgr(redis_mock=None):
    """Return a (cache_manager, redis_mock) tuple."""
    cm = AsyncMock()
    rm = redis_mock or _mock_redis()
    cm.get_redis = AsyncMock(return_value=rm)
    cm.get_stats = MagicMock(return_value={"hits": 0, "misses": 0, "errors": 0})
    cm.get = AsyncMock(return_value=None)
    cm.set = AsyncMock(return_value=True)
    cm.delete = AsyncMock(return_value=True)
    cm.delete_pattern = AsyncMock(return_value=0)
    cm.get_eviction_history = AsyncMock(return_value={"evictions": [], "total": 0, "summary": {}})
    return cm, rm


# ---------------------------------------------------------------------------
# GET /api/cache/stats — basic + error
# ---------------------------------------------------------------------------


class TestCacheStats:
    async def test_stats_body_contains_stats_dict(self, client):
        cm, _ = _mock_cache_mgr()
        cm.get_stats.return_value = {"hits": 99, "misses": 3, "errors": 0}
        with patch("routers.cache.get_cache_manager", return_value=cm):
            resp = await client.get("/api/cache/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert body["stats"]["hits"] == 99
        assert body["stats"]["misses"] == 3

    async def test_stats_error_returns_500(self, client):
        with patch("routers.cache.get_cache_manager", side_effect=Exception("boom")):
            resp = await client.get("/api/cache/stats")
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# GET /api/cache/stats/{container_name}
# ---------------------------------------------------------------------------


class TestContainerCacheStats:
    async def test_string_value_counted_correctly(self, client):
        """String values should be measured by their UTF-8 byte length."""
        cm, rm = _mock_cache_mgr()
        rm.client.scan.return_value = (0, ["key1"])
        rm.client.get.return_value = "hello_world"
        with patch("routers.cache.get_cache_manager", return_value=cm):
            resp = await client.get("/api/cache/stats/myapp")
        assert resp.status_code == 200
        body = resp.json()
        assert body["container_name"] == "myapp"
        assert body["entry_count"] == 1
        assert body["size"] == len(b"hello_world")

    async def test_bytes_value_counted_correctly(self, client):
        """Bytes values should be measured by their raw length."""
        cm, rm = _mock_cache_mgr()
        rm.client.scan.return_value = (0, ["key1"])
        rm.client.get.return_value = b"bytes_val"
        with patch("routers.cache.get_cache_manager", return_value=cm):
            resp = await client.get("/api/cache/stats/myapp")
        assert resp.status_code == 200
        body = resp.json()
        assert body["size"] == len(b"bytes_val")

    async def test_invalid_container_name_returns_400(self, client):
        resp = await client.get("/api/cache/stats/bad name!")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# GET /api/cache/info?file_path=...
# ---------------------------------------------------------------------------


class TestFileCacheInfo:
    async def test_not_cached_returns_false(self, client):
        cm, _ = _mock_cache_mgr()
        with patch("routers.cache.get_cache_manager", return_value=cm):
            resp = await client.get("/api/cache/info", params={"file_path": "/src/foo.py"})
        assert resp.status_code == 200
        assert resp.json()["cached"] is False

    async def test_cached_returns_full_info(self, client):
        cm, _ = _mock_cache_mgr()
        cm.get.return_value = {
            "cached": True,
            "cached_at": "2026-01-01T00:00:00Z",
            "language": "python",
            "entities_count": 10,
        }
        with patch("routers.cache.get_cache_manager", return_value=cm):
            resp = await client.get("/api/cache/info", params={"file_path": "/src/foo.py"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["cached"] is True
        assert body["language"] == "python"

    async def test_empty_path_returns_400(self, client):
        resp = await client.get("/api/cache/info", params={"file_path": ""})
        assert resp.status_code == 400

    async def test_null_path_returns_400(self, client):
        resp = await client.get("/api/cache/info", params={"file_path": "null"})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# DELETE /api/cache/clear
# ---------------------------------------------------------------------------


class TestClearCache:
    async def test_clear_all(self, client):
        cm, rm = _mock_cache_mgr()
        rm.client.scan.return_value = (0, ["k1", "k2"])
        with patch("routers.cache.get_cache_manager", return_value=cm):
            resp = await client.delete("/api/cache/clear")
        assert resp.status_code == 200
        body = resp.json()
        assert body["entries_cleared"] == 2
        assert body["status"] == "success"

    async def test_clear_by_language_mentions_language(self, client):
        cm, rm = _mock_cache_mgr()
        rm.client.scan.return_value = (0, [])
        with patch("routers.cache.get_cache_manager", return_value=cm):
            resp = await client.delete("/api/cache/clear", params={"language": "python"})
        assert resp.status_code == 200
        assert "python" in resp.json()["message"]


# ---------------------------------------------------------------------------
# DELETE /api/cache/clear/{container_name}
# ---------------------------------------------------------------------------


class TestClearContainerCache:
    async def test_clear_container_success(self, client):
        cm, rm = _mock_cache_mgr()
        rm.client.scan.return_value = (0, ["k1", "k2"])
        with (
            patch("routers.cache._require_container_exists"),
            patch("routers.cache.get_cache_manager", return_value=cm),
        ):
            resp = await client.delete("/api/cache/clear/myapp")
        assert resp.status_code == 200
        body = resp.json()
        assert body["container_name"] == "myapp"
        assert body["deleted_count"] == 2

    async def test_clear_container_not_found(self, client):
        from fastapi import HTTPException

        with patch(
            "routers.cache._require_container_exists",
            side_effect=HTTPException(status_code=404, detail="Container 'missing' not found"),
        ):
            resp = await client.delete("/api/cache/clear/missing")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/cache/optimize
# ---------------------------------------------------------------------------


class TestOptimize:
    async def test_optimize_returns_before_count(self, client):
        cm, _ = _mock_cache_mgr()
        cm.get_stats.return_value = {"total_entries": 50}
        with patch("routers.cache.get_cache_manager", return_value=cm):
            resp = await client.post("/api/cache/optimize")
        assert resp.status_code == 200
        body = resp.json()
        assert body["optimization_stats"]["entries_before"] == 50
        assert body["status"] == "success"


# ---------------------------------------------------------------------------
# GET /api/cache/keys/{container_name}
# ---------------------------------------------------------------------------


class TestContainerKeys:
    async def test_returns_key_list(self, client):
        cm, rm = _mock_cache_mgr()
        rm.client.scan.return_value = (0, ["k1", "k2"])
        with patch("routers.cache.get_cache_manager", return_value=cm):
            resp = await client.get("/api/cache/keys/myapp")
        assert resp.status_code == 200
        body = resp.json()
        assert body["container_name"] == "myapp"
        assert isinstance(body["keys"], list)
        assert body["count"] == 2

    async def test_pagination_limits_results(self, client):
        cm, rm = _mock_cache_mgr()
        rm.client.scan.return_value = (0, ["k1", "k2", "k3", "k4"])
        with patch("routers.cache.get_cache_manager", return_value=cm):
            resp = await client.get("/api/cache/keys/myapp", params={"limit": 2, "offset": 1})
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] <= 2


# ---------------------------------------------------------------------------
# PATCH /api/cache/ttl/{container_name}/{cache_operation}
# ---------------------------------------------------------------------------


class TestUpdateTTL:
    async def test_update_ttl_returns_new_value(self, client):
        cm, rm = _mock_cache_mgr()
        rm.client.scan.return_value = (0, ["k1"])
        rm.client.ttl.return_value = 3600
        with (
            patch("routers.cache._validate_container_name", return_value="myapp"),
            patch("routers.cache._require_container_exists"),
            patch("routers.cache.get_cache_manager", return_value=cm),
        ):
            resp = await client.patch(
                "/api/cache/ttl/myapp/analysis",
                json={"ttl_seconds": 1800},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ttl_seconds"] == 1800
        assert body["container_name"] == "myapp"
        assert body["operation"] == "analysis"
        # The URL's last segment scopes the SCAN pattern.
        match = rm.client.scan.call_args.kwargs["match"]
        assert match.endswith(":container:myapp:analysis:*")

    async def test_update_ttl_not_found(self, client):
        from fastapi import HTTPException

        with patch(
            "routers.cache._require_container_exists",
            side_effect=HTTPException(status_code=404, detail="Not found"),
        ):
            resp = await client.patch(
                "/api/cache/ttl/missing/op",
                json={"ttl_seconds": 600},
            )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/cache/entry/{container_name}/{cache_operation}
# ---------------------------------------------------------------------------


class TestCacheEntryDetails:
    async def test_entry_found_returns_key_and_size(self, client):
        cm, rm = _mock_cache_mgr()
        cm.get.return_value = {"data": "value"}
        rm.client.ttl.return_value = 3600
        with patch("routers.cache.get_cache_manager", return_value=cm):
            resp = await client.get("/api/cache/entry/myapp/analysis")
        assert resp.status_code == 200
        body = resp.json()
        # The URL's last segment must reach the handler as the cache-key segment.
        assert body["key"].endswith(":container:myapp:analysis")
        assert body["size"] > 0
        assert body["ttl_remaining"] == 3600

    async def test_entry_not_found_returns_404(self, client):
        cm, _ = _mock_cache_mgr()
        cm.get.return_value = None
        with patch("routers.cache.get_cache_manager", return_value=cm):
            resp = await client.get("/api/cache/entry/myapp/analysis")
        assert resp.status_code == 404

    async def test_entry_negative_ttl_means_no_expiration(self, client):
        cm, rm = _mock_cache_mgr()
        cm.get.return_value = "string_value"
        rm.client.ttl.return_value = -1
        with patch("routers.cache.get_cache_manager", return_value=cm):
            resp = await client.get("/api/cache/entry/myapp/analysis")
        assert resp.status_code == 200
        assert resp.json()["ttl_remaining"] is None


# ---------------------------------------------------------------------------
# GET /api/cache/info/{container_name}
# ---------------------------------------------------------------------------


class TestContainerCacheInfo:
    async def test_returns_health_and_config(self, client):
        cm, rm = _mock_cache_mgr()
        rm.client.scan.return_value = (0, ["k1"])
        rm.client.get.return_value = "val"
        with patch("routers.cache.get_cache_manager", return_value=cm):
            resp = await client.get("/api/cache/info/myapp")
        assert resp.status_code == 200
        body = resp.json()
        assert body["health_status"] == "healthy"
        assert "cache_configuration" in body
        assert "usage_statistics" in body
        assert body["container_name"] == "myapp"


# ---------------------------------------------------------------------------
# GET /api/cache/search/{container_name}
# ---------------------------------------------------------------------------


class TestSearchCacheKeys:
    async def test_search_returns_matches(self, client):
        cm, rm = _mock_cache_mgr()
        rm.client.scan.return_value = (0, ["k1", "k2"])
        with patch("routers.cache.get_cache_manager", return_value=cm):
            resp = await client.get(
                "/api/cache/search/myapp",
                params={"pattern": "analysis*"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert "matches" in body

    async def test_empty_pattern_returns_400(self, client):
        resp = await client.get("/api/cache/search/myapp", params={"pattern": ""})
        assert resp.status_code == 400

    async def test_null_pattern_returns_400(self, client):
        resp = await client.get("/api/cache/search/myapp", params={"pattern": "null"})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# GET /api/cache/export/{container_name}
# ---------------------------------------------------------------------------


class TestExportCache:
    async def test_export_json_returns_entries(self, client):
        cm, rm = _mock_cache_mgr()
        rm.client.scan.return_value = (0, ["k1"])
        rm.client.get.return_value = '{"x":1}'
        rm.ttl.return_value = 3600
        with patch("routers.cache.get_cache_manager", return_value=cm):
            resp = await client.get("/api/cache/export/myapp", params={"format": "json"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["container_name"] == "myapp"
        assert isinstance(body["entries"], list)

    async def test_export_csv_returns_csv_content_type(self, client):
        cm, rm = _mock_cache_mgr()
        rm.client.scan.return_value = (0, ["k1"])
        rm.client.get.return_value = "val"
        rm.ttl.return_value = -1
        with patch("routers.cache.get_cache_manager", return_value=cm):
            resp = await client.get("/api/cache/export/myapp", params={"format": "csv"})
        assert resp.status_code == 200
        assert "text/csv" in resp.headers.get("content-type", "")

    async def test_export_binary_value_handled(self, client):
        cm, rm = _mock_cache_mgr()
        rm.client.scan.return_value = (0, [b"k1"])
        rm.client.get.return_value = b"\x80invalid"
        rm.ttl.return_value = -1
        with patch("routers.cache.get_cache_manager", return_value=cm):
            resp = await client.get("/api/cache/export/myapp", params={"format": "json"})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /api/cache/warmup/{container_name}
# ---------------------------------------------------------------------------


class TestCacheWarmup:
    async def test_warmup_default_operations(self, client):
        cm, _ = _mock_cache_mgr()
        cm.get.return_value = None
        cm.set.return_value = True
        with patch("routers.cache.get_cache_manager", return_value=cm):
            resp = await client.post("/api/cache/warmup/myapp")
        assert resp.status_code == 200
        body = resp.json()
        assert body["container_name"] == "myapp"
        assert body["total_entries"] >= 1
        assert body["success_count"] >= 1
        assert body["failure_count"] == 0

    async def test_warmup_specific_operations(self, client):
        cm, _ = _mock_cache_mgr()
        cm.get.return_value = None
        with patch("routers.cache.get_cache_manager", return_value=cm):
            resp = await client.post(
                "/api/cache/warmup/myapp",
                params={"operations": "graph_stats,file_list"},
            )
        assert resp.status_code == 200
        assert resp.json()["total_entries"] == 2

    async def test_warmup_invalid_operation_returns_400(self, client):
        resp = await client.post(
            "/api/cache/warmup/myapp",
            params={"operations": "invalid_op"},
        )
        assert resp.status_code == 400

    async def test_warmup_cache_hit_counts_as_success(self, client):
        cm, _ = _mock_cache_mgr()
        cm.get.return_value = {"cached": True}
        with patch("routers.cache.get_cache_manager", return_value=cm):
            resp = await client.post(
                "/api/cache/warmup/myapp",
                params={"operations": "graph_stats"},
            )
        assert resp.status_code == 200
        assert resp.json()["success_count"] == 1


# ---------------------------------------------------------------------------
# GET /api/cache/compare
# ---------------------------------------------------------------------------


class TestCompareContainerCaches:
    async def test_compare_two_containers(self, client):
        cm, rm = _mock_cache_mgr()
        rm.client.scan.return_value = (0, [])
        with patch("routers.cache.get_cache_manager", return_value=cm):
            resp = await client.get(
                "/api/cache/compare",
                params={"container_names": "app1,app2"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["comparison_count"] == 2

    async def test_compare_one_container_returns_400(self, client):
        resp = await client.get(
            "/api/cache/compare",
            params={"container_names": "app1"},
        )
        assert resp.status_code == 400

    async def test_compare_too_many_returns_400(self, client):
        names = ",".join(f"app{i}" for i in range(11))
        resp = await client.get(
            "/api/cache/compare",
            params={"container_names": names},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# GET /api/cache/evictions/{container_name}
# ---------------------------------------------------------------------------


class TestEvictionHistory:
    async def test_evictions_success(self, client):
        cm, _ = _mock_cache_mgr()
        with (
            patch("routers.cache._require_container_exists"),
            patch("routers.cache.get_cache_manager", return_value=cm),
        ):
            resp = await client.get("/api/cache/evictions/myapp")
        assert resp.status_code == 200
        body = resp.json()
        assert body["container_name"] == "myapp"
        assert "evictions" in body
        assert "summary" in body

    async def test_evictions_container_not_found(self, client):
        from fastapi import HTTPException

        with patch(
            "routers.cache._require_container_exists",
            side_effect=HTTPException(status_code=404, detail="Not found"),
        ):
            resp = await client.get("/api/cache/evictions/missing")
        assert resp.status_code == 404

    async def test_evictions_with_filters(self, client):
        cm, _ = _mock_cache_mgr()
        with (
            patch("routers.cache._require_container_exists"),
            patch("routers.cache.get_cache_manager", return_value=cm),
        ):
            resp = await client.get(
                "/api/cache/evictions/myapp",
                params={
                    "limit": 50,
                    "offset": 0,
                    "date_from": "2026-01-01T00:00:00Z",
                    "reason": "manual",
                },
            )
        assert resp.status_code == 200
        assert resp.json()["container_name"] == "myapp"


# ---------------------------------------------------------------------------
# Path-parameter naming contract for the generated n8n node
# ---------------------------------------------------------------------------


class TestCachePathParamNames:
    """The n8n node generator turns every path parameter into a node property
    of the same name. A path parameter literally named ``operation`` collides
    with n8n's own ``operation`` selector, which broke both ops below in
    n8n-nodes-coding. The wire URL is unchanged; only the placeholder name is.
    """

    def _path_params(self, op_id: str) -> list[str]:
        from fastapi import FastAPI

        from routers.cache import router

        app = FastAPI()
        app.include_router(router)
        for methods in app.openapi()["paths"].values():
            for op in methods.values():
                if op.get("operationId") == op_id:
                    return [p["name"] for p in op.get("parameters", []) if p["in"] == "path"]
        raise AssertionError(f"operationId {op_id!r} not found in the spec")

    def test_get_cache_entry_path_params(self):
        assert set(self._path_params("get_cache_entry")) == {"container_name", "cache_operation"}

    def test_update_cache_ttl_path_params(self):
        assert set(self._path_params("update_cache_ttl")) == {"container_name", "cache_operation"}

    def test_no_cache_route_declares_a_path_param_named_operation(self):
        from fastapi import FastAPI

        from routers.cache import router

        app = FastAPI()
        app.include_router(router)
        offenders = [
            (path, method)
            for path, methods in app.openapi()["paths"].items()
            for method, op in methods.items()
            for p in op.get("parameters", [])
            if p["in"] == "path" and p["name"] == "operation"
        ]
        assert offenders == []
