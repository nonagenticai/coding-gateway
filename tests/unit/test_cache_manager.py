"""Tests for services.cache_manager — RedisCacheManager."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from services.cache_manager import RedisCacheManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_redis_shim():
    """Return a mock that quacks like _RedisServiceShim."""
    shim = AsyncMock()
    shim.async_get = AsyncMock(return_value=None)
    shim.async_set = AsyncMock(return_value=True)
    shim.async_delete = AsyncMock(return_value=1)
    shim.async_scan_keys = AsyncMock(return_value=[])
    shim.hgetall = MagicMock(return_value={})
    shim.hset = MagicMock(return_value=1)
    shim.lpush = MagicMock(return_value=1)
    shim.lrange = MagicMock(return_value=[])
    shim.expire = MagicMock(return_value=True)
    shim.close = MagicMock()
    # client sub-object used by some internals
    shim.client = MagicMock()
    shim.client.get = MagicMock(return_value=None)
    shim.client.ttl = MagicMock(return_value=-2)
    shim.client.scan = MagicMock(return_value=(0, []))
    shim.client.ltrim = MagicMock()
    return shim


@pytest.fixture
def redis_shim():
    return _make_redis_shim()


@pytest.fixture
def cm(redis_shim):
    """Return a RedisCacheManager wired to a mock Redis shim."""
    manager = RedisCacheManager()
    manager._redis = redis_shim
    return manager


# ---------------------------------------------------------------------------
# get: hit / miss
# ---------------------------------------------------------------------------


async def test_get_cache_hit(cm, redis_shim):
    redis_shim.async_get.return_value = {"data": 1}
    result = await cm.get("my-key")
    assert result == {"data": 1}
    assert cm.stats["hits"] == 1
    assert cm.stats["misses"] == 0


async def test_get_cache_miss(cm, redis_shim):
    redis_shim.async_get.return_value = None
    result = await cm.get("missing")
    assert result is None
    assert cm.stats["misses"] == 1
    assert cm.stats["hits"] == 0


# ---------------------------------------------------------------------------
# set with TTL
# ---------------------------------------------------------------------------


async def test_set_with_ttl(cm, redis_shim):
    ok = await cm.set("k", "v", ttl=60)
    assert ok is True
    redis_shim.async_set.assert_awaited_once_with("k", "v", ttl=60)


async def test_set_without_ttl(cm, redis_shim):
    ok = await cm.set("k", "v")
    assert ok is True
    redis_shim.async_set.assert_awaited_once_with("k", "v", ttl=None)


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


async def test_delete_existing_key(cm, redis_shim):
    redis_shim.async_delete.return_value = 1
    ok = await cm.delete("k", record_eviction=False)
    assert ok is True


async def test_delete_missing_key(cm, redis_shim):
    redis_shim.async_delete.return_value = 0
    ok = await cm.delete("nope", record_eviction=False)
    assert ok is False


# ---------------------------------------------------------------------------
# bulk delete (delete_pattern)
# ---------------------------------------------------------------------------


async def test_delete_pattern(cm, redis_shim):
    redis_shim.async_scan_keys.return_value = ["a:1", "a:2"]
    redis_shim.async_delete.return_value = 1
    count = await cm.delete_pattern("a:*")
    assert count == 2


async def test_delete_pattern_no_keys(cm, redis_shim):
    redis_shim.async_scan_keys.return_value = []
    count = await cm.delete_pattern("no:*")
    assert count == 0


# ---------------------------------------------------------------------------
# clear (clear_container_cache)
# ---------------------------------------------------------------------------


async def test_clear_container_cache(cm, redis_shim):
    redis_shim.async_scan_keys.return_value = ["container:c:1"]
    redis_shim.async_delete.return_value = 1
    count = await cm.clear_container_cache("c")
    assert count == 1


async def test_clear_container_cache_empty_name_raises(cm):
    with pytest.raises(ValueError, match="container_name cannot be empty"):
        await cm.clear_container_cache("")


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------


async def test_get_stats_initial(cm):
    stats = cm.get_stats()
    assert stats == {"hits": 0, "misses": 0, "errors": 0}


async def test_get_stats_after_operations(cm, redis_shim):
    redis_shim.async_get.return_value = "v"
    await cm.get("k")
    redis_shim.async_get.return_value = None
    await cm.get("k2")
    stats = cm.get_stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1


async def test_clear_stats(cm, redis_shim):
    redis_shim.async_get.return_value = "v"
    await cm.get("k")
    await cm.clear_stats()
    assert cm.get_stats() == {"hits": 0, "misses": 0, "errors": 0}


# ---------------------------------------------------------------------------
# redis unavailable fallback (get returns None, set returns False, errors++)
# ---------------------------------------------------------------------------


async def test_get_redis_unavailable(cm, redis_shim):
    redis_shim.async_get.side_effect = ConnectionError("down")
    result = await cm.get("k")
    assert result is None
    assert cm.stats["errors"] == 1


async def test_set_redis_unavailable(cm, redis_shim):
    redis_shim.async_set.side_effect = ConnectionError("down")
    ok = await cm.set("k", "v")
    assert ok is False
    assert cm.stats["errors"] == 1


async def test_delete_redis_unavailable(cm, redis_shim):
    redis_shim.async_delete.side_effect = ConnectionError("down")
    ok = await cm.delete("k", record_eviction=False)
    assert ok is False
    assert cm.stats["errors"] == 1


# ---------------------------------------------------------------------------
# serialization failures (json edge cases)
# ---------------------------------------------------------------------------


async def test_set_serializable_dict(cm, redis_shim):
    ok = await cm.set("k", {"nested": [1, 2]})
    assert ok is True


async def test_get_returns_deserialized_dict(cm, redis_shim):
    redis_shim.async_get.return_value = {"nested": True}
    result = await cm.get("k")
    assert result == {"nested": True}


# ---------------------------------------------------------------------------
# redis exceptions during delete_pattern
# ---------------------------------------------------------------------------


async def test_delete_pattern_connection_error(cm, redis_shim):
    redis_shim.async_scan_keys.side_effect = ConnectionError("nope")
    count = await cm.delete_pattern("p:*")
    assert count == 0
    assert cm.stats["errors"] == 1


# ---------------------------------------------------------------------------
# namespace (container keys)
# ---------------------------------------------------------------------------


async def test_get_container_keys(cm, redis_shim):
    redis_shim.client.scan.return_value = (0, ["container:c:a", "container:c:b"])
    keys = await cm.get_container_keys("c", limit=10, offset=0)
    assert len(keys) == 2


async def test_get_container_keys_empty_name_raises(cm):
    with pytest.raises(ValueError):
        await cm.get_container_keys("")


async def test_get_container_keys_bad_limit(cm):
    with pytest.raises(ValueError, match="limit must be between"):
        await cm.get_container_keys("c", limit=0)


async def test_get_container_keys_bad_offset(cm):
    with pytest.raises(ValueError, match="offset must be non-negative"):
        await cm.get_container_keys("c", limit=10, offset=-1)


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


async def test_close(cm, redis_shim):
    await cm.close()
    redis_shim.close.assert_called_once()
    assert cm._redis is None


async def test_close_when_not_connected():
    mgr = RedisCacheManager()
    await mgr.close()  # should not raise


# ---------------------------------------------------------------------------
# get_or_compute
# ---------------------------------------------------------------------------


async def test_get_or_compute_cached(cm, redis_shim):
    redis_shim.async_get.return_value = "cached"
    result = await cm.get_or_compute("k", lambda: "fresh")
    assert result == "cached"


async def test_get_or_compute_computes(cm, redis_shim):
    redis_shim.async_get.return_value = None

    async def compute():
        return "fresh"

    result = await cm.get_or_compute("k", compute, ttl=30)
    assert result == "fresh"
    redis_shim.async_set.assert_awaited_once()


async def test_get_or_compute_error_propagates(cm, redis_shim):
    redis_shim.async_get.return_value = None

    async def bad():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await cm.get_or_compute("k", bad)
    assert cm.stats["errors"] == 1


# ---------------------------------------------------------------------------
# container stats
# ---------------------------------------------------------------------------


async def test_get_container_stats(cm, redis_shim):
    redis_shim.client.scan.return_value = (0, ["container:c:x"])
    redis_shim.client.get.return_value = "hello"
    stats = await cm.get_container_stats("c")
    assert stats["entry_count"] == 1
    assert stats["container_name"] == "c"


async def test_get_container_stats_empty_name_raises(cm):
    with pytest.raises(ValueError, match="container_name cannot be empty"):
        await cm.get_container_stats("")


# ---------------------------------------------------------------------------
# preload config
# ---------------------------------------------------------------------------


async def test_set_and_get_preload_config(cm, redis_shim):
    redis_shim.hgetall.return_value = {"enabled": "true"}
    cfg = await cm.get_preload_config("c")
    assert cfg == {"enabled": "true"}


async def test_get_preload_config_not_found(cm, redis_shim):
    redis_shim.hgetall.return_value = {}
    cfg = await cm.get_preload_config("c")
    assert cfg is None


async def test_set_preload_config(cm, redis_shim):
    ok = await cm.set_preload_config("c", {"enabled": True})
    assert ok is True


async def test_set_preload_config_empty_name_raises(cm):
    with pytest.raises(ValueError):
        await cm.set_preload_config("", {"a": 1})


async def test_set_preload_config_empty_config_raises(cm):
    with pytest.raises(ValueError, match="config cannot be empty"):
        await cm.set_preload_config("c", {})


async def test_delete_preload_config(cm, redis_shim):
    redis_shim.async_delete.return_value = 1
    ok = await cm.delete_preload_config("c")
    assert ok is True


async def test_delete_preload_config_empty_name_raises(cm):
    with pytest.raises(ValueError):
        await cm.delete_preload_config("")


# ---------------------------------------------------------------------------
# eviction history
# ---------------------------------------------------------------------------


async def test_get_eviction_history(cm, redis_shim):
    redis_shim.lrange.return_value = [
        {"key": "k", "eviction_reason": "manual", "evicted_at": "2025-01-01T00:00:00+00:00"},
    ]
    result = await cm.get_eviction_history("c", limit=10)
    assert result["total"] == 1
    assert result["summary"]["manual_evictions"] == 1


async def test_get_eviction_history_empty_name_raises(cm):
    with pytest.raises(ValueError):
        await cm.get_eviction_history("")


async def test_get_eviction_history_bad_limit(cm):
    with pytest.raises(ValueError, match="limit must be between"):
        await cm.get_eviction_history("c", limit=0)


async def test_get_eviction_history_bad_offset(cm):
    with pytest.raises(ValueError, match="offset must be non-negative"):
        await cm.get_eviction_history("c", limit=10, offset=-1)


async def test_get_eviction_history_reason_filter(cm, redis_shim):
    redis_shim.lrange.return_value = [
        {"key": "k1", "eviction_reason": "ttl", "evicted_at": "2025-01-01T00:00:00+00:00"},
        {"key": "k2", "eviction_reason": "manual", "evicted_at": "2025-01-01T00:00:00+00:00"},
    ]
    result = await cm.get_eviction_history("c", limit=10, reason_filter="ttl")
    assert result["total"] == 1
    assert result["summary"]["ttl_evictions"] == 1
