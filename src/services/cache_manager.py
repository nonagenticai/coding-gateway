"""Module for managing caching mechanisms and utilities."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from inspect import Signature, iscoroutinefunction, signature
from typing import Any, TypeVar

from pydantic import BaseModel, Field

from config.settings import get_settings as _get_settings

# RedisService is provided by the gateway's own redis infrastructure.
# The _RedisServiceShim below wraps the ``redis`` package to match the
# interface the monolith's RedisService exposed.  If a dedicated gateway
# Redis module is introduced later, swap the shim for the real class.
logger = logging.getLogger(__name__)


def _redis_url() -> str:
    """Resolve the Redis URL from settings at call time.

    Defers Settings instantiation until first use so that import-time module
    loading (e.g. test collection) does not need ENVIRONMENT/SECRET_KEY set.
    Production boot fails loudly via Settings._REQUIRED_IN_PRODUCTION when
    REDIS_URL is missing — no code-side fallback per
    ``feedback-no-hardcoded-urls-ports-in-code``.
    """
    return _get_settings().redis_url


T = TypeVar("T")


# ---------------------------------------------------------------------------
# Lightweight Redis service shim
# ---------------------------------------------------------------------------
# The monolith used src.app.core.redis_service.RedisService.  In the gateway
# we keep the same interface but allow the concrete implementation to be
# injected at startup.  Until the gateway's own Redis module is wired up, this
# thin wrapper talks to Redis directly via the ``redis`` package.
# ---------------------------------------------------------------------------


class _RedisServiceShim:
    """Minimal Redis service matching the interface used by cache_manager."""

    def __init__(self) -> None:
        self._client = None

    @property
    def client(self):
        if self._client is None:
            import redis

            self._client = redis.Redis.from_url(_redis_url(), decode_responses=True)
        return self._client

    # -- async wrappers (run blocking redis calls in a thread) ----------------

    async def async_get(self, key: str) -> Any | None:
        value = await asyncio.to_thread(self.client.get, key)
        if value is None:
            return None
        try:
            return json.loads(value)
        except json.JSONDecodeError, TypeError:
            return value

    async def async_set(self, key: str, value: Any, ttl: int | None = None) -> bool:
        serialized = json.dumps(value) if not isinstance(value, str) else value
        if ttl:
            await asyncio.to_thread(self.client.setex, key, ttl, serialized)
        else:
            await asyncio.to_thread(self.client.set, key, serialized)
        return True

    async def async_delete(self, key: str) -> int:
        return await asyncio.to_thread(self.client.delete, key)

    async def async_scan_keys(self, pattern: str) -> list[str]:
        keys: list[str] = []
        cursor = 0
        while True:
            cursor, batch = await asyncio.to_thread(
                self.client.scan, cursor, match=pattern, count=100
            )
            keys.extend(batch)
            if cursor == 0:
                break
        return keys

    # -- sync helpers used by eviction recording -----------------------------

    def hgetall(self, key: str) -> dict:
        return self.client.hgetall(key)

    def hset(self, key: str, field: str, value: Any) -> int:
        return self.client.hset(key, field, value)

    def lpush(self, key: str, value: Any) -> int:
        serialized = json.dumps(value) if not isinstance(value, str) else value
        return self.client.lpush(key, serialized)

    def lrange(self, key: str, start: int, end: int) -> list:
        raw = self.client.lrange(key, start, end)
        results = []
        for item in raw:
            try:
                results.append(json.loads(item))
            except json.JSONDecodeError, TypeError:
                results.append(item)
        return results

    def expire(self, key: str, seconds: int) -> bool:
        return self.client.expire(key, seconds)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


# Module-level Redis service type alias for external reference
RedisService = _RedisServiceShim

_redis_service_instance: _RedisServiceShim | None = None
_redis_service_lock: asyncio.Lock = asyncio.Lock()


async def get_redis_service() -> _RedisServiceShim:
    """Get or create a singleton RedisService shim."""
    global _redis_service_instance
    if _redis_service_instance is None:
        async with _redis_service_lock:
            if _redis_service_instance is None:
                _redis_service_instance = _RedisServiceShim()
    return _redis_service_instance


# ---------------------------------------------------------------------------
# @cached decorator
# ---------------------------------------------------------------------------


def cached(key_prefix: str, ttl_seconds: int | None = None) -> Callable[[Callable], Callable]:
    """Decorator for caching async function results in Redis.

    This decorator caches the results of the decorated function in Redis.
    Works with both async and sync functions.

    Args:
        key_prefix: Prefix for the cache key
        ttl_seconds: Time-to-live in seconds (None for default TTL)

    Returns:
        Decorator function
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        """Internal decorator function that provides caching functionality.

        This function wraps the original function to add caching behavior,
        handling both synchronous and asynchronous functions.

        Args:
            func (Callable): The function to be wrapped with caching

        Returns:
            Callable: The wrapped function with caching capabilities
        """
        sig = signature(func)
        func_name = getattr(func, "__name__", repr(func))
        if iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                cache_key = _create_cache_key(key_prefix, func_name, sig, args, kwargs)
                redis_service = await get_redis_service()
                cached_result = await redis_service.async_get(cache_key)
                if cached_result is not None:
                    logger.debug(f"Cache hit for {cache_key}")
                    return cached_result
                logger.debug(f"Cache miss for {cache_key}")
                result = await func(*args, **kwargs)
                await redis_service.async_set(cache_key, result, ttl=ttl_seconds)
                return result

            return async_wrapper
        else:

            @functools.wraps(func)
            async def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                cache_key = _create_cache_key(key_prefix, func_name, sig, args, kwargs)
                redis_service = await get_redis_service()
                cached_result = await redis_service.async_get(cache_key)
                if cached_result is not None:
                    logger.debug(f"Cache hit for {cache_key}")
                    return cached_result
                logger.debug(f"Cache miss for {cache_key}")
                result = await asyncio.to_thread(func, *args, **kwargs)
                await redis_service.async_set(cache_key, result, ttl=ttl_seconds)
                return result

            return sync_wrapper

    return decorator


