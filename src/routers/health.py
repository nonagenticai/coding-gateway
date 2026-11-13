"""
V2 System Health Endpoint

Clean, modern health monitoring API with comprehensive system diagnostics.
Consolidates all health checking functionality into a unified interface.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from auth.dependencies import require_tenant
from clients.outbound_identity import identity_headers
from config.settings import get_settings

# Optional import for psutil
try:
    import psutil

    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(
    responses={
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)


# ---------------------------------------------------------------------------
# Pydantic response models (self-contained for the gateway)
# ---------------------------------------------------------------------------


class SystemHealth(BaseModel):
    """Overall system health status (matches monolith SystemHealth)."""

    status: str = Field(..., description="Overall system health")
    uptime: float = Field(..., description="System uptime in seconds")
    version: str = Field(default="1.0.0", description="Application version")
    environment: str = Field(default="production", description="Deployment environment")
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    total_containers: int = Field(default=0, description="Total containers")
    healthy_containers_count: int = Field(default=0, description="Healthy containers")
    unhealthy_containers_count: int = Field(default=0, description="Unhealthy containers")
    container_health_percentage: float = Field(default=0.0, description="Container health %")


class HealthCheckResponse(BaseModel):
    """Health check response (matches monolith BaseResponse + health field)."""

    status: str = Field(default="success", description="Response status")
    message: str = Field(default="", description="Human-readable message")
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    health: SystemHealth = Field(..., description="System health status")


class MetricsResponse(BaseModel):
    """System metrics response (matches monolith MetricsResponse)."""

    status: str = Field(default="success", description="Response status")
    message: str = Field(default="", description="Human-readable message")
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    metrics: dict[str, Any] = Field(default_factory=dict, description="System metrics")
    collection_time: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class SystemMetricsResponse(BaseModel):
    """System metrics response for /system-metrics endpoint."""

    status: str = Field(default="success", description="Response status")
    message: str = Field(default="", description="Human-readable message")
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    collection_time: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    cpu_percent: float | None = None
    memory_percent: float | None = None
    memory_used_mb: float | None = None
    memory_total_mb: float | None = None
    disk_percent: float | None = None
    open_file_descriptors: int | None = None


# ---------------------------------------------------------------------------
# Shared httpx client for health checks
# ---------------------------------------------------------------------------
_health_http_client = httpx.AsyncClient(timeout=3.0)

# ---------------------------------------------------------------------------
# Startup time tracker
# ---------------------------------------------------------------------------
_start_time = time.time()


# ---------------------------------------------------------------------------
# Health endpoints
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=HealthCheckResponse,
    summary="Health Check",
    description="Comprehensive health check for the gateway service.",
    operation_id="health_api_check",
)
async def health_check(
    tenant_id: str = Depends(require_tenant),
) -> HealthCheckResponse:
    """Return overall health status of the gateway."""
    uptime = time.time() - _start_time

    # Check backend services reachability
    backend_services = {
        "coding-analysis": settings.analysis_url,
        "coding-testing": settings.testing_url,
        "coding-ai": settings.ai_url,
        "coding-containers": settings.containers_url,
        "coding-github": settings.github_url,
        "coding-security": settings.security_url,
        "coding-realtime": settings.realtime_url,
    }

    overall_healthy = True
    for name, base_url in backend_services.items():
        try:
            resp = await _health_http_client.get(f"{base_url}/health")
            if resp.status_code >= 400:
                overall_healthy = False
        except Exception:
            logger.debug("Health check failed for backend service %s", name, exc_info=True)
            overall_healthy = False

    health_status = "healthy" if overall_healthy else "degraded"

    system_health = SystemHealth(
        status=health_status,
        uptime=round(uptime, 2),
        version=os.getenv("VERSION", "1.0.0"),
        environment=os.getenv("ENVIRONMENT", "production"),
    )

    return HealthCheckResponse(
        message="System health check completed",
        health=system_health,
    )


@router.get(
    "/ready",
    response_model=HealthCheckResponse,
    summary="Readiness Check",
    description="Kubernetes readiness probe.",
    operation_id="health_ready",
)
async def service_readiness_check() -> HealthCheckResponse:
    """Readiness probe -- returns 200 when the gateway can serve traffic."""
    uptime = time.time() - _start_time
    system_health = SystemHealth(
        status="healthy",
        uptime=round(uptime, 2),
        version=os.getenv("VERSION", "1.0.0"),
        environment=os.getenv("ENVIRONMENT", "production"),
    )
    return HealthCheckResponse(
        message="Service is ready",
        health=system_health,
    )


@router.get(
    "/live",
    response_model=HealthCheckResponse,
    summary="Liveness Check",
    description="Kubernetes liveness probe.",
    operation_id="health_live",
)
async def service_liveness_check() -> HealthCheckResponse:
    """Liveness probe -- returns 200 if the process is alive."""
    uptime = time.time() - _start_time
    system_health = SystemHealth(
        status="healthy",
        uptime=round(uptime, 2),
        version=os.getenv("VERSION", "1.0.0"),
        environment=os.getenv("ENVIRONMENT", "production"),
    )
    return HealthCheckResponse(
        message="Service is alive",
        health=system_health,
    )


@router.get(
    "/health/ready",
    response_model=HealthCheckResponse,
    summary="Readiness Check (K8s alias)",
    description="Alias for /ready to match monolith path.",
    operation_id="health_ready_alias",
)
async def health_ready_alias() -> HealthCheckResponse:
    """Alias: /health/ready → /ready.

    Monolith calls health_check() internally and returns 503 if unhealthy.
    Gateway always returns ready since we don't have the same container checks.
    """
    return await service_readiness_check()


@router.get(
    "/health/live",
    response_model=HealthCheckResponse,
    summary="Liveness Check (K8s alias)",
    description="Alias for /live to match monolith path.",
    operation_id="health_live_alias",
)
async def health_live_alias() -> HealthCheckResponse:
    """Alias: /health/live → /live."""
    return await service_liveness_check()


@router.get(
    "/detailed",
    summary="Detailed Health Check",
    description="Detailed health information including system resources.",
    operation_id="health_detailed",
)
async def detailed_health_check(
    tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Return detailed health information including system metrics.

    Matches monolith DetailedHealthResponse structure:
    status, message, timestamp, request_id, system, resources, containers, database, components
    """
    uptime = time.time() - _start_time

    # Check backend services reachability
    backend_services = {
        "coding-analysis": settings.analysis_url,
        "coding-testing": settings.testing_url,
        "coding-ai": settings.ai_url,
        "coding-containers": settings.containers_url,
        "coding-github": settings.github_url,
        "coding-security": settings.security_url,
        "coding-realtime": settings.realtime_url,
    }

    unreachable: list[str] = []
    for name, base_url in backend_services.items():
        try:
            resp = await _health_http_client.get(f"{base_url}/health")
            if resp.status_code >= 400:
                unreachable.append(name)
        except Exception:
            logger.debug("Detailed health check failed for backend service %s", name, exc_info=True)
            unreachable.append(name)

    health_status = "healthy" if not unreachable else "degraded"

    # Build system health (matches monolith SystemHealth)
    system_health = {
        "status": health_status,
        "uptime": round(uptime, 2),
        "version": os.getenv("VERSION", "1.0.0"),
        "environment": os.getenv("ENVIRONMENT", "production"),
        "timestamp": datetime.now(UTC).isoformat(),
        "total_containers": 0,
        "healthy_containers_count": 0,
        "unhealthy_containers_count": 0,
        "container_health_percentage": 0.0,
    }

    # Build resources (matches monolith ResourceMetrics)
    resources: dict[str, Any] = {
        "cpu_usage": 0.0,
        "memory_usage": 0.0,
        "disk_usage": 0.0,
        "network_connections": 0,
    }
    if HAS_PSUTIL:
        try:
            net_connections = len(psutil.net_connections(kind="inet"))
        except psutil.AccessDenied, OSError:
            net_connections = 0
        resources = {
            "cpu_usage": psutil.cpu_percent(interval=0.1),
            "memory_usage": psutil.virtual_memory().percent,
            "disk_usage": psutil.disk_usage("/").percent,
            "network_connections": net_connections,
        }

    # Build database health (matches monolith DatabaseHealth)
    database: dict[str, Any] = {
        "status": "healthy",
        "connection_pool": {"active": 0, "idle": 0, "max": 0},
        "query_performance": {},
        "storage_usage": None,
    }

    # Components (matches monolith list[ComponentHealth])
    components: list[dict[str, Any]] = [
        {
            "name": "API Server",
            "status": "healthy",
            "message": "API server is running normally",
            "metrics": {"uptime": round(uptime, 2)},
        },
    ]

    # Container health (matches monolith ContainerHealth)
    containers: dict[str, Any] = {
        "total_containers": 0,
        "running_containers": 0,
        "failed_containers": 0,
        "resource_usage": resources,
        "containers": [],
        "overall_status": "healthy",
    }

    return {
        "status": "success",
        "message": "Detailed system health check completed",
        "timestamp": datetime.now(UTC).isoformat(),
        "request_id": str(uuid.uuid4()),
        "system": system_health,
        "resources": resources,
        "containers": containers,
        "database": database,
        "components": components,
    }


