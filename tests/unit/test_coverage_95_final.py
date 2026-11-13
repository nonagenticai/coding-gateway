"""Final coverage push — targeting remaining gaps to reach 95%.

Files targeted:
- routers/proxy.py (73%) — WebSocket relay with mock WS
- routers/metrics.py (86%) — psutil fallback paths
- services/cache_manager.py (90%) — error paths, eviction history, preload config
- routers/validation.py (87%) — explicit codebase-path validation (the old
  `_resolve_codebase_path` and its silent default are gone; see
  TestResolveCodebasePath)
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# =====================================================================
# routers/proxy.py — WebSocket relay (lines 149-195, 206-218)
# =====================================================================


class TestProxyWebSocket:
    async def test_proxy_websocket_text_relay(self):
        from routers.proxy import _proxy_websocket

        client_ws = AsyncMock()
        client_ws.accept = AsyncMock()
        client_ws.client_state = MagicMock()
        # `_proxy_websocket` now forwards client headers + auth identity on
        # the backend handshake; the proxy code reads `client_ws.headers`
        # via `dict(...)` and `client_ws.state.user` via getattr. Stub both
        # as plain values so the AsyncMock default doesn't return coroutines.
        client_ws.headers = {}
        client_ws.state = MagicMock()
        client_ws.state.user = None
        client_ws.receive = AsyncMock(
            side_effect=[
                {"type": "websocket.receive", "text": "hello"},
                {"type": "websocket.disconnect"},
            ]
        )

        mock_backend_ws = AsyncMock()
        mock_backend_ws.send = AsyncMock()
        mock_backend_ws.__aiter__ = MagicMock(return_value=iter([]))
        mock_backend_ws.__aenter__ = AsyncMock(return_value=mock_backend_ws)
        mock_backend_ws.__aexit__ = AsyncMock(return_value=False)

        mock_connect = MagicMock(return_value=mock_backend_ws)
        with patch.dict("sys.modules", {"websockets": MagicMock(connect=mock_connect)}):
            await _proxy_websocket(client_ws, "ws://backend:8080/ws")

        client_ws.accept.assert_called_once()
        mock_backend_ws.send.assert_called_with("hello")

    async def test_proxy_websocket_bytes_relay(self):
        from routers.proxy import _proxy_websocket

        client_ws = AsyncMock()
        client_ws.accept = AsyncMock()
        client_ws.client_state = MagicMock()
        # `_proxy_websocket` now forwards client headers + auth identity on
        # the backend handshake; the proxy code reads `client_ws.headers`
        # via `dict(...)` and `client_ws.state.user` via getattr. Stub both
        # as plain values so the AsyncMock default doesn't return coroutines.
        client_ws.headers = {}
        client_ws.state = MagicMock()
        client_ws.state.user = None
        client_ws.receive = AsyncMock(
            side_effect=[
                {"type": "websocket.receive", "bytes": b"\x00\x01"},
                {"type": "websocket.disconnect"},
            ]
        )

        mock_backend_ws = AsyncMock()
        mock_backend_ws.send = AsyncMock()
        mock_backend_ws.__aiter__ = MagicMock(return_value=iter([]))
        mock_backend_ws.__aenter__ = AsyncMock(return_value=mock_backend_ws)
        mock_backend_ws.__aexit__ = AsyncMock(return_value=False)

        with patch.dict(
            "sys.modules",
            {"websockets": MagicMock(connect=MagicMock(return_value=mock_backend_ws))},
        ):
            await _proxy_websocket(client_ws, "ws://backend:8080/ws")

        mock_backend_ws.send.assert_called_with(b"\x00\x01")

    async def test_proxy_websocket_connection_error_closes_client(self):
        from starlette.websockets import WebSocketState

        from routers.proxy import _proxy_websocket

        client_ws = AsyncMock()
        client_ws.accept = AsyncMock()
        client_ws.client_state = WebSocketState.CONNECTED
        client_ws.close = AsyncMock()
        # See note above in test_proxy_websocket_text_relay.
        client_ws.headers = {}
        client_ws.state = MagicMock()
        client_ws.state.user = None

        mock_ws_mod = MagicMock()
        mock_ws_mod.connect = MagicMock(side_effect=ConnectionRefusedError("down"))
        with patch.dict("sys.modules", {"websockets": mock_ws_mod}):
            await _proxy_websocket(client_ws, "ws://backend:8080/ws")

        client_ws.close.assert_called_once_with(code=1011, reason="Backend connection failed")

    async def test_proxy_websocket_client_disconnect(self):
        from fastapi import WebSocketDisconnect

        from routers.proxy import _proxy_websocket

        client_ws = AsyncMock()
        client_ws.accept = AsyncMock()
        client_ws.client_state = MagicMock()
        # `_proxy_websocket` now forwards client headers + auth identity on
        # the backend handshake; the proxy code reads `client_ws.headers`
        # via `dict(...)` and `client_ws.state.user` via getattr. Stub both
        # as plain values so the AsyncMock default doesn't return coroutines.
        client_ws.headers = {}
        client_ws.state = MagicMock()
        client_ws.state.user = None
        client_ws.receive = AsyncMock(side_effect=WebSocketDisconnect())

        mock_backend_ws = AsyncMock()
        mock_backend_ws.close = AsyncMock()
        mock_backend_ws.__aiter__ = MagicMock(return_value=iter([]))
        mock_backend_ws.__aenter__ = AsyncMock(return_value=mock_backend_ws)
        mock_backend_ws.__aexit__ = AsyncMock(return_value=False)

        with patch.dict(
            "sys.modules",
            {"websockets": MagicMock(connect=MagicMock(return_value=mock_backend_ws))},
        ):
            await _proxy_websocket(client_ws, "ws://backend/ws")

        mock_backend_ws.close.assert_called_once()

    async def test_ws_realtime_proxy_with_query(self):
        from routers.proxy import ws_realtime_proxy

        ws = AsyncMock()
        ws.url = MagicMock()
        ws.url.query = "token=abc"
        ws.url.path = "/api/realtime/ws/test/path"
        ws.scope = {"raw_path": b"/api/realtime/ws/test/path"}

        with patch("routers.proxy._proxy_websocket", new_callable=AsyncMock) as mock_proxy:
            await ws_realtime_proxy(ws, "test/path")
            url = mock_proxy.call_args[0][1]
            assert "?token=abc" in url
            assert "/api/realtime/ws/test/path" in url

    async def test_ws_broadcast_proxy_with_query(self):
        from routers.proxy import ws_broadcast_subscribe_proxy

        ws = AsyncMock()
        ws.url = MagicMock()
        ws.url.query = "ch=1"
        ws.url.path = "/api/broadcast/subscribe"
        ws.scope = {"raw_path": b"/api/broadcast/subscribe"}

        with patch("routers.proxy._proxy_websocket", new_callable=AsyncMock) as mock_proxy:
            await ws_broadcast_subscribe_proxy(ws)
            url = mock_proxy.call_args[0][1]
            assert "?ch=1" in url
            assert "/api/broadcast/subscribe" in url


# =====================================================================
# routers/metrics.py — no-psutil fallback (lines 178-274)
# =====================================================================


class TestMetricsNoPsutil:
    async def test_metrics_without_psutil(self, client):
        import sys

        saved = sys.modules.get("psutil")
        sys.modules["psutil"] = None  # type: ignore[assignment]  # ty: ignore[invalid-assignment]
        try:
            response = await client.get("/api/health/metrics/", headers={"X-Tenant-Id": "n8n-dev"})
        finally:
            if saved is not None:
                sys.modules["psutil"] = saved
            else:
                sys.modules.pop("psutil", None)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert "metrics" in data


# =====================================================================
# services/cache_manager.py — error paths
# =====================================================================


class TestRedisCacheManagerErrors:
    def _make_manager(self):
        from services.cache_manager import RedisCacheManager

        mgr = RedisCacheManager.__new__(RedisCacheManager)
        mgr._redis = MagicMock()
        mgr.stats = {"hits": 0, "misses": 0, "errors": 0}
        mgr.get_redis = AsyncMock(return_value=mgr._redis)
        mgr.delete = AsyncMock(return_value=True)
        mgr.delete_pattern = AsyncMock(return_value=5)
        return mgr

    async def test_get_container_stats_error(self):
        mgr = self._make_manager()
        mgr._redis.client = MagicMock()
        mgr._redis.client.scan = MagicMock(side_effect=RuntimeError("redis"))
        with pytest.raises(RuntimeError):
            await mgr.get_container_stats("c1")
        assert mgr.stats["errors"] == 1

    async def test_clear_container_cache_error(self):
        mgr = self._make_manager()
        mgr.delete_pattern = AsyncMock(side_effect=RuntimeError("fail"))
        with pytest.raises(RuntimeError):
            await mgr.clear_container_cache("c1")
        assert mgr.stats["errors"] == 1

    async def test_get_container_keys_error(self):
        mgr = self._make_manager()
        mgr._redis.client = MagicMock()
        mgr._redis.client.scan = MagicMock(side_effect=RuntimeError("fail"))
        with pytest.raises(RuntimeError):
            await mgr.get_container_keys("c1")
        assert mgr.stats["errors"] == 1

    async def test_get_preload_config_error(self):
        mgr = self._make_manager()
        mgr._redis.hgetall = MagicMock(side_effect=RuntimeError("fail"))
        with pytest.raises(RuntimeError):
            await mgr.get_preload_config("c1")
        assert mgr.stats["errors"] == 1

    async def test_set_preload_config_error_returns_false(self):
        mgr = self._make_manager()
        mgr._redis.hset = MagicMock(side_effect=RuntimeError("fail"))
        result = await mgr.set_preload_config("c1", {"key": "val"})
        assert result is False
        assert mgr.stats["errors"] == 1

    async def test_delete_preload_config_error_returns_false(self):
        mgr = self._make_manager()
        mgr.delete = AsyncMock(side_effect=RuntimeError("fail"))
        result = await mgr.delete_preload_config("c1")
        assert result is False
        assert mgr.stats["errors"] == 1

    async def test_record_eviction_failure_swallowed(self):
        mgr = self._make_manager()
        mgr._redis.client = MagicMock()
        mgr._redis.client.get = MagicMock(side_effect=RuntimeError("fail"))
        mgr._redis.lpush = MagicMock(side_effect=RuntimeError("fail"))
        await mgr._record_eviction("key:1", "manual")

    async def test_get_eviction_history_error(self):
        mgr = self._make_manager()
        mgr._redis.lrange = MagicMock(side_effect=RuntimeError("fail"))
        with pytest.raises(RuntimeError):
            await mgr.get_eviction_history("c1")
        assert mgr.stats["errors"] == 1

    async def test_get_eviction_history_date_filters(self):
        """Eviction history with date range filters — lrange returns dicts."""
        mgr = self._make_manager()
        now = datetime.now(UTC).isoformat()
        # lrange returns dicts (stored via lpush as dicts)
        mgr._redis.lrange = MagicMock(
            return_value=[
                {"key": "k1", "eviction_reason": "ttl", "evicted_at": now, "size_bytes": 100}
            ]
        )
        result = await mgr.get_eviction_history(
            "c1", date_from="2020-01-01T00:00:00+00:00", date_to="2099-01-01T00:00:00+00:00"
        )
        assert result["total"] == 1
        assert result["summary"]["ttl_evictions"] == 1
        assert result["summary"]["total_size_bytes"] == 100

    async def test_get_eviction_history_invalid_date_from(self):
        mgr = self._make_manager()
        now = datetime.now(UTC).isoformat()
        mgr._redis.lrange = MagicMock(
            return_value=[{"key": "k1", "eviction_reason": "manual", "evicted_at": now}]
        )
        result = await mgr.get_eviction_history("c1", date_from="not-a-date")
        assert result["total"] >= 0

    async def test_get_eviction_history_reason_filter(self):
        mgr = self._make_manager()
        now = datetime.now(UTC).isoformat()
        mgr._redis.lrange = MagicMock(
            return_value=[
                {"key": "k1", "eviction_reason": "ttl", "evicted_at": now},
                {"key": "k2", "eviction_reason": "lru", "evicted_at": now},
            ]
        )
        result = await mgr.get_eviction_history("c1", reason_filter="ttl")
        assert result["total"] == 1
        assert result["summary"]["ttl_evictions"] == 1

    async def test_get_eviction_history_all_reasons(self):
        mgr = self._make_manager()
        now = datetime.now(UTC).isoformat()
        reasons = ["ttl", "manual", "lru", "memory_pressure", "overwrite"]
        evictions = [
            {"key": f"k{i}", "eviction_reason": r, "evicted_at": now} for i, r in enumerate(reasons)
        ]
        mgr._redis.lrange = MagicMock(return_value=evictions)
        result = await mgr.get_eviction_history("c1")
        s = result["summary"]
        assert s["ttl_evictions"] == 1
        assert s["manual_evictions"] == 1
        assert s["lru_evictions"] == 1
        assert s["memory_pressure_evictions"] == 1
        assert s["overwrite_evictions"] == 1


# =====================================================================
# services/cache_manager.py — TestingConfigurationService validation
# =====================================================================


class TestConfigValidation:
    async def test_quality_score_out_of_range(self):
        from services.cache_manager import TestingConfig, TestingConfigurationService

        svc = TestingConfigurationService.__new__(TestingConfigurationService)
        svc.cache_manager = MagicMock()
        config = TestingConfig.model_construct(
            default_timeout=-1,
            max_memory_mb=256,
            max_cpu_quota=50000,
            quality_score_threshold=11.0,
            enabled_python_tools=[],
            enabled_js_tools=[],
            enabled_ts_tools=[],
            cache_ttl_seconds=3600,
            base_images={},
        )
        result = await svc.validate_configuration(config)
        assert not result.valid

    async def test_valid_config_high_values(self):
        from services.cache_manager import TestingConfig, TestingConfigurationService

        svc = TestingConfigurationService.__new__(TestingConfigurationService)
        svc.cache_manager = MagicMock()
        config = TestingConfig.model_construct(
            default_timeout=300,
            max_memory_mb=2048,
            max_cpu_quota=100000,
            quality_score_threshold=5.0,
            enabled_python_tools=[],
            enabled_js_tools=[],
            enabled_ts_tools=[],
            cache_ttl_seconds=86400,
            base_images={"python": "python3"},
        )
        result = await svc.validate_configuration(config)
        # Should have warnings for no tools, bad base image, etc.
        assert len(result.warnings) >= 2

    async def test_short_cache_ttl_warning(self):
        from services.cache_manager import TestingConfig, TestingConfigurationService

        svc = TestingConfigurationService.__new__(TestingConfigurationService)
        svc.cache_manager = MagicMock()
        config = TestingConfig.model_construct(
            default_timeout=30,
            max_memory_mb=256,
            max_cpu_quota=50000,
            quality_score_threshold=5.0,
            enabled_python_tools=["ruff"],
            enabled_js_tools=[],
            enabled_ts_tools=[],
            cache_ttl_seconds=200,
            base_images={},
        )
        result = await svc.validate_configuration(config)
        assert any("short" in w.lower() for w in result.warnings)

    async def test_long_cache_ttl_warning(self):
        from services.cache_manager import TestingConfig, TestingConfigurationService

        svc = TestingConfigurationService.__new__(TestingConfigurationService)
        svc.cache_manager = MagicMock()
        config = TestingConfig.model_construct(
            default_timeout=30,
            max_memory_mb=256,
            max_cpu_quota=50000,
            quality_score_threshold=5.0,
            enabled_python_tools=["ruff"],
            enabled_js_tools=[],
            enabled_ts_tools=[],
            cache_ttl_seconds=86401,
            base_images={},
        )
        result = await svc.validate_configuration(config)
        assert any("long" in w.lower() for w in result.warnings)


# =====================================================================
# routers/validation.py — _resolve_codebase_path
# =====================================================================


class TestResolveCodebasePath:
    """Codebase resolution after ``_resolve_codebase_path`` was removed.

    ⚠️ Two of the three tests that used to live here asserted the DEFECT as the
    specification:

        async def test_container_name_no_workspace_falls_back_to_default(...)
            # When no workspace directories exist, falls back to _CODEBASE_PATH
        async def test_no_args_returns_default_codebase_path(...)

    That silent fallback is precisely what made every reference endpoint answer
    from the gateway's own empty filesystem and report ``valid=false`` for every
    symbol in existence. They passed, and they were guarding the bug. They are
    deleted rather than adapted, because there is no longer a default to return:
    a request that names no codebase is refused.
    """

    def test_explicit_path_is_still_honoured(self, tmp_path):
        from routers.validation import _validate_explicit_path

        assert _validate_explicit_path(str(tmp_path)) == str(tmp_path)

    def test_explicit_path_must_exist(self):
        from fastapi import HTTPException

        from routers.validation import _validate_explicit_path

        with pytest.raises(HTTPException) as exc:
            _validate_explicit_path("/custom/path")
        assert exc.value.status_code == 400
        assert "not a directory" in exc.value.detail

    @pytest.mark.parametrize("bad", ["/", "/app", "/tmp\x00injected"])
    def test_broad_or_malformed_paths_are_refused(self, bad):
        from fastapi import HTTPException

        from routers.validation import _validate_explicit_path

        with pytest.raises(HTTPException) as exc:
            _validate_explicit_path(bad)
        assert exc.value.status_code == 400

    def test_there_is_no_silent_default_anymore(self):
        """Regression guard: re-introducing either name brings the defect back."""
        import routers.validation as validation_module

        assert not hasattr(validation_module, "_resolve_codebase_path")
        assert not hasattr(validation_module, "_CODEBASE_PATH")


# =====================================================================
# services/cache_manager.py — cached() sync wrapper
# =====================================================================


class TestCachedSyncDecorator:
    async def test_sync_function_caching(self):
        from services.cache_manager import cached

        mock_redis = AsyncMock()
        mock_redis.async_get = AsyncMock(return_value=None)
        mock_redis.async_set = AsyncMock()

        def multiply(x: int, y: int) -> int:
            return x * y

        with patch("services.cache_manager.get_redis_service", return_value=mock_redis):
            decorated = cached(key_prefix="test", ttl_seconds=60)(multiply)
            result = await decorated(3, 4)
            assert result == 12
            mock_redis.async_set.assert_called_once()

    async def test_sync_function_cache_hit(self):
        from services.cache_manager import cached

        mock_redis = AsyncMock()
        mock_redis.async_get = AsyncMock(return_value=42)

        def compute(x: int) -> int:
            return x * 2  # pragma: no cover

        with patch("services.cache_manager.get_redis_service", return_value=mock_redis):
            decorated = cached(key_prefix="test")(compute)
            result = await decorated(5)
            assert result == 42