def _create_cache_key(
    prefix: str,
    func_name: str,
    sig: Signature,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> str:
    """Create a cache key from function name and arguments.

    Args:
        prefix: Key prefix
        func_name: Function name
        sig: Function signature
        args: Positional arguments
        kwargs: Keyword arguments

    Returns:
        Cache key string
    """
    key_parts = [prefix, func_name]
    parameters = list(sig.parameters.items())
    for i, arg in enumerate(args):
        if i < len(parameters):
            param_name = parameters[i][0]
            key_parts.append(f"{param_name}:{_arg_to_str(arg)}")
        else:
            key_parts.append(f"arg{i}:{_arg_to_str(arg)}")
    for key, value in sorted(kwargs.items()):
        key_parts.append(f"{key}:{_arg_to_str(value)}")
    return ":".join(key_parts)


def _arg_to_str(arg: Any) -> str:
    """Convert argument to string representation for cache key.

    Args:
        arg: Function argument

    Returns:
        String representation of argument
    """
    if arg is None:
        return "None"
    elif isinstance(arg, (str, int, float, bool)):
        return str(arg)
    elif isinstance(arg, (list, tuple, set)):
        return f"{type(arg).__name__}:{len(arg)}"
    elif isinstance(arg, dict):
        return f"dict:{len(arg)}"
    else:
        return f"{type(arg).__name__}"


class RedisCacheManager:
    """Cache manager for caching data with Redis."""

    def __init__(self) -> None:
        """Initialize the cache manager."""
        self._redis: _RedisServiceShim | None = None
        self.stats = {"hits": 0, "misses": 0, "errors": 0}

    async def get_redis(self) -> _RedisServiceShim:
        """Get the Redis service.

        Returns:
            Redis service
        """
        if not self._redis:
            self._redis = await get_redis_service()
        assert self._redis is not None
        return self._redis

    async def get(self, key: str) -> Any | None:
        """Get a value from cache.

        Args:
            key: Cache key

        Returns:
            Cached value or None if not found
        """
        try:
            redis = await self.get_redis()
            value = await redis.async_get(key)
            if value is not None:
                self.stats["hits"] += 1
                return value
            self.stats["misses"] += 1
            return None
        except Exception as e:
            logger.error(f"Error getting from cache: {e}")
            self.stats["errors"] += 1
            return None

    async def set(self, key: str, value: Any, ttl: int | None = None) -> bool:
        """Set a value in cache.

        Args:
            key: Cache key
            value: Value to cache
            ttl: Time-to-live in seconds

        Returns:
            True if successful, False otherwise
        """
        try:
            redis = await self.get_redis()
            return await redis.async_set(key, value, ttl=ttl)
        except Exception as e:
            logger.error(f"Error setting in cache: {e}")
            self.stats["errors"] += 1
            return False

    async def delete(
        self, key: str, record_eviction: bool = True, eviction_reason: str = "manual"
    ) -> bool:
        """Delete a value from cache.

        Args:
            key: Cache key
            record_eviction: Whether to record this deletion as an eviction
            eviction_reason: Reason for eviction (manual, ttl, lru, memory_pressure, overwrite)

        Returns:
            True if successful, False otherwise
        """
        try:
            redis = await self.get_redis()

            # Record eviction if requested
            if record_eviction:
                await self._record_eviction(key, eviction_reason)

            result = await redis.async_delete(key)
            return result > 0
        except Exception as e:
            logger.error(f"Error deleting from cache: {e}")
            self.stats["errors"] += 1
            return False

    async def delete_pattern(self, pattern: str) -> int:
        """Delete keys matching a pattern.

        Args:
            pattern: Key pattern

        Returns:
            Number of keys deleted
        """
        try:
            redis = await self.get_redis()
            keys = await redis.async_scan_keys(pattern)
            deleted_count = 0
            for key in keys:
                result = await redis.async_delete(key)
                deleted_count += result
            return deleted_count
        except (ConnectionError, TimeoutError, AttributeError) as e:
            logger.error("Error deleting pattern from cache: %s", e)
            self.stats["errors"] += 1
            return 0

    async def get_or_compute(
        self, key: str, compute_func: Callable[[], Any], ttl: int | None = None
    ) -> Any:
        """Get from cache or compute and cache.

        Args:
            key: Cache key
            compute_func: Function to compute value if not cached
            ttl: Time-to-live in seconds

        Returns:
            Cached or computed value
        """
        cached_value = await self.get(key)
        if cached_value is not None:
            return cached_value
        try:
            if iscoroutinefunction(compute_func):
                result = await compute_func()
            else:
                result = await asyncio.to_thread(compute_func)
            await self.set(key, result, ttl)
            return result
        except Exception as e:
            logger.error(f"Error computing value for cache: {e}")
            self.stats["errors"] += 1
            raise

    def get_stats(self) -> dict[str, int]:
        """Get cache statistics.

        Returns:
            Dictionary of cache stats
        """
        return self.stats

    async def clear_stats(self) -> None:
        """Reset cache statistics."""
        self.stats = {"hits": 0, "misses": 0, "errors": 0}

    async def get_container_stats(self, container_name: str) -> dict[str, Any]:
        """Get cache statistics for a specific container.

        Args:
            container_name: Name of the container to get stats for

        Returns:
            Dictionary containing:
            - hit_count: Number of cache hits (global stats)
            - miss_count: Number of cache misses (global stats)
            - size: Total size of all cached values in bytes
            - entry_count: Number of cache entries for this container

        Raises:
            ValueError: If container_name is empty
        """
        if not container_name:
            raise ValueError("container_name cannot be empty")

        try:
            redis = await self.get_redis()
            prefix = f"container:{container_name}:"
            pattern = f"{prefix}*"

            # Get global cache stats
            global_stats = self.get_stats()

            # Count entries and calculate size
            entry_count = 0
            total_size = 0

            # Use Redis SCAN command to iterate through keys
            cursor = 0
            while True:
                cursor, keys = await asyncio.to_thread(
                    redis.client.scan, cursor, match=pattern, count=100
                )
                entry_count += len(keys)

                # Calculate size for these keys
                for key in keys:
                    try:
                        value = await asyncio.to_thread(redis.client.get, key)
                        if value is not None:
                            if isinstance(value, str):
                                total_size += len(value.encode("utf-8"))
                            elif isinstance(value, bytes):
                                total_size += len(value)
                            else:
                                total_size += len(str(value).encode("utf-8"))
                    except Exception as e:
                        logger.warning(f"Error calculating size for key {key}: {e}")

                if cursor == 0:
                    break

            stats = {
                "hit_count": global_stats.get("hits", 0),
                "miss_count": global_stats.get("misses", 0),
                "size": total_size,
                "entry_count": entry_count,
                "container_name": container_name,
            }

            logger.info(
                f"Retrieved cache stats for container {container_name}: {entry_count} entries, {total_size} bytes"
            )

            return stats

        except Exception as e:
            logger.error(f"Error getting container stats for {container_name}: {e}")
            self.stats["errors"] += 1
            raise

    async def clear_container_cache(self, container_name: str) -> int:
        """Clear all cache entries for a specific container.

        Args:
            container_name: Name of the container whose cache should be cleared

        Returns:
            Number of cache entries deleted

        Raises:
            ValueError: If container_name is empty
        """
        if not container_name:
            raise ValueError("container_name cannot be empty")

        try:
            prefix = f"container:{container_name}:"
            pattern = f"{prefix}*"
            deleted_count = await self.delete_pattern(pattern)

            logger.info(f"Cleared {deleted_count} cache entries for container: {container_name}")

            return deleted_count

        except Exception as e:
            logger.error(f"Error clearing container cache for {container_name}: {e}")
            self.stats["errors"] += 1
            raise

    async def get_container_keys(
        self, container_name: str, limit: int = 100, offset: int = 0
    ) -> list[str]:
        """Get paginated list of cache keys for a specific container.

        Args:
            container_name: Name of the container to get keys for
            limit: Maximum number of keys to return (default: 100, max: 1000)
            offset: Number of keys to skip (default: 0)

        Returns:
            List of cache key strings

        Raises:
            ValueError: If container_name is empty or limit/offset are invalid
        """
        if not container_name:
            raise ValueError("container_name cannot be empty")

        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")

        if offset < 0:
            raise ValueError("offset must be non-negative")

        try:
            redis = await self.get_redis()
            prefix = f"container:{container_name}:"
            pattern = f"{prefix}*"

            # Collect all keys using SCAN
            all_keys: list[str] = []
            cursor = 0
            max_scan = offset + limit

            while len(all_keys) < max_scan:
                cursor, batch_keys = await asyncio.to_thread(
                    redis.client.scan, cursor, match=pattern, count=100
                )
                all_keys.extend(batch_keys)

                if cursor == 0:
                    break

            # Apply pagination
            paginated_keys = all_keys[offset : offset + limit]

            logger.debug(
                f"Retrieved {len(paginated_keys)} cache keys for container {container_name} "
                f"(offset: {offset}, limit: {limit})"
            )

            return paginated_keys

        except Exception as e:
            logger.error(f"Error getting container keys for {container_name}: {e}")
            self.stats["errors"] += 1
            raise

    async def get_preload_config(self, container_name: str) -> dict[str, Any] | None:
        """Get cache preload configuration for a container.

        Args:
            container_name: Name of the container

        Returns:
            Preload configuration dictionary or None if not found

        Raises:
            ValueError: If container_name is empty
        """
        if not container_name:
            raise ValueError("container_name cannot be empty")

        try:
            redis = await self.get_redis()
            config_key = f"container:{container_name}:preload:config"
            config_data = await asyncio.to_thread(redis.hgetall, config_key)

            if not config_data:
                logger.debug(f"No preload config found for container: {container_name}")
                return None

            # Convert operations from JSON string back to list
            if "operations" in config_data and isinstance(config_data["operations"], str):
                config_data["operations"] = json.loads(config_data["operations"])

            logger.debug(f"Retrieved preload config for container: {container_name}")
            return config_data

        except Exception as e:
            logger.error(f"Error getting preload config for {container_name}: {e}")
            self.stats["errors"] += 1
            raise

    async def set_preload_config(self, container_name: str, config: dict[str, Any]) -> bool:
        """Set cache preload configuration for a container.

        Args:
            container_name: Name of the container
            config: Preload configuration dictionary

        Returns:
            True if successful, False otherwise

        Raises:
            ValueError: If container_name is empty or config is invalid
        """
        if not container_name:
            raise ValueError("container_name cannot be empty")

        if not config:
            raise ValueError("config cannot be empty")

        try:
            redis = await self.get_redis()
            config_key = f"container:{container_name}:preload:config"

            # Convert operations list to JSON string for storage
            storage_config = config.copy()
            if "operations" in storage_config:
                storage_config["operations"] = json.dumps(storage_config["operations"])

            # Add timestamps
            current_time = datetime.now(UTC).isoformat()
            if "created_at" not in storage_config:
                storage_config["created_at"] = current_time
            storage_config["updated_at"] = current_time

            # Store in Redis hash
            for key, value in storage_config.items():
                await asyncio.to_thread(redis.hset, config_key, key, value)

            logger.info(f"Set preload config for container: {container_name}")
            return True

        except Exception as e:
            logger.error(f"Error setting preload config for {container_name}: {e}")
            self.stats["errors"] += 1
            return False

    async def delete_preload_config(self, container_name: str) -> bool:
        """Delete cache preload configuration for a container.

        Args:
            container_name: Name of the container

        Returns:
            True if successful, False otherwise

        Raises:
            ValueError: If container_name is empty
        """
        if not container_name:
            raise ValueError("container_name cannot be empty")

        try:
            await self.get_redis()
            config_key = f"container:{container_name}:preload:config"
            deleted = await self.delete(config_key)

            logger.info(f"Deleted preload config for container: {container_name}")
            return deleted

        except Exception as e:
            logger.error(f"Error deleting preload config for {container_name}: {e}")
            self.stats["errors"] += 1
            return False

    async def close(self) -> None:
        """Close the Redis connection."""
        if self._redis:
            # Redis service close() is synchronous, not async
            self._redis.close()
            self._redis = None

    async def _record_eviction(
        self,
        key: str,
        reason: str,
        size_bytes: int | None = None,
        ttl_remaining: int | None = None,
    ) -> None:
        """Record a cache eviction event.

        Args:
            key: Cache key that was evicted
            reason: Reason for eviction (manual, ttl, lru, memory_pressure, overwrite)
            size_bytes: Size of evicted value in bytes (will be calculated if None)
            ttl_remaining: TTL remaining at eviction time
        """
        try:
            redis = await self.get_redis()

            # Extract container name and operation from key
            # Expected format: container:{container_name}:{operation}:{args_hash}
            container_name = None
            operation = None
            if key.startswith("container:"):
                parts = key.split(":", 3)
                if len(parts) >= 3:
                    container_name = parts[1]
                    operation = parts[2]

            # Calculate size if not provided
            if size_bytes is None:
                try:
                    value = await asyncio.to_thread(redis.client.get, key)
                    if value is not None:
                        if isinstance(value, str):
                            size_bytes = len(value.encode("utf-8"))
                        elif isinstance(value, bytes):
                            size_bytes = len(value)
                        else:
                            size_bytes = len(str(value).encode("utf-8"))
                except Exception as e:
                    logger.debug(f"Could not calculate size for evicted key {key}: {e}")

            # Get TTL if not provided and reason is not TTL
            if ttl_remaining is None and reason != "ttl":
                try:
                    ttl_remaining = await asyncio.to_thread(redis.client.ttl, key)
                    if ttl_remaining in (-2, -1):  # Key doesn't exist or no TTL
                        ttl_remaining = None
                except Exception as e:
                    logger.debug(f"Could not get TTL for evicted key {key}: {e}")

            # Build eviction record
            eviction_record = {
                "key": key,
                "eviction_reason": reason,
                "evicted_at": datetime.now(UTC).isoformat(),
                "operation": operation,
                "size_bytes": size_bytes,
                "ttl_remaining": ttl_remaining,
            }

            # Store eviction record in Redis list
            # Use container-specific list if container name is available
            if container_name:
                eviction_list_key = f"container:{container_name}:evictions"
            else:
                eviction_list_key = "cache:evictions:global"

            # Add to list (most recent first)
            await asyncio.to_thread(redis.lpush, eviction_list_key, eviction_record)

            # Keep only last 10000 eviction records per container
            await asyncio.to_thread(redis.client.ltrim, eviction_list_key, 0, 9999)

            # Set TTL on eviction list (30 days)
            await asyncio.to_thread(redis.expire, eviction_list_key, 30 * 24 * 60 * 60)

            logger.debug(
                f"Recorded eviction: key={key}, reason={reason}, container={container_name}"
            )

        except Exception as e:
            # Don't fail the deletion if eviction recording fails
            logger.warning(f"Failed to record eviction for key {key}: {e}")

    async def get_eviction_history(
        self,
        container_name: str,
        limit: int = 100,
        offset: int = 0,
        date_from: str | None = None,
        date_to: str | None = None,
        reason_filter: str | None = None,
    ) -> dict[str, Any]:
        """Get cache eviction history for a container.

        Args:
            container_name: Name of the container
            limit: Maximum number of records to return (1-1000)
            offset: Number of records to skip
            date_from: Start of date range filter (ISO 8601)
            date_to: End of date range filter (ISO 8601)
            reason_filter: Filter by eviction reason

        Returns:
            Dictionary containing:
            - evictions: List of eviction records
            - total: Total number of matching records
            - summary: Summary statistics

        Raises:
            ValueError: If parameters are invalid
        """
        if not container_name:
            raise ValueError("container_name cannot be empty")

        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")

        if offset < 0:
            raise ValueError("offset must be non-negative")

        try:
            redis = await self.get_redis()
            eviction_list_key = f"container:{container_name}:evictions"

            # Get all evictions (we'll filter in memory for now)
            # In a production system, you might want to use a more sophisticated storage
            all_evictions_raw = await asyncio.to_thread(redis.lrange, eviction_list_key, 0, -1)

            # Parse date filters
            date_from_dt = None
            date_to_dt = None
            if date_from:
                try:
                    date_from_dt = datetime.fromisoformat(date_from.replace("Z", "+00:00"))
                except Exception as e:
                    logger.warning(f"Invalid date_from format: {e}")

            if date_to:
                try:
                    date_to_dt = datetime.fromisoformat(date_to.replace("Z", "+00:00"))
                except Exception as e:
                    logger.warning(f"Invalid date_to format: {e}")

            # Filter evictions
            filtered_evictions = []
            summary = {
                "ttl_evictions": 0,
                "manual_evictions": 0,
                "lru_evictions": 0,
                "memory_pressure_evictions": 0,
                "overwrite_evictions": 0,
                "total_size_bytes": 0,
            }

            for eviction_raw in all_evictions_raw:
                eviction = eviction_raw if isinstance(eviction_raw, dict) else eviction_raw

                # Apply date filters
                if date_from_dt or date_to_dt:
                    try:
                        evicted_at = datetime.fromisoformat(
                            eviction["evicted_at"].replace("Z", "+00:00")
                        )
                        if date_from_dt and evicted_at < date_from_dt:
                            continue
                        if date_to_dt and evicted_at > date_to_dt:
                            continue
                    except Exception as e:
                        logger.debug(f"Error parsing eviction date: {e}")
                        continue

                # Apply reason filter
                if reason_filter and eviction.get("eviction_reason") != reason_filter:
                    continue

                filtered_evictions.append(eviction)

                # Update summary
                reason = eviction.get("eviction_reason", "unknown")
                if reason == "ttl":
                    summary["ttl_evictions"] += 1
                elif reason == "manual":
                    summary["manual_evictions"] += 1
                elif reason == "lru":
                    summary["lru_evictions"] += 1
                elif reason == "memory_pressure":
                    summary["memory_pressure_evictions"] += 1
                elif reason == "overwrite":
                    summary["overwrite_evictions"] += 1

                if eviction.get("size_bytes"):
                    summary["total_size_bytes"] += eviction["size_bytes"]

            # Apply pagination
            total = len(filtered_evictions)
            paginated_evictions = filtered_evictions[offset : offset + limit]

            logger.info(
                f"Retrieved {len(paginated_evictions)} eviction records for container "
                f"'{container_name}' (total: {total})"
            )

            return {
                "evictions": paginated_evictions,
                "total": total,
                "summary": summary,
            }

        except Exception as e:
            logger.error(f"Error getting eviction history for {container_name}: {e}")
            self.stats["errors"] += 1
            raise


class TestingConfig(BaseModel):
    """Configuration for testing and quality analysis tools."""

    default_timeout: int = Field(
        default=30, ge=1, le=300, description="Default test timeout in seconds"
    )
    max_memory_mb: int = Field(
        default=512, ge=128, le=2048, description="Maximum memory per container in MB"
    )
    max_cpu_quota: int = Field(
        default=50000,
        ge=10000,
        le=100000,
        description="Maximum CPU quota (100000 = 1 CPU)",
    )
    network_isolation: bool = Field(
        default=True, description="Enable network isolation for containers"
    )
    readonly_filesystem: bool = Field(
        default=True, description="Use read-only filesystem for containers"
    )
    enable_security_scanning: bool = Field(
        default=True, description="Enable security scanning of code"
    )
    python_version: str = Field(default="3.11", description="Python version to use")
    node_version: str = Field(default="18", description="Node.js version to use")
    enabled_python_tools: list[str] = Field(
        default=["ruff", "ruff-format", "ty", "bandit"],
        description="Enabled Python quality tools",
    )
    enabled_js_tools: list[str] = Field(
        default=["eslint", "prettier"], description="Enabled JavaScript quality tools"
    )
    enabled_ts_tools: list[str] = Field(
        default=["eslint", "prettier", "tsc"],
        description="Enabled TypeScript quality tools",
    )
    quality_score_threshold: float = Field(
        default=7.0, ge=0.0, le=10.0, description="Minimum quality score threshold"
    )
    auto_fix_enabled: bool = Field(
        default=False, description="Enable automatic resolving of issues"
    )
    fail_on_security_issues: bool = Field(
        default=True, description="Fail analysis on security issues"
    )
    cache_test_results: bool = Field(default=True, description="Cache test execution results")
    cache_quality_results: bool = Field(default=True, description="Cache quality analysis results")
    cache_ttl_seconds: int = Field(
        default=3600, ge=300, le=86400, description="Cache TTL in seconds"
    )
    cleanup_containers: bool = Field(
        default=True, description="Automatically cleanup containers after execution"
    )
    container_registry: str = Field(default="docker.io", description="Container registry to use")
    base_images: dict[str, str] = Field(
        default={
            "python": "code_graph_helper-api:latest",
            "javascript": "node:18-slim",
            "typescript": "node:18-slim",
        },
        description="Base container images for each language",
    )


class ConfigValidationResult(BaseModel):
    """Result of configuration validation."""

    valid: bool = Field(description="Whether configuration is valid")
    errors: list[str] = Field(default=[], description="Validation errors")
    warnings: list[str] = Field(default=[], description="Validation warnings")
    validated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When validation was performed",
    )


