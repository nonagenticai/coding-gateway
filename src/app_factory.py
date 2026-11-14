"""Application factory for creating and configuring the FastAPI gateway app.

This module provides the central application factory pattern for creating
and configuring the FastAPI application instance. It handles the complete
application lifecycle including startup, configuration, middleware setup,
route registration, and shutdown procedures.

Usage:
    app = create_app()
    # App is now ready to run with uvicorn or other ASGI server
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from coding_client.api.logging_setup import configure_logging
from coding_client.api.request_logging import HealthCheckFilter, RequestLoggingMiddleware
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Manage application lifespan with proper startup and shutdown procedures.

    Startup:
        - Initialize cache manager for application-wide caching
        - Set up health checker
        - Initialize background task tracking

    Shutdown:
        - Close cache connections
        - Close proxy httpx client
        - Cancel background tasks
    """
    startup_time = time.time()

    service_name = os.getenv("SERVICE_NAME", "Code Graph Helper API")
    version = os.getenv("VERSION", "1.0.0")
    environment = os.getenv("ENVIRONMENT", "production")

    logger.info("Starting %s v%s (%s)", service_name, version, environment)

    try:
        # NOTE: Database tables are created by Alembic migrations via K8s initContainer
        print("[startup] Database migrations handled by K8s initContainer", flush=True)

        # Initialize lightweight state
        app.state.background_tasks = set()

        # Initialize cache manager
        from services.cache_manager import get_cache_manager

        app.state.cache = await get_cache_manager()

        startup_duration = time.time() - startup_time
        logger.info("%s ready! Started in %.2fs", service_name, startup_duration)
        yield

    except Exception as e:
        logger.error("Error during %s startup: %s", service_name, e)
        raise
    finally:
        logger.info("Shutting down %s", service_name)

        # Flush + shut down OTel metrics (if enabled)
        from observability.telemetry import shutdown_metrics

        shutdown_metrics(getattr(app.state, "meter_provider", None))

        # Close proxy client
        from routers.proxy import close_proxy_client

        await close_proxy_client()

        # Close cache
        if hasattr(app.state, "cache") and app.state.cache is not None:
            try:
                await app.state.cache.close()
            except Exception:
                logger.warning("Failed to close cache during shutdown", exc_info=True)

        # Cancel background tasks
        if hasattr(app.state, "background_tasks"):
            for task in app.state.background_tasks:
                if not task.done():
                    task.cancel()
            if app.state.background_tasks:
                await asyncio.gather(*app.state.background_tasks, return_exceptions=True)


def _describe_exc(exc: BaseException) -> str:
    """Render an exception for an operator-facing honesty marker.

    ``str(exc)`` is empty for a bare ``SomeError()``, which would produce an
    ``"error": ""`` field that reads as "no error". Fall back to the class name.
    """
    return str(exc) or exc.__class__.__name__


# ---------------------------------------------------------------------------
# Unique operation ID generator
# ---------------------------------------------------------------------------