@router.get(
    "/system-metrics",
    response_model=SystemMetricsResponse,
    summary="System Metrics",
    description="System resource metrics (CPU, memory, disk).",
    operation_id="get_system_metrics",
)
async def get_system_metrics(
    tenant_id: str = Depends(require_tenant),
) -> SystemMetricsResponse:
    """Return system resource metrics."""
    if not HAS_PSUTIL:
        return SystemMetricsResponse(message="System metrics (psutil not available)")

    vm = psutil.virtual_memory()
    return SystemMetricsResponse(
        message="System metrics retrieved",
        cpu_percent=psutil.cpu_percent(interval=0.1),
        memory_percent=vm.percent,
        memory_used_mb=round(vm.used / (1024 * 1024), 2),
        memory_total_mb=round(vm.total / (1024 * 1024), 2),
        disk_percent=psutil.disk_usage("/").percent,
        open_file_descriptors=len(psutil.Process().open_files())
        if hasattr(psutil.Process(), "open_files")
        else None,
    )


# ---------------------------------------------------------------------------
# System metrics on health router (matches monolith's /api/health/metrics)
# ---------------------------------------------------------------------------


@router.get(
    "/metrics",
    response_model=MetricsResponse,
    summary="System Metrics (health router)",
    description="System metrics endpoint on the health router, matching monolith.",
    operation_id="get_health_metrics",
)
async def health_router_metrics(
    tenant_id: str = Depends(require_tenant),
) -> MetricsResponse:
    """Return psutil system metrics from the health router (monolith parity).

    Matches monolith MetricsResponse: status, message, timestamp, request_id, metrics, collection_time.
    """
    import platform as _platform
    import sys as _sys

    metrics: dict[str, Any] = {}
    if HAS_PSUTIL:
        try:
            vm = psutil.virtual_memory()
            swap = psutil.swap_memory()
            disk = psutil.disk_usage("/")
            system_metrics = {
                "cpu_count": psutil.cpu_count(),
                "cpu_percent": psutil.cpu_percent(interval=0.1),
                "memory": dict(vm._asdict()),
                "swap": dict(swap._asdict()),
                "disk": dict(disk._asdict()),
                "boot_time": psutil.boot_time(),
                "platform": _platform.platform(),
                "python_version": _sys.version,
            }
        except Exception:
            logger.debug("Failed to collect system metrics via psutil", exc_info=True)
            system_metrics = {
                "cpu_count": os.cpu_count() or 1,
                "cpu_percent": 0.0,
                "memory": {},
                "swap": {},
                "disk": {},
                "boot_time": 0.0,
                "platform": _platform.platform(),
                "python_version": _sys.version,
            }

        try:
            net = psutil.net_io_counters()
            network_metrics = dict(net._asdict()) if net else {}
        except Exception:
            logger.debug("Failed to collect network metrics via psutil", exc_info=True)
            network_metrics = {}

        try:
            proc = psutil.Process()
            process_metrics = {
                "cpu_percent": proc.cpu_percent(),
                "memory_info": dict(proc.memory_info()._asdict()),
                "num_threads": proc.num_threads(),
                "create_time": proc.create_time(),
            }
        except Exception:
            logger.debug("Failed to collect process metrics via psutil", exc_info=True)
            process_metrics = {
                "cpu_percent": 0.0,
                "memory_info": {},
                "num_threads": 1,
                "create_time": _start_time,
            }

        metrics = {
            "system": system_metrics,
            "network": network_metrics,
            "process": process_metrics,
        }
    else:
        metrics = {
            "system": {
                "cpu_count": os.cpu_count() or 1,
                "cpu_percent": 0.0,
                "memory": {},
                "swap": {},
                "disk": {},
                "boot_time": 0.0,
                "platform": _platform.platform(),
                "python_version": _sys.version,
            },
            "network": {},
            "process": {
                "cpu_percent": 0.0,
                "memory_info": {},
                "num_threads": 1,
                "create_time": _start_time,
            },
        }
    return MetricsResponse(
        message="System metrics retrieved",
        metrics=metrics,
    )


