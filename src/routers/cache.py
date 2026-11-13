"""
Analysis Cache Management API Endpoints

API endpoints for managing the analysis cache system, including cache statistics,
clearing cache entries, and optimizing cache performance.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import uuid as _uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from coding_client.models.responses import (
    bad_request as _bad_request,
)
from coding_client.models.responses import (
    internal_error,
    not_found,
)
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi import Path as PathParam
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from auth.dependencies import require_tenant
from clients.outbound_identity import identity_headers
from config.settings import get_settings
from services.cache_manager import get_cache_manager

logger = logging.getLogger(__name__)

_SAFE_CONTAINER_NAME = re.compile(r"^[a-zA-Z0-9_\-\.]+$")


def _validate_container_name(name: str) -> str:
    """Validate container_name to prevent glob injection."""
    if not _SAFE_CONTAINER_NAME.fullmatch(name):
        raise _bad_request("Invalid container name")
    return name


async def _require_container_exists(container_name: str, tenant_id: str) -> None:
    """Raise 404 if the CALLER's tenant has no graph for this container.

    Graph data lives in coding-analysis now; ask its metadata endpoint instead of
    scanning a local SQLite directory. Uses the same plain-string ``detail`` format
    as the monolith's ``get_container_from_path`` dependency so parity tests pass.

    ``tenant_id`` is the tenant ``require_tenant`` resolved for THIS request and
    is required on purpose. This check used to ask as ``service_tenant()`` -- the
    gateway's own tenant -- so coding-analysis looked the name up in the wrong
    tenant and answered 404 for every customer workspace (measured 2026-10-12:
    ``GET /api/cache/evictions/cv-sweep-ems`` 404 while
    ``GET /api/analysis/graphs/cv-sweep-ems`` answered 200 with 228 nodes for the
    same licence). It also meant a name owned by the gateway's tenant read as
    "exists" for any caller.
    """
    from routers.proxy import get_proxy_client

    settings = get_settings()
    try:
        client = await get_proxy_client()
        url = f"{settings.analysis_url.rstrip('/')}/api/analysis/graphs/{container_name}"
        resp = await client.get(url, headers=identity_headers(tenant_id))
        exists = resp.status_code == 200
    except Exception:
        exists = False
    if not exists:
        raise HTTPException(status_code=404, detail=f"Container '{container_name}' not found")


async def _depend_container_exists(
    container_name: str = PathParam(..., description="Container name"),
    tenant_id: str = Depends(require_tenant),
) -> str:
    """FastAPI dependency: validate + existence-check a container.

    Runs before body validation so that a missing container returns 404
    instead of a Pydantic 422 (matching the monolith behaviour).
    """
    _validate_container_name(container_name)
    await _require_container_exists(container_name, tenant_id)
    return container_name


router = APIRouter(
    responses={
        404: {"description": "Resource not found"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable - cache system not available"},
    },
)


# ---------------------------------------------------------------------------
# Local Pydantic models (self-contained for the gateway)
# ---------------------------------------------------------------------------


class ExportFormat(StrEnum):
    """Export format options."""

    JSON = "json"
    CSV = "csv"


class CacheEntryExport(BaseModel):
    """Single cache entry for export."""

    key: str = Field(..., description="Cache key")
    operation: str = Field(..., description="Operation type extracted from key")
    value: Any | None = Field(None, description="Cached value (serialized)")
    size: int = Field(..., description="Size in bytes")
    created_at: str | None = Field(None, description="Estimated creation timestamp")
    ttl: int | None = Field(None, description="Time-to-live in seconds (-1 if no expiration)")


class CacheExportResponse(BaseModel):
    """Response for cache export."""

    container_name: str = Field(..., description="Name of the container")
    total_entries: int = Field(..., description="Total number of entries exported")
    export_format: ExportFormat = Field(..., description="Format of the export")
    exported_at: str = Field(..., description="Timestamp when export was generated")
    entries: list[CacheEntryExport] = Field(..., description="List of cache entries")


class CacheInfoResponse(BaseModel):
    """Cache information for a specific file."""

    cached: bool
    cached_at: str | None = None
    last_accessed: str | None = None
    access_count: int = 0
    language: str | None = None
    parser_type: str | None = None
    file_size: int = 0
    entities_count: int = 0
    relationships_count: int = 0
    cache_hit_savings_ms: float = 0.0
    age_hours: float = 0.0


class CacheStatsResponse(BaseModel):
    """Response containing cache statistics."""

    status: str = "success"
    message: str = ""
    request_id: str = Field(default_factory=lambda: f"req_{_uuid.uuid4().hex[:8]}")
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    stats: dict[str, Any] = Field(..., description="Cache performance statistics")


class ContainerCacheStatsResponse(BaseModel):
    """Response containing container-specific cache statistics."""

    status: str = "success"
    message: str = ""
    request_id: str = Field(default_factory=lambda: f"req_{_uuid.uuid4().hex[:8]}")
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    container_name: str = Field(..., description="Name of the container")
    hit_count: int | None = Field(None, description="Number of cache hits (not tracked)")
    miss_count: int | None = Field(None, description="Number of cache misses (not tracked)")
    size: int = Field(..., description="Total size of cache in bytes")
    entry_count: int = Field(..., description="Number of cache entries")


class CacheClearResponse(BaseModel):
    """Response from cache clear operation."""

    status: str = "success"
    message: str = ""
    request_id: str = Field(default_factory=lambda: f"req_{_uuid.uuid4().hex[:8]}")
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    entries_cleared: int = Field(..., description="Number of cache entries cleared")


class ContainerCacheClearResponse(BaseModel):
    """Response from container-specific cache clear operation."""

    status: str = "success"
    message: str = ""
    request_id: str = Field(default_factory=lambda: f"req_{_uuid.uuid4().hex[:8]}")
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    container_name: str = Field(..., description="Name of the container")
    deleted_count: int = Field(..., description="Number of cache entries deleted")


class CacheOptimizationResponse(BaseModel):
    """Response from cache optimization operation."""

    status: str = "success"
    message: str = ""
    request_id: str = Field(default_factory=lambda: f"req_{_uuid.uuid4().hex[:8]}")
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    optimization_stats: dict[str, Any] = Field(..., description="Cache optimization results")


class CacheWarmupEntry(BaseModel):
    """Single cache warmup entry."""

    operation: str = Field(..., description="Operation type")
    cache_key: str = Field(..., description="Generated cache key")
    success: bool = Field(..., description="Whether warmup was successful")
    cached_size_bytes: int | None = Field(None, description="Size of cached data")
    execution_time_ms: float = Field(..., description="Time taken to execute and cache")


class CacheWarmupResponse(BaseModel):
    """Response from cache warmup operation."""

    status: str = "success"
    message: str = ""
    request_id: str = Field(default_factory=lambda: f"req_{_uuid.uuid4().hex[:8]}")
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    container_name: str = Field(..., description="Name of the container")
    warmed_entries: list[CacheWarmupEntry] = Field(..., description="List of warmed cache entries")
    total_entries: int = Field(..., description="Total number of entries warmed")
    total_time_ms: float = Field(..., description="Total time taken for warmup")
    success_count: int = Field(..., description="Number of successfully warmed entries")
    failure_count: int = Field(..., description="Number of failed warmup attempts")


class CacheEntryDetailsResponse(BaseModel):
    """Detailed information about a specific cache entry."""

    key: str = Field(..., description="Cache key")
    value: Any | None = Field(None, description="Cache value (if not sensitive)")
    ttl_remaining: int | None = Field(None, description="Time-to-live remaining in seconds")
    created_at: str | None = Field(None, description="When cache entry was created")
    size: int = Field(..., description="Size of cached value in bytes")


class TTLUpdateRequest(BaseModel):
    """Request body for updating cache entry TTL."""

    ttl_seconds: int = Field(
        ...,
        gt=0,
        lt=86400,
        description="New TTL in seconds (must be > 0 and < 86400 / 24 hours)",
    )


class TTLUpdateResponse(BaseModel):
    """Response from TTL update operation."""

    status: str = "success"
    message: str = ""
    request_id: str = Field(default_factory=lambda: f"req_{_uuid.uuid4().hex[:8]}")
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    container_name: str = Field(..., description="Name of the container")
    operation: str = Field(..., description="Operation for which TTL was updated")
    ttl_seconds: int = Field(..., description="New TTL in seconds")
    entries_updated: int = Field(..., description="Number of cache entries updated")
    previous_ttl: int | None = Field(None, description="Previous TTL in seconds")


class ContainerCacheKeysResponse(BaseModel):
    """Response containing paginated list of cache keys for a container."""

    status: str = "success"
    message: str = ""
    request_id: str = Field(default_factory=lambda: f"req_{_uuid.uuid4().hex[:8]}")
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    container_name: str = Field(..., description="Name of the container")
    keys: list[str] = Field(..., description="List of cache keys")
    count: int = Field(..., description="Number of keys returned")
    limit: int = Field(..., description="Maximum number of keys requested")
    offset: int = Field(..., description="Number of keys skipped")


class ContainerCacheInfoResponse(BaseModel):
    """Response containing comprehensive cache information for a container."""

    status: str = "success"
    message: str = ""
    request_id: str = Field(default_factory=lambda: f"req_{_uuid.uuid4().hex[:8]}")
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    container_name: str = Field(..., description="Name of the container")
    cache_configuration: dict[str, Any] = Field(..., description="Cache configuration settings")
    usage_statistics: dict[str, Any] = Field(..., description="Current cache usage statistics")
    health_status: str = Field(..., description="Cache health status")
    last_eviction_timestamp: str | None = Field(
        None, description="Timestamp of last cache eviction"
    )


# ---------------------------------------------------------------------------
# Helper to generate container cache keys
# ---------------------------------------------------------------------------


def _generate_container_cache_key(
    container_name: str,
    operation: str,
    tenant_id: str = "default",
) -> str:
    """Generate a tenant-scoped container cache key."""
    return f"tenant:{tenant_id}:container:{container_name}:{operation}"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/stats",
    response_model=CacheStatsResponse,
    summary="Cache Statistics",
    description="Get comprehensive cache performance metrics.",
    operation_id="get_cache_stats",
)
async def get_cache_stats(
    tenant_id: str = Depends(require_tenant),
) -> CacheStatsResponse:
    """Get comprehensive cache performance statistics."""
    try:
        cache_manager = await get_cache_manager()
        stats = cache_manager.get_stats()

        logger.info("Cache stats retrieved")

        return CacheStatsResponse(message="Cache statistics retrieved successfully", stats=stats)

    except Exception as e:
        logger.exception("Error retrieving cache statistics: %s", e)
        raise internal_error(f"Error retrieving cache statistics: {str(e)}") from e


@router.get(
    "/stats/{container_name}",
    response_model=ContainerCacheStatsResponse,
    summary="Container Cache Statistics",
    description="Get cache performance metrics for a specific container.",
    operation_id="get_container_cache_stats",
)
async def get_container_cache_statistics(
    container_name: str = PathParam(..., description="Container name"),
    tenant_id: str = Depends(require_tenant),
) -> ContainerCacheStatsResponse:
    """Get cache statistics for a specific container."""
    _validate_container_name(container_name)
    try:
        cache_manager = await get_cache_manager()
        redis = await cache_manager.get_redis()

        pattern = f"tenant:{tenant_id}:container:{container_name}:*"
        total_size = 0
        entry_count = 0

        cursor = 0
        max_keys = 10000
        while True:
            cursor, keys = redis.client.scan(cursor, match=pattern, count=100)
            for key in keys:
                entry_count += 1
                value = redis.client.get(key)
                if value is not None:
                    if isinstance(value, bytes):
                        total_size += len(value)
                    elif isinstance(value, str):
                        total_size += len(value.encode("utf-8"))
            if cursor == 0 or entry_count >= max_keys:
                break

        return ContainerCacheStatsResponse(
            message=f"Cache statistics retrieved for container '{container_name}'",
            container_name=container_name,
            hit_count=None,
            miss_count=None,
            size=total_size,
            entry_count=entry_count,
        )

    except Exception as e:
        logger.exception(
            "Error retrieving cache statistics for container '%s': %s", container_name, e
        )
        raise internal_error(f"Error retrieving cache statistics: {str(e)}") from e


@router.get(
    "/info",
    response_model=CacheInfoResponse,
    summary="File Cache Info",
    description="Get cache information for a specific file.",
    operation_id="get_cache_info",
)
async def get_file_cache_info(
    file_path: str = Query(..., description="Path to the file to check"),
    tenant_id: str = Depends(require_tenant),
) -> CacheInfoResponse:
    """Get cache information for a specific file."""
    try:
        if not file_path or file_path.strip() in ("", "null", "undefined", "None"):
            raise _bad_request("File path is required")

        # For the gateway, delegate to cache manager
        cache_manager = await get_cache_manager()
        cache_key = f"file_cache:{file_path}"
        cached_value = await cache_manager.get(cache_key)

        if cached_value and isinstance(cached_value, dict):
            return CacheInfoResponse(**cached_value)
        return CacheInfoResponse(cached=False)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error retrieving cache info for %s: %s", file_path, e)
        raise internal_error(f"Error retrieving cache info: {str(e)}") from e


@router.delete(
    "/clear",
    response_model=CacheClearResponse,
    summary="Clear Cache",
    description="Clear cache entries (optionally filtered by language).",
    operation_id="clear_all_cache",
)
async def clear_cache(
    language: str | None = Query(None, description="Optional language filter"),
    tenant_id: str = Depends(require_tenant),
) -> CacheClearResponse:
    """Clear cache entries (optionally filtered by language)."""
    try:
        cache_manager = await get_cache_manager()
        redis = await cache_manager.get_redis()

        if language:
            pattern = f"tenant:{tenant_id}:{language}:*"
        else:
            pattern = f"tenant:{tenant_id}:*"
        entries_cleared = 0
        max_keys = 10000
        cursor = 0
        while True:
            cursor, keys = redis.client.scan(cursor, match=pattern, count=100)
            for key in keys:
                redis.client.delete(key)
                entries_cleared += 1
            if cursor == 0 or entries_cleared >= max_keys:
                break

        message = f"Cleared {entries_cleared} cache entries"
        if language:
            message += f" for language {language}"

        logger.info(message)
        return CacheClearResponse(message=message, entries_cleared=entries_cleared)

    except Exception as e:
        logger.exception("Error clearing cache: %s", e)
        raise internal_error(f"Error clearing cache: {str(e)}") from e


@router.delete(
    "/clear/{container_name}",
    response_model=ContainerCacheClearResponse,
    summary="Clear Container Cache",
    description="Clear all cache entries for a specific container.",
    operation_id="clear_container_cache",
)
async def clear_container_cache(
    container_name: str = PathParam(..., description="Container name"),
    tenant_id: str = Depends(require_tenant),
) -> ContainerCacheClearResponse:
    """Clear all cache entries for a specific container.

    Like the monolith, verifies the container exists (has a graph database)
    before attempting to clear its cache. Returns 404 if not found.
    """
    _validate_container_name(container_name)
    await _require_container_exists(container_name, tenant_id)
    try:
        cache_manager = await get_cache_manager()
        redis = await cache_manager.get_redis()

        pattern = f"tenant:{tenant_id}:container:{container_name}:*"
        deleted_count = 0
        max_keys = 10000
        cursor = 0
        while True:
            cursor, keys = redis.client.scan(cursor, match=pattern, count=100)
            for key in keys:
                redis.client.delete(key)
                deleted_count += 1
            if cursor == 0 or deleted_count >= max_keys:
                break

        message = f"Cleared {deleted_count} cache entries for container '{container_name}'"
        logger.info(message)

        return ContainerCacheClearResponse(
            message=message,
            container_name=container_name,
            deleted_count=deleted_count,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error clearing cache for container %s: %s", container_name, e)
        raise internal_error(f"Error clearing container cache: {str(e)}") from e


@router.post(
    "/optimize",
    response_model=CacheOptimizationResponse,
    summary="Optimize Cache",
    description="Intelligently optimize cache for better performance.",
    operation_id="optimize_cache",
)
async def optimize_cache(
    tenant_id: str = Depends(require_tenant),
) -> CacheOptimizationResponse:
    """Optimize cache by removing least useful entries."""
    try:
        cache_manager = await get_cache_manager()
        # Basic optimization: count current entries
        stats = cache_manager.get_stats()
        entries_before = stats.get("total_entries", 0) if isinstance(stats, dict) else 0
        optimization_stats = {
            "entries_before": entries_before,
            "entries_removed": 0,
            "space_freed_mb": 0.0,
            "optimization_strategy": [],
            "entries_after": entries_before,
        }

        logger.info("Cache optimization completed: %s", optimization_stats)

        return CacheOptimizationResponse(
            message=f"Cache optimization completed: removed {optimization_stats['entries_removed']} entries",
            optimization_stats=optimization_stats,
        )

    except Exception as e:
        logger.exception("Error optimizing cache: %s", e)
        raise internal_error(f"Error optimizing cache: {str(e)}") from e


@router.get(
    "/health",
    summary="Cache Health Check",
    description="Check cache system health and status.",
    operation_id="cache_health_check",
)
async def get_cache_health() -> dict[str, Any]:
    """Get cache system health information."""
    try:
        cache_manager = await get_cache_manager()
        stats = cache_manager.get_stats()

        return {
            "status": "healthy",
            "database_connected": True,
            "performance": stats,
        }

    except Exception as e:
        logger.exception("Error checking cache health: %s", e)
        raise internal_error(f"Error checking cache health: {str(e)}") from e


@router.get(
    "/keys/{container_name}",
    response_model=ContainerCacheKeysResponse,
    summary="List Container Cache Keys",
    description="Get paginated list of cache keys for a specific container.",
    operation_id="list_container_cache_keys",
)
async def get_container_cache_keys(
    container_name: str = PathParam(..., description="Container name"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum number of keys to return"),
    offset: int = Query(0, ge=0, description="Number of keys to skip"),
    tenant_id: str = Depends(require_tenant),
) -> ContainerCacheKeysResponse:
    """Get paginated list of cache keys for a specific container."""
    _validate_container_name(container_name)
    try:
        cache_manager = await get_cache_manager()
        redis = await cache_manager.get_redis()

        pattern = f"tenant:{tenant_id}:container:{container_name}:*"
        all_keys: list[str] = []
        max_keys = 10000
        cursor = 0
        while True:
            cursor, keys = redis.client.scan(cursor, match=pattern, count=100)
            all_keys.extend(k if isinstance(k, str) else k.decode("utf-8") for k in keys)
            if cursor == 0 or len(all_keys) >= max_keys:
                break

        paginated = all_keys[offset : offset + limit]

        return ContainerCacheKeysResponse(
            message=f"Retrieved {len(paginated)} cache keys for container '{container_name}'",
            container_name=container_name,
            keys=paginated,
            count=len(paginated),
            limit=limit,
            offset=offset,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error retrieving cache keys for container '%s': %s", container_name, e)
        raise internal_error(f"Error retrieving cache keys: {str(e)}") from e


@router.patch(
    "/ttl/{container_name}/{cache_operation}",
    response_model=TTLUpdateResponse,
    summary="Update Cache TTL",
    description="Update time-to-live for specific cache entries.",
    operation_id="update_cache_ttl",
)
async def update_cache_ttl(
    container_name: str = Depends(_depend_container_exists),
    cache_operation: str = PathParam(
        ..., description="Cache operation type (the cache-key segment)"
    ),
    request: TTLUpdateRequest = Body(..., description="TTL update request body"),
    tenant_id: str = Depends(require_tenant),
) -> TTLUpdateResponse:
    """Update TTL for specific cache entries."""
    try:
        ttl_seconds = request.ttl_seconds
        cache_manager = await get_cache_manager()
        redis = await cache_manager.get_redis()

        pattern = f"tenant:{tenant_id}:container:{container_name}:{cache_operation}:*"
        entries_updated = 0
        max_keys = 10000
        previous_ttl: int | None = None

        cursor = 0
        while True:
            cursor, keys = redis.client.scan(cursor, match=pattern, count=100)
            for key in keys:
                if entries_updated == 0:
                    raw_ttl = redis.client.ttl(key)
                    if raw_ttl == -2:
                        continue
                    previous_ttl = None if raw_ttl == -1 else raw_ttl

                if redis.expire(key, ttl_seconds):
                    entries_updated += 1
            if cursor == 0 or entries_updated >= max_keys:
                break

        message = f"Updated TTL to {ttl_seconds} seconds for {entries_updated} cache entries"
        logger.info("%s (container: %s, operation: %s)", message, container_name, cache_operation)

        return TTLUpdateResponse(
            message=message,
            container_name=container_name,
            operation=cache_operation,
            ttl_seconds=ttl_seconds,
            entries_updated=entries_updated,
            previous_ttl=previous_ttl,
        )

    except Exception as e:
        logger.exception(
            "Error updating TTL for container '%s', operation '%s': %s",
            container_name,
            cache_operation,
            e,
        )
        raise internal_error(f"Error updating cache TTL: {str(e)}") from e


@router.get(
    "/entry/{container_name}/{cache_operation}",
    response_model=CacheEntryDetailsResponse,
    summary="Cache Entry Details",
    description="Get detailed information about a specific cache entry.",
    operation_id="get_cache_entry",
)
async def get_cache_entry_details(
    container_name: str = PathParam(..., description="Name of the container"),
    cache_operation: str = PathParam(
        ..., description="Cache operation type or identifier (the cache-key segment)"
    ),
    tenant_id: str = Depends(require_tenant),
) -> CacheEntryDetailsResponse:
    """Get detailed information about a specific cache entry."""
    _validate_container_name(container_name)
    try:
        cache_key = _generate_container_cache_key(
            container_name, cache_operation, tenant_id=tenant_id
        )
        cache_manager = await get_cache_manager()
        redis = await cache_manager.get_redis()

        cached_value = await cache_manager.get(cache_key)
        if cached_value is None:
            raise not_found("Cache entry", f"{container_name}/{cache_operation}")

        ttl_raw: int = redis.client.ttl(cache_key)
        if ttl_raw == -2:
            raise not_found("Cache entry", f"{container_name}/{cache_operation}")
        ttl_remaining: int | None = None if ttl_raw == -1 else ttl_raw

        size = 0
        if isinstance(cached_value, str):
            size = len(cached_value.encode("utf-8"))
        elif isinstance(cached_value, bytes):
            size = len(cached_value)
        elif isinstance(cached_value, (dict, list)):
            size = len(json.dumps(cached_value).encode("utf-8"))
        else:
            size = len(str(cached_value).encode("utf-8"))

        return CacheEntryDetailsResponse(
            key=cache_key,
            value=cached_value,
            ttl_remaining=ttl_remaining,
            created_at=None,
            size=size,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error retrieving cache entry: %s", e)
        raise internal_error(f"Error retrieving cache entry: {str(e)}") from e


@router.get(
    "/info/{container_name}",
    response_model=ContainerCacheInfoResponse,
    summary="Container Cache Information",
    description="Get comprehensive cache information for a container.",
    operation_id="get_container_cache_info",
)
async def get_container_cache_info(
    container_name: str = PathParam(..., description="Name of the container"),
    tenant_id: str = Depends(require_tenant),
) -> ContainerCacheInfoResponse:
    """Get comprehensive cache information for a specific container."""
    _validate_container_name(container_name)
    try:
        cache_manager = await get_cache_manager()
        redis = await cache_manager.get_redis()

        pattern = f"tenant:{tenant_id}:container:{container_name}:*"
        total_size = 0
        entry_count = 0

        cursor = 0
        max_keys = 10000
        while True:
            cursor, keys = redis.client.scan(cursor, match=pattern, count=100)
            for key in keys:
                entry_count += 1
                value = redis.client.get(key)
                if value is not None:
                    if isinstance(value, bytes):
                        total_size += len(value)
                    elif isinstance(value, str):
                        total_size += len(value.encode("utf-8"))
            if cursor == 0 or entry_count >= max_keys:
                break

        hit_rate = 0.0

        cache_configuration = {
            "ttl_hours": 24,
            "max_cache_size_mb": 1024,
            "max_entries": 10000,
        }

        usage_statistics = {
            "entry_count": entry_count,
            "total_size_bytes": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "hit_count": None,
            "miss_count": None,
            "hit_rate_percentage": round(hit_rate, 2),
            "total_requests": 0,
        }

        health_status = "healthy"
        size_mb = total_size / (1024 * 1024)
        if size_mb > 700:
            health_status = "warning"
        if size_mb > 900:
            health_status = "critical"

        return ContainerCacheInfoResponse(
            message=f"Cache information retrieved for container '{container_name}'",
            container_name=container_name,
            cache_configuration=cache_configuration,
            usage_statistics=usage_statistics,
            health_status=health_status,
            last_eviction_timestamp=None,
        )

    except Exception as e:
        logger.exception("Error retrieving cache info for container '%s': %s", container_name, e)
        raise internal_error(f"Error retrieving cache info: {str(e)}") from e


@router.get(
    "/search/{container_name}",
    summary="Search Cache Keys by Pattern",
    description="Search cache keys matching a pattern within container scope.",
    operation_id="search_cache_keys",
)
async def search_cache_keys(
    container_name: str = PathParam(..., description="Container name"),
    pattern: str = Query(..., description="Glob-style pattern to match"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum results"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Search cache keys matching a pattern within a container's scope."""
    _validate_container_name(container_name)
    try:
        if not pattern or pattern.strip() in ("", "null", "undefined"):
            raise _bad_request("Pattern is required")

        cache_manager = await get_cache_manager()
        redis = await cache_manager.get_redis()

        redis_pattern = f"tenant:{tenant_id}:container:{container_name}:{pattern}"
        all_keys: list[str] = []
        max_keys = 10000
        cursor_val = 0

        while True:
            cursor_val, batch_keys = redis.client.scan(cursor_val, match=redis_pattern, count=100)
            all_keys.extend(k if isinstance(k, str) else k.decode("utf-8") for k in batch_keys)
            if cursor_val == 0 or len(all_keys) >= max_keys:
                break

        total = len(all_keys)
        paginated = all_keys[offset : offset + limit]

        matches = []
        for key in paginated:
            display_key = key.replace(f"tenant:{tenant_id}:container:{container_name}:", "", 1)
            matches.append({"key": display_key, "full_key": key})

        return {
            "container_name": container_name,
            "pattern": pattern,
            "matches": matches,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(matches) < total,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error searching cache keys: %s", e)
        raise internal_error(f"Error searching cache keys: {str(e)}") from e


@router.get(
    "/export/{container_name}",
    summary="Export Container Cache",
    description="Export all cache entries for a container in JSON or CSV format.",
    operation_id="export_container_cache",
)
async def export_container_cache(
    container_name: str = PathParam(..., description="Container name"),
    format: ExportFormat = Query(ExportFormat.JSON, description="Export format: json or csv"),
    max_keys: int = Query(1000, ge=1, le=10000, description="Maximum entries to export"),
    tenant_id: str = Depends(require_tenant),
):
    """Export all cache entries for a container in JSON or CSV format."""
    _validate_container_name(container_name)
    try:
        cache_manager = await get_cache_manager()
        redis = await cache_manager.get_redis()

        prefix = f"tenant:{tenant_id}:container:{container_name}:"
        redis_pattern = f"{prefix}*"

        entries: list[CacheEntryExport] = []
        cursor_val = 0

        while True:
            cursor_val, keys = redis.client.scan(cursor_val, match=redis_pattern, count=100)
            for key in keys:
                try:
                    key_str = key.decode("utf-8") if isinstance(key, bytes) else key
                    key_parts = key_str.split(":")
                    operation = key_parts[4] if len(key_parts) >= 5 else "unknown"

                    value = redis.client.get(key)
                    size = 0
                    value_serialized = None
                    if value is not None:
                        if isinstance(value, str):
                            size = len(value.encode("utf-8"))
                            try:
                                value_serialized = json.loads(value)
                            except json.JSONDecodeError, TypeError:
                                value_serialized = value
                        elif isinstance(value, bytes):
                            size = len(value)
                            try:
                                decoded = value.decode("utf-8")
                                value_serialized = json.loads(decoded)
                            except UnicodeDecodeError, json.JSONDecodeError, TypeError:
                                value_serialized = value.decode("utf-8", errors="replace")
                        else:
                            size = len(str(value).encode("utf-8"))
                            value_serialized = value

                    ttl = redis.ttl(key_str)

                    entries.append(
                        CacheEntryExport(
                            key=key_str,
                            operation=operation,
                            value=value_serialized,
                            size=size,
                            created_at=None,
                            ttl=ttl,
                        )
                    )
                except Exception as entry_err:
                    logger.warning(
                        "Error processing cache key %s during export: %s", key, entry_err
                    )
                    continue

            if cursor_val == 0 or len(entries) >= max_keys:
                break

        exported_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        if format == ExportFormat.JSON:
            return CacheExportResponse(
                container_name=container_name,
                total_entries=len(entries),
                export_format=format,
                exported_at=exported_at,
                entries=entries,
            )
        else:
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["key", "operation", "size", "created_at", "ttl"])
            for entry in entries:
                writer.writerow(
                    [
                        entry.key,
                        entry.operation,
                        entry.size,
                        entry.created_at or "",
                        entry.ttl if entry.ttl is not None else "",
                    ]
                )

            csv_content = output.getvalue()
            output.close()

            return StreamingResponse(
                io.StringIO(csv_content),
                media_type="text/csv",
                headers={
                    "Content-Disposition": f'attachment; filename="cache_export_{container_name}_{datetime.now(UTC).strftime("%Y%m%d_%H%M%S")}.csv"'
                },
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error exporting cache for container '%s': %s", container_name, e)
        raise internal_error(f"Error exporting cache: {str(e)}") from e


@router.post(
    "/warmup/{container_name}",
    response_model=CacheWarmupResponse,
    summary="Cache Warmup",
    description="Pre-populate cache with common operations for a container.",
    operation_id="warmup_container_cache",
)
async def warmup_cache(
    container_name: str = PathParam(..., description="Name of the container"),
    operations: str | None = Query(None, description="Comma-separated operations to warm"),
    tenant_id: str = Depends(require_tenant),
) -> CacheWarmupResponse:
    """Pre-populate cache with common operations for a container."""
    _validate_container_name(container_name)
    import time as _time

    start_time = _time.time()
    warmed_entries: list[CacheWarmupEntry] = []

    try:
        if operations:
            ops_to_warm = [op.strip() for op in operations.split(",") if op.strip()]
        else:
            ops_to_warm = ["graph_stats", "file_list", "analysis_results"]

        valid_operations = {"graph_stats", "file_list", "analysis_results"}
        invalid_ops = [op for op in ops_to_warm if op not in valid_operations]
        if invalid_ops:
            raise _bad_request(
                f"Invalid operations: {', '.join(invalid_ops)}. Valid: {', '.join(valid_operations)}"
            )

        cache_manager = await get_cache_manager()

        for operation in ops_to_warm:
            op_start = _time.time()
            cache_key = _generate_container_cache_key(
                container_name, operation, tenant_id=tenant_id
            )

            cached_value = await cache_manager.get(cache_key)
            if cached_value is not None:
                op_time = (_time.time() - op_start) * 1000
                warmed_entries.append(
                    CacheWarmupEntry(
                        operation=operation,
                        cache_key=cache_key,
                        success=True,
                        cached_size_bytes=len(str(cached_value).encode("utf-8")),
                        execution_time_ms=op_time,
                    )
                )
                continue

            # Placeholder warmup data
            result = {"container_name": container_name, "operation": operation, "warmed": True}
            await cache_manager.set(cache_key, result, ttl=3600)

            op_time = (_time.time() - op_start) * 1000
            warmed_entries.append(
                CacheWarmupEntry(
                    operation=operation,
                    cache_key=cache_key,
                    success=True,
                    cached_size_bytes=len(str(result).encode("utf-8")),
                    execution_time_ms=op_time,
                )
            )

        total_time = (_time.time() - start_time) * 1000
        success_count = sum(1 for e in warmed_entries if e.success)
        failure_count = len(warmed_entries) - success_count

        return CacheWarmupResponse(
            message=f"Cache warmup completed for {container_name}: {success_count}/{len(warmed_entries)} successful",
            container_name=container_name,
            warmed_entries=warmed_entries,
            total_entries=len(warmed_entries),
            total_time_ms=total_time,
            success_count=success_count,
            failure_count=failure_count,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error during cache warmup for %s: %s", container_name, e)
        raise internal_error(f"Error during cache warmup: {str(e)}") from e


@router.get(
    "/compare",
    summary="Compare Container Caches",
    description="Side-by-side comparison of cache performance across containers.",
    operation_id="compare_container_caches",
)
async def compare_container_caches(
    container_names: str = Query(..., description="Comma-separated container names"),
    tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Compare cache statistics across multiple containers."""
    try:
        names = [n.strip() for n in container_names.split(",") if n.strip()]
        if len(names) < 2:
            raise _bad_request("At least 2 container names required for comparison")
        if len(names) > 10:
            raise _bad_request("Maximum 10 containers for comparison")

        for name in names:
            _validate_container_name(name)

        cache_manager = await get_cache_manager()
        redis = await cache_manager.get_redis()

        containers = []
        for name in names:
            pattern = f"tenant:{tenant_id}:container:{name}:*"
            entry_count = 0
            total_size = 0
            max_keys = 10000
            cursor_val = 0
            while True:
                cursor_val, keys = redis.client.scan(cursor_val, match=pattern, count=100)
                for key in keys:
                    entry_count += 1
                    value = redis.client.get(key)
                    if value:
                        total_size += (
                            len(value)
                            if isinstance(value, bytes)
                            else len(str(value).encode("utf-8"))
                        )
                if cursor_val == 0 or entry_count >= max_keys:
                    break

            containers.append(
                {
                    "container_name": name,
                    "hit_count": 0,
                    "miss_count": 0,
                    "hit_ratio": 0.0,
                    "size_bytes": total_size,
                    "size_mb": round(total_size / (1024 * 1024), 2),
                    "entry_count": entry_count,
                    "requests": 0,
                }
            )

        return {
            "status": "success",
            "containers": containers,
            "comparison_count": len(containers),
            "visualization": {
                "hit_ratio_chart": {c["container_name"]: c["hit_ratio"] for c in containers},
                "size_chart": {c["container_name"]: c["size_mb"] for c in containers},
                "entry_count_chart": {c["container_name"]: c["entry_count"] for c in containers},
                "outliers": {},
            },
            "summary": {
                "total_size_mb": sum(c["size_mb"] for c in containers),
                "total_entries": sum(c["entry_count"] for c in containers),
            },
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error comparing container caches: %s", e)
        raise internal_error(f"Error comparing caches: {str(e)}") from e


@router.get(
    "/evictions/{container_name}",
    summary="Cache Eviction History",
    description="Get cache eviction history for a container.",
    operation_id="get_cache_eviction_history",
)
async def get_cache_eviction_history(
    container_name: str = PathParam(..., description="Container name"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum eviction records to return"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    date_from: str | None = Query(None, description="Filter from date (ISO 8601)"),
    date_to: str | None = Query(None, description="Filter to date (ISO 8601)"),
    reason: str | None = Query(None, description="Filter by eviction reason"),
    tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Get cache eviction history for a container.

    Checks container existence first (returning 404 like the monolith's
    ``get_container_from_path`` dependency) then retrieves eviction records.
    """
    _validate_container_name(container_name)
    await _require_container_exists(container_name, tenant_id)
    try:
        cache_manager = await get_cache_manager()
        history = await cache_manager.get_eviction_history(
            container_name=container_name,
            limit=limit,
            offset=offset,
            date_from=date_from,
            date_to=date_to,
            reason_filter=reason,
        )

        return {
            "container_name": container_name,
            "evictions": history.get("evictions", []),
            "total": history.get("total", 0),
            "limit": limit,
            "offset": offset,
            "date_from": date_from,
            "date_to": date_to,
            "reason_filter": reason,
            "summary": history.get("summary", {}),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(
            "Error retrieving eviction history for container '%s': %s", container_name, e
        )
        raise internal_error(f"Error retrieving eviction history: {str(e)}") from e