def generate_unique_id(route: APIRoute) -> str:
    """Generate unique operation IDs from route paths and HTTP methods.

    Converts route paths to clean, readable operation IDs without version prefixes
    or module path noise. Includes HTTP method to ensure uniqueness.
    """
    path = route.path.replace("/api/", "").strip("/")
    method = list(route.methods)[0].lower() if route.methods else "unknown"

    if not path:
        return f"root_{method}"

    path = path.replace("-", "_")
    parts = path.split("/")
    clean_parts = []
    for part in parts:
        clean_part = part.replace("{", "").replace("}", "")
        if clean_part:
            clean_parts.append(clean_part)

    base_id = "_".join(clean_parts) if clean_parts else "root"

    if base_id == "health":
        if route.path == "/health":
            return f"health_root_{method}"
        elif route.path.startswith("/api/health"):
            return f"health_api_{method}"

    return f"{base_id}_{method}"


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    """Create and configure a fully functional FastAPI gateway application.

    Returns:
        FastAPI: Fully configured application instance ready for deployment.
    """
    configure_logging()

    creation_start = time.time()
    logger.info("Creating FastAPI gateway application...")

    from config.settings import get_settings

    settings = get_settings()

    app = FastAPI(
        title=os.getenv("SERVICE_NAME", "Code Graph Helper API"),
        version=os.getenv("VERSION", "1.0.0"),
        description=(
            "API gateway for the Code Graph Helper platform.\n\n"
            "## Auth\n"
            "Istio-forwarded headers (`x-user-id`, `x-tenant-id`, `x-user-roles`), "
            "license token introspection (`Authorization: Bearer <token>`), "
            "or trusted-network bypass for RFC1918/loopback IPs. "
            "Auth provided by `coding-client`.\n\n"
            "## Gateway-owned endpoints\n"
            "- **Graphs** `/api/graphs` -- code graph CRUD, SQLite per-container databases\n"
            "- **Validation** `/api/validation` -- reference checks, consistency, architecture\n"
            "- **Cache** `/api/cache` -- Redis cache stats, clear, import/export\n"
            "- **Grammars** `/api/grammars` -- Tree-sitter grammar lifecycle\n"
            "- **Visualization** `/api/visualization` -- cross-language dependency graphs\n"
            "- **Metrics** `/api/health/metrics` -- graph analytics, centrality, per-container\n"
            "- **Jobs** `/api/jobs` -- Kubernetes pytest job orchestration\n"
            "- **Health** `/api/health` -- readiness, liveness, detailed, system metrics\n\n"
            "## Proxied backends\n"
            "coding-analysis (:8688), coding-testing (:8689), coding-ai (:8690), "
            "coding-containers (:8691), coding-github (:8692), coding-security (:8693), "
            "coding-realtime (:8694)"
        ),
        openapi_url="/openapi.json",
        lifespan=lifespan,
        docs_url="/docs",
        swagger_ui_parameters={"persistAuthorization": True},
        redoc_url="/redoc",
        generate_unique_id_function=generate_unique_id,
    )

    # --- Middlewares ---
    _add_middlewares(app, settings)

    # --- Exception handlers ---
    _add_exception_handlers(app)

    # --- Routers ---
    _include_routers(app)

    # --- OTel metrics (push to alloy-gateway) ---
    # Skipped in test envs even when OTEL_ENDPOINT is set, so unit tests don't
    # spawn a background export thread. service.name comes from OTEL_SERVICE_NAME
    # / OTEL_RESOURCE_ATTRIBUTES (set on the deployment). Metrics only — no tracer
    # is wired here (alloy-gateway forwards metrics only).
    from observability.telemetry import instrument_app, setup_metrics

    if settings.environment not in ("test", "testing"):
        app.state.meter_provider = setup_metrics(settings.otel_endpoint)
        instrument_app(app, app.state.meter_provider)
    else:
        app.state.meter_provider = None

    # Zero-seed the X-Project-Id counters now that the provider (if any) is set.
    from middleware.project_id import preinit_project_id_metrics

    preinit_project_id_metrics()

    # --- OpenAPI security (KAG standard) ---
    from coding_client.api.openapi_config import configure_openapi

    configure_openapi(
        app,
        protected_prefixes={
            "/api/graphs",
            "/api/validation",
            "/api/cache",
            "/api/grammars",
            "/api/visualization",
            "/api/jobs",
            "/api/health/metrics",
        },
        public_endpoints={"/", "/health", "/docs", "/redoc", "/openapi.json"},
    )

    # --- Replace built-in /openapi.json with lazy async aggregation ---
    # The built-in route calls the sync app.openapi() which only returns
    # gateway-owned paths. We replace it with an async handler that fetches
    # and merges backend specs on demand (cached for 5 min).
    # IMPORTANT: We insert at the same position as the removed route so
    # it sits BEFORE the proxy catch-all /{path:path}.
    _openapi_idx = next(
        (i for i, r in enumerate(app.routes) if hasattr(r, "path") and r.path == "/openapi.json"),
        0,
    )
    app.routes.pop(_openapi_idx)

    @app.get("/openapi.json", include_in_schema=False)
    async def aggregated_openapi():
        """Serve the unified OpenAPI spec (gateway + all backends).

        Lazy: fetches backend specs on first request, then caches for 5 min.
        Falls back to a gateway-only spec if aggregation fails — but that
        fallback is stamped ``x-coding-aggregation.complete = false`` so it is
        never mistaken for a full spec.
        """
        from fastapi.responses import JSONResponse

        from openapi_aggregator import get_aggregated_spec, spec_with_failure_marker

        gateway_spec = app.openapi()
        try:
            merged = await get_aggregated_spec(gateway_spec)
            return JSONResponse(content=merged)
        except Exception as exc:
            logger.warning("OpenAPI aggregation failed, serving gateway-only spec", exc_info=True)
            # Honesty: serving the BARE gateway_spec here dropped the
            # x-coding-aggregation marker entirely, so a consumer gating on
            # `complete` saw a missing key (indistinguishable from an older
            # gateway) instead of an explicit false — a spec containing zero
            # backend paths read as authoritative. Stamp the failure.
            return JSONResponse(content=spec_with_failure_marker(gateway_spec, _describe_exc(exc)))

    # Move the just-appended route to where the old one was
    app.routes.insert(_openapi_idx, app.routes.pop())

    @app.post("/api/openapi/refresh", include_in_schema=False)
    async def refresh_openapi():
        """Invalidate the cached spec and re-fetch from all backends.

        Reports the REAL aggregation state. A refresh that could not reach every
        backend answers 207 with ``status: "partial"`` and the per-backend
        succeeded/failed lists — it previously answered a flat
        ``{"status": "ok"}`` whether one backend was down or all of them were,
        so an operator re-running this after an incident got a green light off a
        spec that was still missing services.
        """
        from fastapi.responses import JSONResponse

        from openapi_aggregator import AGGREGATION_EXT, failure_marker, get_aggregated_spec
        from openapi_aggregator import invalidate_cache as _invalidate_cache

        _invalidate_cache()
        gateway_spec = app.openapi()
        try:
            merged = await get_aggregated_spec(gateway_spec)
        except Exception as exc:
            logger.error("OpenAPI refresh failed", exc_info=True)
            marker = failure_marker(_describe_exc(exc))
            return JSONResponse(
                status_code=500,
                content={
                    "status": "error",
                    "message": "Failed to refresh OpenAPI spec",
                    "complete": False,
                    "total_paths": 0,
                    "backends": marker["backends"],
                    "succeeded": marker["succeeded"],
                    "failed": marker["failed"],
                    "error": marker["error"],
                },
            )

        ext = merged.get(AGGREGATION_EXT) or {}
        complete = bool(ext.get("complete"))
        failed = list(ext.get("failed", []))
        return JSONResponse(
            # 207 Multi-Status: still 2xx (raise_for_status callers keep working)
            # but distinguishable from a full refresh by anyone checking == 200.
            status_code=200 if complete else 207,
            content={
                "status": "ok" if complete else "partial",
                "message": (
                    "OpenAPI spec refreshed"
                    if complete
                    else f"OpenAPI spec refreshed WITHOUT {len(failed)} backend(s): "
                    + ", ".join(failed)
                ),
                "complete": complete,
                "total_paths": len(merged.get("paths", {})),
                "backends": ext.get("backends", []),
                "succeeded": ext.get("succeeded", []),
                "failed": failed,
            },
        )

    # Move refresh endpoint before the proxy catch-all too
    app.routes.insert(_openapi_idx + 1, app.routes.pop())

    total_creation_time = time.time() - creation_start
    logger.info("FastAPI gateway application created in %.3fs", total_creation_time)

    return app