# ---------------------------------------------------------------------------
# Container health proxy routes (forward to coding-containers)
#
# There is deliberately NO ``/container/{name}/history`` route: it proxied to a
# coding-containers path that does not exist, so it could never succeed and was
# removed from the published API (op index 347 -> 346).
# ---------------------------------------------------------------------------


async def _proxy_to_containers(
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    body: Any = None,
    *,
    tenant_id: str | None,
) -> Response:
    """Forward a request to the coding-containers service, preserving status.

    Sends ``identity_headers(tenant_id)`` -- the caller's tenant (as resolved by
    ``require_tenant`` for THIS request) in ``X-Tenant-Id`` plus the gateway's own
    licence in ``Authorization`` -- exactly like every other gateway-owned call to
    coding-containers (``services/container_workspace.py``). coding-containers
    honours the asserted tenant because ``svc-coding-gateway`` is on its
    on-behalf-of allowlist, and scopes the lookup to it, so another tenant's
    container reads as 404.

    It used to send NO identity at all: ``tenant_id`` was resolved by every route
    and then dropped, so coding-containers answered 401 to every valid licence
    (measured 2026-10-12 on ``/ready``, ``/live``, ``/statistics``).
    ``tenant_id`` is keyword-only and has no default so a new call site cannot
    forget it again.
    """
    url = f"{settings.containers_url}{path}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.request(
                method, url, params=params, json=body, headers=identity_headers(tenant_id)
            )
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type", "application/json"),
            )
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail="coding-containers service unreachable")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="coding-containers service timed out")


