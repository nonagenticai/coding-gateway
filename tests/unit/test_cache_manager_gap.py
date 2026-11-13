"""Tests for services/cache_manager.py — unit tests for the cache manager,
Redis shim, @cached decorator, and TestingConfigurationService.

Uses mocked Redis client to avoid needing a real Redis instance.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# _RedisServiceShim — async/sync helpers
# ---------------------------------------------------------------------------


class TestRedisServiceShim:
    """Tests for the lightweight Redis shim wrapper."""

    def _make_shim(self, mock_client):
        with patch("redis.Redis.from_url", return_value=mock_client):
            from services.cache_manager import _RedisServiceShim

            shim = _RedisServiceShim()
            _ = shim.client  # trigger lazy init
        return shim

    async def test_async_get_returns_deserialized_json(self):
        mock_client = MagicMock()
        mock_client.get.return_value = '{"a": 1}'
        shim = self._make_shim(mock_client)
        result = await shim.async_get("key1")
        assert result == {"a": 1}

    async def test_async_get_returns_none_for_missing_key(self):
        mock_client = MagicMock()
        mock_client.get.return_value = None
        shim = self._make_shim(mock_client)
        result = await shim.async_get("key1")
        assert result is None

    async def test_async_get_returns_raw_string_on_json_decode_error(self):
        mock_client = MagicMock()
        mock_client.get.return_value = "not-json"
        shim = self._make_shim(mock_client)
        result = await shim.async_get("key1")
        assert result == "not-json"

    async def test_async_set_with_ttl_uses_setex(self):
        mock_client = MagicMock()
        shim = self._make_shim(mock_client)
        ok = await shim.async_set("k", {"val": 1}, ttl=60)
        assert ok is True
        mock_client.setex.assert_called_once()
        # Verify the serialized value is valid JSON
        _, ttl_arg, serialized = mock_client.setex.call_args[0]
        assert ttl_arg == 60
        assert json.loads(serialized) == {"val": 1}

    async def test_async_set_without_ttl_uses_set(self):
        mock_client = MagicMock()
        shim = self._make_shim(mock_client)
        ok = await shim.async_set("k", "v")
        assert ok is True
        mock_client.set.assert_called_once_with("k", "v")

    async def test_async_set_string_value_not_double_serialized(self):
        mock_client = MagicMock()
        shim = self._make_shim(mock_client)
        ok = await shim.async_set("k", "plain-string")
        assert ok is True
        mock_client.set.assert_called_once_with("k", "plain-string")

    async def test_async_delete_returns_count(self):
        mock_client = MagicMock()
        mock_client.delete.return_value = 1
        shim = self._make_shim(mock_client)
        result = await shim.async_delete("k")
        assert result == 1

    async def test_async_scan_keys_single_batch(self):
        mock_client = MagicMock()
        mock_client.scan.side_effect = [(0, ["k1", "k2"])]
        shim = self._make_shim(mock_client)
        keys = await shim.async_scan_keys("prefix:*")
        assert keys == ["k1", "k2"]

    async def test_async_scan_keys_multiple_batches(self):
        mock_client = MagicMock()
        mock_client.scan.side_effect = [
            (42, ["k1"]),
            (0, ["k2"]),
        ]
        shim = self._make_shim(mock_client)
        keys = await shim.async_scan_keys("prefix:*")
        assert keys == ["k1", "k2"]

    def test_hgetall_delegates_to_client(self):
        mock_client = MagicMock()
        mock_client.hgetall.return_value = {"field": "value"}
        shim = self._make_shim(mock_client)
        assert shim.hgetall("k") == {"field": "value"}

    def test_hset_delegates_to_client(self):
        mock_client = MagicMock()
        mock_client.hset.return_value = 1
        shim = self._make_shim(mock_client)
        assert shim.hset("k", "f", "v") == 1

    def test_lpush_serializes_dict_to_json(self):
        mock_client = MagicMock()
        mock_client.lpush.return_value = 1
        shim = self._make_shim(mock_client)
        assert shim.lpush("k", {"a": 1}) == 1
        call_args = mock_client.lpush.call_args
        assert json.loads(call_args[0][1]) == {"a": 1}

    def test_lpush_passes_string_through(self):
        mock_client = MagicMock()
        mock_client.lpush.return_value = 1
        shim = self._make_shim(mock_client)
        shim.lpush("k", "raw")
        mock_client.lpush.assert_called_once_with("k", "raw")

    def test_lrange_deserializes_json_items(self):
        mock_client = MagicMock()
        mock_client.lrange.return_value = ['{"a":1}', "notjson"]
        shim = self._make_shim(mock_client)
        result = shim.lrange("k", 0, -1)
        assert result == [{"a": 1}, "notjson"]

    def test_expire_delegates_to_client(self):
        mock_client = MagicMock()
        mock_client.expire.return_value = True
        shim = self._make_shim(mock_client)
        assert shim.expire("k", 300) is True

    def test_close_closes_client_and_clears_reference(self):
        mock_client = MagicMock()
        shim = self._make_shim(mock_client)
        shim.close()
        mock_client.close.assert_called_once()
        assert shim._client is None

    def test_close_when_no_client_does_not_raise(self):
        from services.cache_manager import _RedisServiceShim

        shim = _RedisServiceShim()
        shim.close()  # should not raise


# ---------------------------------------------------------------------------
# get_redis_service singleton
# ---------------------------------------------------------------------------


class TestGetRedisService:
    async def test_returns_singleton_instance(self):
        import services.cache_manager as cm

        old = cm._redis_service_instance
        cm._redis_service_instance = None
        try:
            svc1 = await cm.get_redis_service()
            svc2 = await cm.get_redis_service()
            assert svc1 is svc2
        finally:
            cm._redis_service_instance = old


# ---------------------------------------------------------------------------
# @cached decorator
# ---------------------------------------------------------------------------


class TestCachedDecorator:
    async def test_cache_hit_returns_cached_value(self):
        mock_redis = MagicMock()
        mock_redis.async_get = AsyncMock(return_value=42)
        mock_redis.async_set = AsyncMock()

        with patch("services.cache_manager.get_redis_service", return_value=mock_redis):
            from services.cache_manager import cached

            @cached("test_prefix", ttl_seconds=60)
            async def my_func(x: int) -> int:
                return x * 2

            result = await my_func(5)
            assert result == 42
            mock_redis.async_get.assert_called_once()
            mock_redis.async_set.assert_not_called()

    async def test_cache_miss_computes_and_stores(self):
        mock_redis = MagicMock()
        mock_redis.async_get = AsyncMock(return_value=None)
        mock_redis.async_set = AsyncMock()

        with patch("services.cache_manager.get_redis_service", return_value=mock_redis):
            from services.cache_manager import cached

            @cached("test_prefix", ttl_seconds=60)
            async def my_func(x: int) -> int:
                return x * 2

            result = await my_func(5)
            assert result == 10
            mock_redis.async_set.assert_called_once()

    async def test_sync_function_is_awaitable_after_wrapping(self):
        mock_redis = MagicMock()
        mock_redis.async_get = AsyncMock(return_value=None)
        mock_redis.async_set = AsyncMock()

        with patch("services.cache_manager.get_redis_service", return_value=mock_redis):
            from services.cache_manager import cached

            @cached("sync_prefix")
            def my_sync_func(a: str) -> str:
                return a.upper()

            result = await my_sync_func("hello")
            assert result == "HELLO"


# ---------------------------------------------------------------------------
# Cache key helpers
# ---------------------------------------------------------------------------


class TestCacheKeyHelpers:
    def test_arg_to_str_none(self):
        from services.cache_manager import _arg_to_str

        assert _arg_to_str(None) == "None"

    def test_arg_to_str_primitives(self):
        from services.cache_manager import _arg_to_str

        assert _arg_to_str("hi") == "hi"
        assert _arg_to_str(42) == "42"
        assert _arg_to_str(3.14) == "3.14"
        assert _arg_to_str(True) == "True"

    def test_arg_to_str_list(self):
        from services.cache_manager import _arg_to_str

        assert _arg_to_str([1, 2]) == "list:2"

    def test_arg_to_str_tuple(self):
        from services.cache_manager import _arg_to_str

        assert _arg_to_str((1,)) == "tuple:1"

    def test_arg_to_str_set(self):
        from services.cache_manager import _arg_to_str

        assert _arg_to_str({1, 2, 3}) == "set:3"

    def test_arg_to_str_dict(self):
        from services.cache_manager import _arg_to_str

        assert _arg_to_str({"a": 1}) == "dict:1"

    def test_arg_to_str_object_includes_class_name(self):
        from services.cache_manager import _arg_to_str

        class Foo:
            pass

        assert "Foo" in _arg_to_str(Foo())


# ---------------------------------------------------------------------------
# RedisCacheManager
# ---------------------------------------------------------------------------


class TestRedisCacheManager:
    def _make_manager(self):
        from services.cache_manager import RedisCacheManager

        mgr = RedisCacheManager()
        mock_redis = MagicMock()
        mock_redis.async_get = AsyncMock()
        mock_redis.async_set = AsyncMock()
        mock_redis.async_delete = AsyncMock()
        mock_redis.async_scan_keys = AsyncMock()
        mock_redis.hgetall = MagicMock()
        mock_redis.hset = MagicMock()
        mock_redis.lpush = MagicMock()
        mock_redis.lrange = MagicMock()
        mock_redis.expire = MagicMock()
        mock_redis.client = MagicMock()
        mgr._redis = mock_redis
        return mgr, mock_redis

    async def test_get_hit_increments_hit_counter(self):
        mgr, redis = self._make_manager()
        redis.async_get.return_value = "cached_value"
        result = await mgr.get("k")
        assert result == "cached_value"
        assert mgr.stats["hits"] == 1
        assert mgr.stats["misses"] == 0

    async def test_get_miss_increments_miss_counter(self):
        mgr, redis = self._make_manager()
        redis.async_get.return_value = None
        result = await mgr.get("k")
        assert result is None
        assert mgr.stats["misses"] == 1
        assert mgr.stats["hits"] == 0

    async def test_get_error_increments_error_counter(self):
        mgr, redis = self._make_manager()
        redis.async_get.side_effect = Exception("fail")
        result = await mgr.get("k")
        assert result is None
        assert mgr.stats["errors"] == 1

    async def test_set_success(self):
        mgr, redis = self._make_manager()
        redis.async_set.return_value = True
        ok = await mgr.set("k", "v", ttl=60)
        assert ok is True
        redis.async_set.assert_called_once_with("k", "v", ttl=60)

    async def test_set_error_returns_false(self):
        mgr, redis = self._make_manager()
        redis.async_set.side_effect = Exception("fail")
        ok = await mgr.set("k", "v")
        assert ok is False
        assert mgr.stats["errors"] == 1

    async def test_delete_returns_true_on_success(self):
        mgr, redis = self._make_manager()
        redis.async_delete.return_value = 1
        redis.client.get.return_value = "data"
        redis.client.ttl.return_value = 100
        redis.client.ltrim = MagicMock()
        ok = await mgr.delete("container:myapp:analysis:abc123")
        assert ok is True

    async def test_delete_without_eviction_recording(self):
        mgr, redis = self._make_manager()
        redis.async_delete.return_value = 1
        ok = await mgr.delete("k", record_eviction=False)
        assert ok is True

    async def test_delete_error_returns_false(self):
        mgr, redis = self._make_manager()
        redis.async_delete.side_effect = Exception("fail")
        ok = await mgr.delete("k", record_eviction=False)
        assert ok is False
        assert mgr.stats["errors"] == 1

    async def test_delete_pattern_deletes_all_matched_keys(self):
        mgr, redis = self._make_manager()
        redis.async_scan_keys.return_value = ["k1", "k2"]
        redis.async_delete.return_value = 1
        count = await mgr.delete_pattern("prefix:*")
        assert count == 2

    async def test_delete_pattern_error_returns_zero(self):
        mgr, redis = self._make_manager()
        redis.async_scan_keys.side_effect = ConnectionError("down")
        count = await mgr.delete_pattern("prefix:*")
        assert count == 0

    async def test_get_or_compute_returns_cached_on_hit(self):
        mgr, redis = self._make_manager()
        redis.async_get.return_value = "cached"
        result = await mgr.get_or_compute("k", lambda: "computed")
        assert result == "cached"

    async def test_get_or_compute_calls_async_func_on_miss(self):
        mgr, redis = self._make_manager()
        redis.async_get.return_value = None
        redis.async_set.return_value = True

        async def compute():
            return "async_result"

        result = await mgr.get_or_compute("k", compute)
        assert result == "async_result"

    async def test_get_or_compute_calls_sync_func_on_miss(self):
        mgr, redis = self._make_manager()
        redis.async_get.return_value = None
        redis.async_set.return_value = True
        result = await mgr.get_or_compute("k", lambda: "sync_result")
        assert result == "sync_result"

    async def test_get_or_compute_propagates_compute_error(self):
        mgr, redis = self._make_manager()
        redis.async_get.return_value = None

        async def fail():
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            await mgr.get_or_compute("k", fail)

    def test_get_stats_returns_current_counters(self):
        mgr, _ = self._make_manager()
        mgr.stats = {"hits": 3, "misses": 1, "errors": 0}
        stats = mgr.get_stats()
        assert stats == {"hits": 3, "misses": 1, "errors": 0}

    async def test_clear_stats_resets_counters(self):
        mgr, _ = self._make_manager()
        mgr.stats = {"hits": 10, "misses": 5, "errors": 2}
        await mgr.clear_stats()
        assert mgr.stats == {"hits": 0, "misses": 0, "errors": 0}

    async def test_get_container_stats_calculates_size(self):
        mgr, redis = self._make_manager()
        redis.client.scan.side_effect = [(0, ["k1"])]
        redis.client.get.return_value = "hello"
        stats = await mgr.get_container_stats("myapp")
        assert stats["container_name"] == "myapp"
        assert stats["entry_count"] == 1
        assert stats["size"] == len(b"hello")

    async def test_get_container_stats_empty_name_raises(self):
        mgr, _ = self._make_manager()
        with pytest.raises(ValueError, match="cannot be empty"):
            await mgr.get_container_stats("")

    async def test_get_container_stats_handles_bytes_value(self):
        mgr, redis = self._make_manager()
        redis.client.scan.side_effect = [(0, ["k1"])]
        redis.client.get.return_value = b"binary"
        stats = await mgr.get_container_stats("myapp")
        assert stats["size"] == len(b"binary")

    async def test_clear_container_cache(self):
        mgr, redis = self._make_manager()
        redis.async_scan_keys.return_value = ["k1", "k2"]
        redis.async_delete.return_value = 1
        count = await mgr.clear_container_cache("myapp")
        assert count == 2

    async def test_clear_container_cache_empty_name_raises(self):
        mgr, _ = self._make_manager()
        with pytest.raises(ValueError, match="cannot be empty"):
            await mgr.clear_container_cache("")

    async def test_get_container_keys_with_limit(self):
        mgr, redis = self._make_manager()
        redis.client.scan.side_effect = [(0, ["k1", "k2", "k3"])]
        keys = await mgr.get_container_keys("myapp", limit=2, offset=0)
        assert len(keys) == 2

    async def test_get_container_keys_invalid_limit_raises(self):
        mgr, _ = self._make_manager()
        with pytest.raises(ValueError, match="limit must be"):
            await mgr.get_container_keys("myapp", limit=0)

    async def test_get_container_keys_negative_offset_raises(self):
        mgr, _ = self._make_manager()
        with pytest.raises(ValueError, match="offset must be"):
            await mgr.get_container_keys("myapp", offset=-1)

    async def test_get_preload_config_returns_parsed_operations(self):
        mgr, redis = self._make_manager()
        redis.hgetall.return_value = {
            "operations": '["analyze", "graph"]',
            "enabled": "true",
        }
        config = await mgr.get_preload_config("myapp")
        assert config is not None
        assert config["operations"] == ["analyze", "graph"]

    async def test_get_preload_config_returns_none_when_missing(self):
        mgr, redis = self._make_manager()
        redis.hgetall.return_value = {}
        result = await mgr.get_preload_config("myapp")
        assert result is None

    async def test_get_preload_config_empty_name_raises(self):
        mgr, _ = self._make_manager()
        with pytest.raises(ValueError):
            await mgr.get_preload_config("")

    async def test_set_preload_config(self):
        mgr, redis = self._make_manager()
        ok = await mgr.set_preload_config("myapp", {"operations": ["analyze"], "enabled": True})
        assert ok is True

    async def test_set_preload_config_empty_config_raises(self):
        mgr, _ = self._make_manager()
        with pytest.raises(ValueError, match="config cannot be empty"):
            await mgr.set_preload_config("myapp", {})

    async def test_delete_preload_config(self):
        mgr, redis = self._make_manager()
        redis.async_delete.return_value = 1
        redis.client.get.return_value = None
        redis.client.ttl.return_value = -2
        redis.client.ltrim = MagicMock()
        ok = await mgr.delete_preload_config("myapp")
        assert ok is True

    async def test_delete_preload_config_empty_name_raises(self):
        mgr, _ = self._make_manager()
        with pytest.raises(ValueError):
            await mgr.delete_preload_config("")

    async def test_close_closes_redis_and_clears(self):
        mgr, redis = self._make_manager()
        await mgr.close()
        redis.close.assert_called_once()
        assert mgr._redis is None

    async def test_close_when_no_redis_is_safe(self):
        from services.cache_manager import RedisCacheManager

        mgr = RedisCacheManager()
        await mgr.close()  # should not raise


# ---------------------------------------------------------------------------
# Eviction recording and history
# ---------------------------------------------------------------------------


class TestEvictionRecording:
    def _make_manager(self):
        from services.cache_manager import RedisCacheManager

        mgr = RedisCacheManager()
        mock_redis = MagicMock()
        mock_redis.async_get = AsyncMock()
        mock_redis.async_set = AsyncMock()
        mock_redis.async_delete = AsyncMock()
        mock_redis.async_scan_keys = AsyncMock()
        mock_redis.hgetall = MagicMock()
        mock_redis.hset = MagicMock()
        mock_redis.lpush = MagicMock()
        mock_redis.lrange = MagicMock()
        mock_redis.expire = MagicMock()
        mock_redis.client = MagicMock()
        mgr._redis = mock_redis
        return mgr, mock_redis

    async def test_record_eviction_for_container_key(self):
        mgr, redis = self._make_manager()
        redis.client.get.return_value = '{"data": true}'
        redis.client.ttl.return_value = 42
        redis.client.ltrim = MagicMock()
        await mgr._record_eviction("container:app:op:hash", "manual")
        redis.lpush.assert_called_once()
        redis.client.ltrim.assert_called_once()
        redis.expire.assert_called_once()

    async def test_record_eviction_for_global_key(self):
        mgr, redis = self._make_manager()
        redis.client.get.return_value = None
        redis.client.ttl.return_value = -2
        redis.client.ltrim = MagicMock()
        await mgr._record_eviction("some:random:key", "lru")
        redis.lpush.assert_called_once()
        call_args = redis.lpush.call_args[0]
        assert call_args[0] == "cache:evictions:global"

    async def test_record_eviction_ttl_reason_skips_ttl_fetch(self):
        mgr, redis = self._make_manager()
        redis.client.get.return_value = b"bytes_data"
        redis.client.ltrim = MagicMock()
        await mgr._record_eviction("container:app:op:h", "ttl", size_bytes=100)
        redis.client.ttl.assert_not_called()

    async def test_record_eviction_error_does_not_propagate(self):
        mgr, redis = self._make_manager()
        redis.client.get.side_effect = Exception("fail")
        redis.client.ltrim = MagicMock()
        # Should not raise
        await mgr._record_eviction("k", "manual")

    async def test_get_eviction_history_counts_and_totals(self):
        mgr, redis = self._make_manager()
        redis.lrange.return_value = [
            {
                "key": "k1",
                "eviction_reason": "manual",
                "evicted_at": datetime.now(UTC).isoformat(),
                "size_bytes": 100,
            },
            {
                "key": "k2",
                "eviction_reason": "ttl",
                "evicted_at": datetime.now(UTC).isoformat(),
                "size_bytes": 200,
            },
        ]
        result = await mgr.get_eviction_history("myapp")
        assert result["total"] == 2
        assert result["summary"]["manual_evictions"] == 1
        assert result["summary"]["ttl_evictions"] == 1
        assert result["summary"]["total_size_bytes"] == 300

    async def test_get_eviction_history_with_reason_filter(self):
        mgr, redis = self._make_manager()
        redis.lrange.return_value = [
            {
                "key": "k1",
                "eviction_reason": "manual",
                "evicted_at": datetime.now(UTC).isoformat(),
            },
            {
                "key": "k2",
                "eviction_reason": "lru",
                "evicted_at": datetime.now(UTC).isoformat(),
            },
        ]
        result = await mgr.get_eviction_history("myapp", reason_filter="lru")
        assert result["total"] == 1

    async def test_get_eviction_history_empty_name_raises(self):
        mgr, _ = self._make_manager()
        with pytest.raises(ValueError):
            await mgr.get_eviction_history("")

    async def test_get_eviction_history_invalid_limit_raises(self):
        mgr, _ = self._make_manager()
        with pytest.raises(ValueError, match="limit"):
            await mgr.get_eviction_history("app", limit=0)

    async def test_get_eviction_history_date_from_filter(self):
        mgr, redis = self._make_manager()
        redis.lrange.return_value = [
            {
                "key": "k1",
                "eviction_reason": "manual",
                "evicted_at": "2026-01-15T00:00:00+00:00",
            },
        ]
        result = await mgr.get_eviction_history(
            "myapp",
            date_from="2026-01-01T00:00:00Z",
            date_to="2026-02-01T00:00:00Z",
        )
        assert result["total"] == 1

    async def test_get_eviction_history_date_filter_excludes_old(self):
        mgr, redis = self._make_manager()
        redis.lrange.return_value = [
            {
                "key": "k1",
                "eviction_reason": "manual",
                "evicted_at": "2025-01-15T00:00:00+00:00",
            },
        ]
        result = await mgr.get_eviction_history(
            "myapp",
            date_from="2026-01-01T00:00:00Z",
        )
        assert result["total"] == 0

    async def test_get_eviction_history_pagination(self):
        mgr, redis = self._make_manager()
        redis.lrange.return_value = [
            {
                "key": f"k{i}",
                "eviction_reason": "manual",
                "evicted_at": datetime.now(UTC).isoformat(),
            }
            for i in range(5)
        ]
        result = await mgr.get_eviction_history("myapp", limit=2, offset=1)
        assert len(result["evictions"]) == 2
        assert result["total"] == 5

    async def test_get_eviction_history_all_reason_types(self):
        mgr, redis = self._make_manager()
        redis.lrange.return_value = [
            {
                "key": "k1",
                "eviction_reason": "memory_pressure",
                "evicted_at": datetime.now(UTC).isoformat(),
            },
            {
                "key": "k2",
                "eviction_reason": "overwrite",
                "evicted_at": datetime.now(UTC).isoformat(),
            },
        ]
        result = await mgr.get_eviction_history("myapp")
        assert result["summary"]["memory_pressure_evictions"] == 1
        assert result["summary"]["overwrite_evictions"] == 1


# ---------------------------------------------------------------------------
# TestingConfigurationService
# ---------------------------------------------------------------------------


class TestTestingConfigurationService:
    def _make_service(self):
        from services.cache_manager import RedisCacheManager, TestingConfigurationService

        mgr = RedisCacheManager()
        mock_redis = MagicMock()
        mock_redis.async_get = AsyncMock(return_value=None)
        mock_redis.async_set = AsyncMock(return_value=True)
        mock_redis.async_delete = AsyncMock(return_value=1)
        mock_redis.async_scan_keys = AsyncMock(return_value=[])
        mock_redis.client = MagicMock()
        mock_redis.client.get.return_value = None
        mock_redis.client.ttl.return_value = -2
        mock_redis.client.ltrim = MagicMock()
        mgr._redis = mock_redis
        svc = TestingConfigurationService(mgr)
        return svc, mgr, mock_redis

    async def test_get_testing_config_returns_defaults(self):
        svc, _, _ = self._make_service()
        config = await svc.get_testing_config()
        assert config.default_timeout == 30
        assert config.max_memory_mb == 512

    async def test_get_testing_config_returns_cached_values(self):
        svc, _, redis = self._make_service()
        redis.async_get.return_value = {"default_timeout": 60, "max_memory_mb": 1024}
        config = await svc.get_testing_config()
        assert config.default_timeout == 60
        assert config.max_memory_mb == 1024

    async def test_update_testing_config(self):
        from services.cache_manager import TestingConfig

        svc, _, redis = self._make_service()
        redis.async_get.return_value = None
        redis.async_set.return_value = True
        config = TestingConfig(default_timeout=45)
        ok = await svc.update_testing_config(config)
        assert ok is True

    async def test_validate_configuration_valid(self):
        from services.cache_manager import TestingConfig

        svc, _, _ = self._make_service()
        config = TestingConfig()
        result = await svc.validate_configuration(config)
        assert result.valid is True

    async def test_validate_configuration_warns_on_no_tools(self):
        from services.cache_manager import TestingConfig

        svc, _, _ = self._make_service()
        config = TestingConfig(
            enabled_python_tools=[],
            enabled_js_tools=[],
            enabled_ts_tools=[],
        )
        result = await svc.validate_configuration(config)
        assert result.valid is True
        assert any("No quality tools" in w for w in result.warnings)

    async def test_get_quality_tool_config_default(self):
        svc, _, redis = self._make_service()
        redis.async_get.return_value = None
        config = await svc.get_quality_tool_config("ruff")
        assert config.name == "ruff"

    async def test_get_quality_tool_config_cached(self):
        svc, _, redis = self._make_service()
        redis.async_get.return_value = {"name": "ruff", "enabled": True, "timeout": 30}
        config = await svc.get_quality_tool_config("ruff")
        assert config.name == "ruff"

    async def test_update_quality_tool_config(self):
        from services.cache_manager import ToolConfig

        svc, _, redis = self._make_service()
        redis.async_get.return_value = None
        redis.async_set.return_value = True
        config = ToolConfig(name="ruff")
        ok = await svc.update_quality_tool_config("ruff", config)
        assert ok is True

    async def test_get_enabled_tools_python(self):
        svc, _, redis = self._make_service()
        redis.async_get.return_value = None
        tools = await svc.get_enabled_tools_for_language("python")
        assert tools == ["ruff", "ruff-format", "ty", "bandit"]

    async def test_get_enabled_tools_javascript(self):
        svc, _, redis = self._make_service()
        redis.async_get.return_value = None
        tools = await svc.get_enabled_tools_for_language("javascript")
        assert "eslint" in tools

    async def test_get_enabled_tools_typescript(self):
        svc, _, redis = self._make_service()
        redis.async_get.return_value = None
        tools = await svc.get_enabled_tools_for_language("typescript")
        assert "tsc" in tools

    async def test_get_enabled_tools_unknown_language(self):
        svc, _, redis = self._make_service()
        redis.async_get.return_value = None
        tools = await svc.get_enabled_tools_for_language("ruby")
        assert tools == []

    async def test_is_tool_enabled(self):
        svc, _, redis = self._make_service()
        redis.async_get.return_value = None
        assert await svc.is_tool_enabled("ty", "python") is True
        assert await svc.is_tool_enabled("unknown_tool", "python") is False

    async def test_clear_configuration_cache(self):
        svc, _, redis = self._make_service()
        redis.async_delete.return_value = 1
        redis.async_scan_keys.return_value = []
        ok = await svc.clear_configuration_cache()
        assert ok is True


# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------


class TestModuleSingletons:
    def test_get_cache_manager_sync_returns_singleton(self):
        import services.cache_manager as cm

        old = cm._cache_manager
        cm._cache_manager = None
        try:
            mgr = cm.get_cache_manager_sync()
            assert mgr is not None
            mgr2 = cm.get_cache_manager_sync()
            assert mgr is mgr2
        finally:
            cm._cache_manager = old

    async def test_get_cache_manager_async_returns_singleton(self):
        import services.cache_manager as cm

        old_mgr = cm._cache_manager
        old_lock = cm._cache_manager_lock
        cm._cache_manager = None
        cm._cache_manager_lock = None
        try:
            mgr = await cm.get_cache_manager()
            assert mgr is not None
            mgr2 = await cm.get_cache_manager()
            assert mgr is mgr2
        finally:
            cm._cache_manager = old_mgr
            cm._cache_manager_lock = old_lock