# ---------------------------------------------------------------------------
# Middleware setup
# ---------------------------------------------------------------------------


def _add_middlewares(app: FastAPI, settings) -> None:
    """Add middlewares in KAG standard order: Security → CORS → RequestLogging → Auth.

    That is the *runtime* (outermost-first) order. Starlette's ``add_middleware``
    inserts at index 0 of ``user_middleware``, so registration below runs in the
    REVERSE of the documented order: Auth first, Security last.
    """
    # Auth middleware — always registered. Handles ENABLE_AUTH=false internally
    # by injecting anonymous user with DEFAULT_TENANT_ID.
    enable_auth = getattr(settings, "enable_auth", True)
    if isinstance(enable_auth, str):
        enable_auth = enable_auth.lower() == "true"

    # X-Project-Id gate — registered BEFORE Auth so it runs INSIDE it and only
    # ever sees requests authentication admitted (a 401 is never counted).
    from middleware.project_id import ProjectIdMiddleware

    app.add_middleware(ProjectIdMiddleware)  # type: ignore[invalid-argument-type]

    try:
        from middleware.auth import AuthMiddleware

        app.add_middleware(AuthMiddleware)
        logger.info("Auth middleware registered (enforce=%s)", enable_auth)
    except ImportError:
        logger.warning("AuthMiddleware not available, skipping")

    # Request logging (generates request_id, adds X-Request-ID + X-Processing-Time)
    app.add_middleware(RequestLoggingMiddleware)  # type: ignore[invalid-argument-type]

    # CORS
    cors_origins = getattr(settings, "cors_origins", ["*"])
    if isinstance(cors_origins, str):
        cors_origins = [o.strip() for o in cors_origins.split(",")]

    allow_creds = "*" not in cors_origins

    app.add_middleware(
        CORSMiddleware,  # type: ignore[invalid-argument-type]
        allow_origins=cors_origins,
        allow_credentials=allow_creds,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

    # Security — registered LAST so it is the OUTERMOST middleware, matching the
    # README's "request processing order" step #1. Rejections therefore happen
    # before any other layer touches the request, and the security response
    # headers are applied to every response including CORS preflights and the
    # error responses produced by inner middleware.
    #
    # Gated on ENABLE_AUTH per the README: with auth off the gateway is running
    # unauthenticated (local/dev), where the input validator's XSS and
    # command-injection query rejections are noise rather than defence.
    if enable_auth:
        try:
            from middleware.security import SecurityMiddleware

            app.add_middleware(SecurityMiddleware)  # type: ignore[invalid-argument-type]
            logger.info("Security middleware registered (outermost)")
        except ImportError:
            logger.warning("SecurityMiddleware not available, skipping")
    else:
        logger.info("Security middleware skipped (ENABLE_AUTH=false)")

    # Suppress /health noise from uvicorn access logs
    logging.getLogger("uvicorn.access").addFilter(HealthCheckFilter())


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------


def _add_exception_handlers(app: FastAPI) -> None:
    """Register all exception handlers for standardized error responses."""
    try:
        from middleware.exception_handlers import register_exception_handlers

        register_exception_handlers(app)
    except ImportError:
        # Fallback: register a basic global exception handler
        from fastapi import Request
        from fastapi.responses import JSONResponse

        @app.exception_handler(Exception)
        async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
            from fastapi import HTTPException

            if isinstance(exc, HTTPException):
                raise exc
            logger.error("Unhandled error: %s", exc, exc_info=True)
            return JSONResponse(
                status_code=500,
                content={"error": {"message": "Internal server error", "status": 500}},
            )


# ---------------------------------------------------------------------------
# Router registration
# ---------------------------------------------------------------------------


def _include_routers(app: FastAPI) -> None:
    """Include all gateway-owned routers and the reverse proxy.

    Gateway-owned routers handle endpoints that live in the gateway itself.
    The proxy router is a catch-all that forwards everything else to backends.
    """

    # --- System-level endpoints ---
    from datetime import UTC, datetime

    _gw_start_time = time.monotonic()

    @app.get("/health", operation_id="health_check")
    async def root_health():
        return {
            "status": "ok",
            "api_version": os.getenv("VERSION", "1.0.0"),
            "uptime_seconds": time.monotonic() - _gw_start_time,
            "components": [
                {"name": "gateway", "status": "ok", "message": "Gateway is running"},
            ],
            "timestamp": datetime.now(UTC).isoformat(),
        }

    @app.get("/", operation_id="root")
    async def root():
        return {
            "service": os.getenv("SERVICE_NAME", "Code Graph Helper API"),
            "version": os.getenv("VERSION", "1.0.0"),
            "docs": "/docs",
            "health": "/health",
        }

    # --- Gateway-owned API routers ---
    from routers.cache import router as cache_router
    from routers.grammars import router as grammars_router
    from routers.graphs import router as graphs_router
    from routers.health import router as health_router
    from routers.jobs import router as jobs_router
    from routers.metrics import router as metrics_router
    from routers.validation import router as validation_router
    from routers.validation_status import router as validation_status_router
    from routers.visualization import router as visualization_router

    app.include_router(health_router, prefix="/api/health", tags=["Health"])
    app.include_router(metrics_router, prefix="/api/health/metrics", tags=["Health - Metrics"])
    app.include_router(cache_router, prefix="/api/cache", tags=["Cache"])
    app.include_router(grammars_router, prefix="/api/grammars", tags=["Grammars"])
    app.include_router(visualization_router, prefix="/api/visualization", tags=["Visualization"])
    app.include_router(validation_router, prefix="/api/validation", tags=["Validation"])
    app.include_router(
        validation_status_router, prefix="/api/validation", tags=["Validation - Status"]
    )
    app.include_router(graphs_router, prefix="/api/graphs", tags=["Code Graphs"])
    app.include_router(jobs_router, prefix="/api/jobs", tags=["Jobs"])

    # --- Reverse proxy (catch-all, must be last) ---
    from routers.proxy import router as proxy_router

    app.include_router(proxy_router, tags=["Proxy"])