def _container_error(name: str, path: str, status_code: int) -> Response:
    """Monolith-compatible error envelope whose status and code tell the truth.

    Both container routes below used to call every non-200 "not found":
    ``get_container_health`` forced the status to 404 and ``get_container_liveness``
    kept the backend status but labelled it ``not_found`` -- so a 401 from
    coding-containers read as "your container does not exist". The envelope
    fields are unchanged; only ``status_code`` / ``error_code`` / ``message`` now
    follow the backend's actual answer:

        401 -> 401 unauthorized        403 -> 403 forbidden
        404 -> 404 not_found           503 -> 503 service_unavailable
        other 4xx -> same status, bad_request
        other 5xx -> 502 bad_gateway   unreachable/timeout -> 503 service_unavailable
    """
    from fastapi.responses import JSONResponse

    if status_code == 401:
        status, code, message = 401, "unauthorized", "coding-containers refused the credential"
    elif status_code == 403:
        status, code, message = 403, "forbidden", f"Access to container '{name}' is forbidden"
    elif status_code == 404:
        status, code, message = 404, "not_found", f"Container '{name}' not found"
    elif status_code == 503:
        status, code, message = 503, "service_unavailable", f"Container '{name}' is unavailable"
    elif 400 <= status_code < 500:
        status, code, message = status_code, "bad_request", f"Request for '{name}' was rejected"
    elif status_code == 0:
        status, code, message = 503, "service_unavailable", "coding-containers is unreachable"
    else:
        status, code, message = 502, "bad_gateway", f"coding-containers failed ({status_code})"
    return JSONResponse(
        status_code=status,
        content={
            "status": "error",
            "message": message,
            "error_code": code,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "details": message,
            "request_id": str(uuid.uuid4()),
            "path": path,
            "method": "GET",
        },
    )


@router.get(
    "/containers/unhealthy",
    summary="Unhealthy Containers",
    description="List unhealthy containers.",
    response_model=None,
    operation_id="list_unhealthy_containers",
)
async def get_unhealthy_containers(
    tenant_id: str = Depends(require_tenant),
) -> dict[str, Any] | Response:
    """Get list of unhealthy containers.

    Matches monolith response structure: count, total_containers,
    unhealthy_containers, timestamp.
    Falls back to empty result if coding-containers is unreachable.
    """
    try:
        resp = await _proxy_to_containers(
            "GET", "/api/health/containers/unhealthy", tenant_id=tenant_id
        )
        if resp.status_code == 200:
            return resp
    except HTTPException:
        logger.debug("coding-containers unreachable for unhealthy containers check", exc_info=True)

    # Fallback: return empty result (matches monolith with no containers)
    import time as _time

    return {
        "count": 0,
        "total_containers": 0,
        "unhealthy_containers": [],
        "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
    }