class ToolConfig(BaseModel):
    """Configuration for a specific quality tool."""

    name: str = Field(description="Tool name")
    enabled: bool = Field(default=True, description="Whether tool is enabled")
    timeout: int = Field(default=30, ge=5, le=300, description="Tool execution timeout")
    command_args: list[str] = Field(default=[], description="Additional command line arguments")
    config_file: str | None = Field(None, description="Path to tool config file")
    environment: dict[str, str] = Field(default={}, description="Environment variables")
    severity_threshold: str = Field(default="warning", description="Minimum severity to report")
    ignore_patterns: list[str] = Field(default=[], description="Patterns to ignore")
    install_command: str | None = Field(None, description="Custom installation command")
    version: str | None = Field(None, description="Specific tool version")
    output_format: str = Field(default="json", description="Output format (json, text, xml)")
    include_context: bool = Field(default=True, description="Include code context in output")


class TestingConfigurationService:
    """Manages testing and quality configuration with validation."""

    def __init__(self, cache_manager: RedisCacheManager):
        """Initialize the TestingConfigurationService service.

        Args:
            cache_manager ('RedisCacheManager'): Cache manager
        """
        self.cache_manager = cache_manager
        self.config_cache_key = "testing:config:default"
        self.tool_config_cache_prefix = "testing:tool_config"
        self._default_config: TestingConfig | None = None

    async def get_testing_config(self) -> TestingConfig:
        """Get the current testing configuration."""
        try:
            cached_config = await self.cache_manager.get(self.config_cache_key)
            if cached_config:
                logger.debug("Retrieved testing config from cache")
                return TestingConfig(**cached_config)
            config = self._get_default_config()
            await self.cache_manager.set(
                self.config_cache_key, config.model_dump(), ttl=config.cache_ttl_seconds
            )
            logger.info("Using default testing configuration")
            return config
        except Exception as e:
            logger.error(f"Failed to get testing config: {e}")
            return self._get_default_config()

    async def update_testing_config(self, config: TestingConfig) -> bool:
        """Update the testing configuration."""
        try:
            validation = await self.validate_configuration(config)
            if not validation.valid:
                logger.error(f"Invalid configuration: {validation.errors}")
                return False
            success = await self.cache_manager.set(
                self.config_cache_key, config.model_dump(), ttl=config.cache_ttl_seconds
            )
            if success:
                logger.info("Successfully updated testing configuration")
                await self.cache_manager.delete_pattern(f"{self.tool_config_cache_prefix}:*")
            else:
                logger.error("Failed to update testing configuration in cache")
            return success
        except Exception as e:
            logger.error(f"Failed to update testing config: {e}")
            return False

    async def validate_configuration(self, config: TestingConfig) -> ConfigValidationResult:
        """Validate testing configuration."""
        errors = []
        warnings = []
        try:
            if config.default_timeout <= 0:
                errors.append("Default timeout must be positive")
            elif config.default_timeout > 300:
                warnings.append("Default timeout is very high (>300s)")
            if config.max_memory_mb < 128:
                errors.append("Minimum memory is 128MB")
            elif config.max_memory_mb > 2048:
                warnings.append("High memory limit may impact system performance")
            if config.max_cpu_quota <= 0:
                errors.append("CPU quota must be positive")
            elif config.max_cpu_quota > 100000:
                warnings.append("CPU quota exceeds 1 full CPU")
            if not 0.0 <= config.quality_score_threshold <= 10.0:
                errors.append("Quality score threshold must be between 0.0 and 10.0")
            all_tools = (
                config.enabled_python_tools + config.enabled_js_tools + config.enabled_ts_tools
            )
            if not all_tools:
                warnings.append("No quality tools enabled")
            for lang, image in config.base_images.items():
                if not image or ":" not in image:
                    warnings.append(f"Base image for {lang} may be invalid: {image}")
            if config.cache_ttl_seconds < 300:
                warnings.append("Cache TTL is very short (<5 minutes)")
            elif config.cache_ttl_seconds > 86400:
                warnings.append("Cache TTL is very long (>24 hours)")
            logger.debug(
                f"Configuration validation: {len(errors)} errors, {len(warnings)} warnings"
            )
            return ConfigValidationResult(valid=len(errors) == 0, errors=errors, warnings=warnings)
        except Exception as e:
            logger.error(f"Configuration validation failed: {e}")
            return ConfigValidationResult(
                valid=False, errors=[f"Validation error: {str(e)}"], warnings=warnings
            )

    async def get_quality_tool_config(self, tool: str) -> ToolConfig:
        """Get configuration for a specific quality tool."""
        try:
            cache_key = f"{self.tool_config_cache_prefix}:{tool}"
            cached_config = await self.cache_manager.get(cache_key)
            if cached_config:
                logger.debug(f"Retrieved tool config for {tool} from cache")
                return ToolConfig(**cached_config)
            config = self._get_default_tool_config(tool)
            main_config = await self.get_testing_config()
            await self.cache_manager.set(
                cache_key, config.model_dump(), ttl=main_config.cache_ttl_seconds
            )
            logger.debug(f"Generated default config for tool: {tool}")
            return config
        except Exception as e:
            logger.error(f"Failed to get tool config for {tool}: {e}")
            return ToolConfig(name=tool, config_file=None, install_command=None, version=None)

    async def update_quality_tool_config(self, tool: str, config: ToolConfig) -> bool:
        """Update configuration for a specific quality tool."""
        try:
            cache_key = f"{self.tool_config_cache_prefix}:{tool}"
            main_config = await self.get_testing_config()
            success = await self.cache_manager.set(
                cache_key, config.model_dump(), ttl=main_config.cache_ttl_seconds
            )
            if success:
                logger.info(f"Updated configuration for tool: {tool}")
            else:
                logger.error(f"Failed to update configuration for tool: {tool}")
            return success
        except Exception as e:
            logger.error(f"Failed to update tool config for {tool}: {e}")
            return False

    async def get_enabled_tools_for_language(self, language: str) -> list[str]:
        """Get list of enabled quality tools for a language."""
        try:
            config = await self.get_testing_config()
            language = language.lower()
            if language == "python":
                return config.enabled_python_tools
            elif language == "javascript":
                return config.enabled_js_tools
            elif language == "typescript":
                return config.enabled_ts_tools
            else:
                logger.warning(f"Unknown language: {language}")
                return []
        except Exception as e:
            logger.error(f"Failed to get enabled tools for {language}: {e}")
            return []

    async def is_tool_enabled(self, tool: str, language: str) -> bool:
        """Check if a specific tool is enabled for a language."""
        try:
            enabled_tools = await self.get_enabled_tools_for_language(language)
            return tool in enabled_tools
        except Exception as e:
            logger.error(f"Failed to check if tool {tool} is enabled for {language}: {e}")
            return False

    async def clear_configuration_cache(self) -> bool:
        """Clear all cached configuration data."""
        try:
            await self.cache_manager.delete(self.config_cache_key)
            deleted_count = await self.cache_manager.delete_pattern(
                f"{self.tool_config_cache_prefix}:*"
            )
            logger.info(f"Cleared configuration cache: {deleted_count + 1} items")
            return True
        except Exception as e:
            logger.error(f"Failed to clear configuration cache: {e}")
            return False

    def _get_default_config(self) -> TestingConfig:
        """Get the default testing configuration."""
        if self._default_config is None:
            self._default_config = TestingConfig()
        return self._default_config

    def _get_default_tool_config(self, tool: str) -> ToolConfig:
        """Get default configuration for a specific tool."""
        return ToolConfig(name=tool, config_file=None, install_command=None, version=None)


_cache_manager = None
_cache_manager_lock = None


def get_cache_manager_sync() -> RedisCacheManager:
    """Get or create a RedisCacheManager singleton (synchronous version).

    Returns:
        RedisCacheManager instance
    """
    global _cache_manager
    if _cache_manager is None:
        _cache_manager = RedisCacheManager()
    return _cache_manager


async def get_cache_manager() -> RedisCacheManager:
    """Get or create a RedisCacheManager singleton (async version).

    Returns:
        RedisCacheManager instance
    """
    global _cache_manager, _cache_manager_lock
    if _cache_manager_lock is None:
        _cache_manager_lock = asyncio.Lock()
    if _cache_manager is None:
        async with _cache_manager_lock:
            if _cache_manager is None:
                _cache_manager = RedisCacheManager()
    return _cache_manager