@router.get(
    "/container/{name}",
    summary="Container Health Check",
    description="Get comprehensive health for a specific container.",
    response_model=None,
    operation_id="get_container_health",
)
async def get_container_health(
    name: str,
    tenant_id: str = Depends(require_tenant),
) -> dict[str, Any] | Response:
    """Container-specific health check.

    Matches monolith ContainerHealthResponse structure: container_name, status,
    healthy, codebase_path, api_available, graph_initialized, last_activity, issues.
    Non-200 answers keep the monolith envelope with a faithful status
    (``_container_error``); unreachable is 503, not "not found".
    """
    path = f"/api/health/container/{name}"
    try:
        resp = await _proxy_to_containers("GET", path, tenant_id=tenant_id)
    except HTTPException:
        logger.debug(
            "coding-containers unreachable for container %s health check", name, exc_info=True
        )
        return _container_error(name, path, 0)
    if resp.status_code == 200:
        return resp
    return _container_error(name, path, resp.status_code)


@router.get(
    "/container/{name}/ready",
    summary="Container Readiness",
    description="Check if a container is ready (proxied to coding-containers).",
    operation_id="get_container_readiness",
)
async def get_container_readiness(
    name: str,
    tenant_id: str = Depends(require_tenant),
) -> Response:
    """Container readiness probe.

    Matches monolith behaviour: returns 503 Service Unavailable when the
    container is unknown or not ready (monolith catches all exceptions and
    raises service_unavailable).
    """
    try:
        resp = await _proxy_to_containers(
            "GET", f"/api/health/container/{name}/ready", tenant_id=tenant_id
        )
    except HTTPException:
        # coding-containers unreachable → treat as not ready
        raise HTTPException(
            status_code=503,
            detail={
                "status": "error",
                "message": f"Container '{name}' is not ready",
                "error_code": "SERVICE_UNAVAILABLE",
                "details": {},
            },
        )
    # Monolith returns 503 for unknown/unready containers (not 404).
    if resp.status_code in (404, 502, 504):
        raise HTTPException(
            status_code=503,
            detail={
                "status": "error",
                "message": f"Container '{name}' is not ready",
                "error_code": "SERVICE_UNAVAILABLE",
                "details": {},
            },
        )
    return resp


@router.get(
    "/container/{name}/live",
    summary="Container Liveness",
    description="Check if a container is alive (proxied to coding-containers).",
    operation_id="get_container_liveness",
)
async def get_container_liveness(
    name: str,
    tenant_id: str = Depends(require_tenant),
) -> Response:
    """Container liveness probe -- proxy through to coding-containers.

    Non-200 responses are wrapped in the monolith's standard error format with
    a faithful status/error_code (``_container_error``).
    """
    path = f"/api/health/container/{name}/live"
    try:
        resp = await _proxy_to_containers("GET", path, tenant_id=tenant_id)
    except HTTPException:
        return _container_error(name, path, 0)
    if resp.status_code == 200:
        return resp
    return _container_error(name, path, resp.status_code)


@router.get(
    "/container/{name}/statistics",
    summary="Container Statistics",
    description="Get container statistics (proxied to coding-containers).",
    operation_id="get_container_statistics",
)
async def get_container_statistics(
    name: str,
    tenant_id: str = Depends(require_tenant),
) -> Response:
    return await _proxy_to_containers(
        "GET", f"/api/health/container/{name}/statistics", tenant_id=tenant_id
    )


@router.post(
    "/containers/batch",
    summary="Batch Container Health",
    description="Batch health check for containers.",
    response_model=None,
    operation_id="batch_container_health",
)
async def batch_container_health(
    body: dict[str, Any] | None = None,
    tenant_id: str = Depends(require_tenant),
) -> dict[str, Any] | Response:
    """Batch container health check.

    Matches monolith BatchContainerHealthResponse structure: status, message,
    timestamp, request_id, containers, total_requested, total_found, total_not_found.
    Falls back to stub response if coding-containers is unreachable.
    """
    try:
        resp = await _proxy_to_containers(
            "POST", "/api/health/containers/batch", body=body, tenant_id=tenant_id
        )
        if resp.status_code == 200:
            return resp
    except HTTPException:
        logger.debug("coding-containers unreachable for batch health check", exc_info=True)

    # Fallback: return stub response matching monolith schema
    container_names = (body or {}).get("container_names", [])
    not_found_containers = [
        {
            "container_name": name,
            "status": "not_found",
            "healthy": False,
            "codebase_path": "",
            "api_available": False,
            "graph_initialized": False,
            "last_activity": None,
            "issues": ["Container does not exist"],
        }
        for name in container_names
    ]
    total = len(container_names)
    return {
        "status": "success",
        "message": f"Batch health check completed for {total} container(s): 0 found, {total} not found",
        "timestamp": datetime.now(UTC).isoformat(),
        "request_id": str(uuid.uuid4()),
        "containers": not_found_containers,
        "total_requested": total,
        "total_found": 0,
        "total_not_found": total,
    }
