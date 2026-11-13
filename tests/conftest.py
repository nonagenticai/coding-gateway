"""Shared test fixtures for coding-gateway."""

from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Module scope on purpose: this file has `from __future__ import annotations`, so
# every annotation is a string that FastAPI resolves against the *module* globals
# of the annotated callable. A function-local import would not resolve, and the
# require_tenant stub's `request` param would silently degrade to a query
# parameter (422 on every route that depends on it).
from starlette.requests import Request as StarletteRequest


def _install_test_stubs() -> None:  # noqa: C901
    """Install lightweight stubs for optional runtime dependencies used in tests.

    Uses setattr() to assign attributes on dynamically-created ModuleType objects
    so that the type checker doesn't flag unresolved-attribute errors.
    """
    if "coding_client" not in sys.modules:
        # Preserve the REAL validation/metrics middleware modules: gateway's
        # middleware/{validation,metrics}.py are thin shims re-exporting them,
        # and the middleware tests assert real behaviour. Everything else in
        # coding_client stays stubbed below. Imported before the stub install
        # so they resolve; re-registered after so the fake api package can't
        # shadow them.
        import coding_client.api.metrics as _real_api_metrics
        import coding_client.api.validation as _real_api_validation

        # The REAL outbound header builder. Deliberately not stubbed: the
        # gateway's own routes now attach this service's licence through it, and
        # the whole point of those tests is that the credential and the customer
        # tenant land in SEPARATE headers. A stub would be a double narrower than
        # the thing it replaces — it would happily "pass" while the real helper
        # put the token in the wrong header, or dropped it entirely.
        import coding_client.auth.outbound as _real_auth_outbound

        # Settings subclasses the real BaseMicroserviceSettings, so config must
        # be the genuine module (a stub can't stand in for a base class).
        import coding_client.config as _real_config

        # The REAL vouch accessor, for the same reason as outbound above. The
        # proxy asks it whether to forward the caller's credential or substitute
        # ours, so a stub here would be a double narrower than the thing it
        # replaces: it would return whatever the fake was told to, and the tests
        # that distinguish a vouched call from an agreeing one would be theatre.
        # It is a pure read of `request.state` — no settings, no network — so
        # there is nothing to isolate.
        from coding_client.auth._onbehalf import (
            tenant_license_verified as _real_tenant_license_verified,
        )
        from coding_client.auth._onbehalf import tenant_was_vouched as _real_tenant_was_vouched

        coding_client = types.ModuleType("coding_client")
        setattr(coding_client, "__path__", [])  # Make it a package so sub-imports work
        auth_module = types.ModuleType("coding_client.auth")
        setattr(auth_module, "__path__", [])  # Make it a package so sub-imports work
        auth_dependencies_module = types.ModuleType("coding_client.auth.dependencies")
        auth_tenant_module = types.ModuleType("coding_client.auth.tenant")
        auth_models_module = types.ModuleType("coding_client.auth.models")
        models_module = types.ModuleType("coding_client.models")
        responses_module = types.ModuleType("coding_client.models.responses")
        api_module = types.ModuleType("coding_client.api")
        setattr(api_module, "__path__", [])  # Make it a package

        class _User:
            def __init__(self, **kwargs):
                self.sub = kwargs.get("sub", "test-sub")
                self.preferred_username = kwargs.get("preferred_username", "test-user")
                self.tenant_id = kwargs.get("tenant_id", "n8n-dev")
                self.effective_tenant_id = self.tenant_id
                self.email = kwargs.get("email")
                self.organization_id = kwargs.get("organization_id")
                self.realm_access = kwargs.get("realm_access")

            def is_admin(self) -> bool:
                return False

        class _PassthroughMiddleware:
            def __init__(self, app, *args, **kwargs) -> None:
                self.app = app

            async def __call__(self, scope, receive, send) -> None:
                await self.app(scope, receive, send)

        def _http_exception(status_code: int, detail):
            from fastapi import HTTPException

            return HTTPException(status_code=status_code, detail=detail)

        setattr(auth_module, "AuthMiddleware", _PassthroughMiddleware)
        setattr(auth_module, "TenantMiddleware", _PassthroughMiddleware)
        setattr(auth_module, "User", _User)
        setattr(auth_models_module, "User", _User)
        setattr(auth_module, "check_tenant_access", lambda *args, **kwargs: True)
        setattr(auth_module, "create_tenant_context", lambda *args, **kwargs: {})
        setattr(auth_module, "get_current_user", lambda: _User())
        setattr(auth_module, "get_current_user_with_tenant", lambda: _User())
        setattr(auth_module, "get_tenant_id", lambda: "n8n-dev")
        setattr(auth_module, "optional_tenant", lambda: None)
        setattr(auth_module, "require_access_coding", lambda: None)
        setattr(auth_module, "require_role", lambda *args, **kwargs: None)
        setattr(auth_module, "require_same_tenant", lambda *args, **kwargs: None)

        async def _require_tenant(request: StarletteRequest) -> str:
            """Mirror the real coding_client signature: async, takes the connection.

            The real ``require_tenant(request)`` is a coroutine accepting a
            Starlette ``HTTPConnection``. The proxy's WebSocket path awaits it
            DIRECTLY (not via ``Depends``), so a zero-arg sync stub would raise
            TypeError there — which `_resolve_ws_tenant` catches as "fail closed",
            silently making every WS test vacuous.

            The annotation must be a type FastAPI resolves as a *special* param
            (Request/WebSocket/HTTPConnection). An un-annotated ``connection=None``
            is treated as a query parameter and injected into every route that
            depends on it, which 422s eight graph-router tests.
            """
            return "n8n-dev"

        setattr(auth_module, "require_tenant", _require_tenant)
        setattr(auth_module, "require_tenant_context", lambda: {})
        setattr(auth_module, "tenant_was_vouched", _real_tenant_was_vouched)
        setattr(auth_module, "tenant_license_verified", _real_tenant_license_verified)
        setattr(auth_dependencies_module, "require_any_role", lambda *args, **kwargs: None)
        setattr(auth_tenant_module, "DEFAULT_TENANT_ID", "n8n-dev")

        setattr(responses_module, "bad_request", lambda message: _http_exception(400, message))
        setattr(responses_module, "internal_error", lambda message: _http_exception(500, message))
        setattr(
            responses_module,
            "not_found",
            lambda resource, name: _http_exception(404, f"{resource} '{name}' not found"),
        )

        # api module stubs (request logging, exception handlers, openapi config, logging setup)
        api_request_logging_module = types.ModuleType("coding_client.api.request_logging")
        api_exception_handlers_module = types.ModuleType("coding_client.api.exception_handlers")
        api_openapi_config_module = types.ModuleType("coding_client.api.openapi_config")
        api_logging_setup_module = types.ModuleType("coding_client.api.logging_setup")

        import logging as _logging

        class _HealthCheckFilter(_logging.Filter):
            def filter(self, record):
                return "/health" not in record.getMessage()

        class _RequestLoggingMiddleware:
            def __init__(self, app, *args, **kwargs):
                self.app = app

            async def __call__(self, scope, receive, send):
                import time as _time

                if scope["type"] != "http":
                    await self.app(scope, receive, send)
                    return

                start = _time.perf_counter()

                async def send_wrapper(message):
                    if message["type"] == "http.response.start":
                        headers = list(message.get("headers", []))
                        elapsed = f"{_time.perf_counter() - start:.6f}"
                        headers.append((b"x-process-time", elapsed.encode()))
                        headers.append((b"x-request-id", b"test-request-id"))
                        message = {**message, "headers": headers}
                    await send(message)

                await self.app(scope, receive, send_wrapper)

        setattr(api_request_logging_module, "HealthCheckFilter", _HealthCheckFilter)
        setattr(api_request_logging_module, "RequestLoggingMiddleware", _RequestLoggingMiddleware)
        setattr(
            api_exception_handlers_module, "register_exception_handlers", lambda app, **kwargs: None
        )
        setattr(api_openapi_config_module, "configure_openapi", lambda app, **kwargs: None)
        setattr(api_logging_setup_module, "configure_logging", lambda *args, **kwargs: None)

        # exceptions module stub
        from fastapi import HTTPException as _HTTPException

        exceptions_module = types.ModuleType("coding_client.exceptions")

        class _ValidationException(_HTTPException):
            def __init__(
                self,
                message="Validation failed",
                error_code="VALIDATION_FAILED",
                field=None,
                details=None,
                **kwargs,
            ):
                detail: dict[str, object] = {"message": message, "error_code": error_code}
                if field:
                    detail.setdefault("details", {})["field"] = field  # ty: ignore[invalid-assignment]
                if details:
                    detail["details"] = {**(detail.get("details") or {}), **details}  # ty: ignore[invalid-argument-type]
                super().__init__(status_code=400, detail=detail)
                self.error_code = error_code
                self.error_details = details

        setattr(exceptions_module, "ValidationException", _ValidationException)
        setattr(exceptions_module, "BaseAPIException", _HTTPException)

        sys.modules["coding_client"] = coding_client
        sys.modules["coding_client.api"] = api_module
        sys.modules["coding_client.api.request_logging"] = api_request_logging_module
        sys.modules["coding_client.api.exception_handlers"] = api_exception_handlers_module
        sys.modules["coding_client.api.openapi_config"] = api_openapi_config_module
        sys.modules["coding_client.api.logging_setup"] = api_logging_setup_module
        # Real middleware modules (shims re-export these); registered after the
        # fake api package so they are not shadowed by it.
        sys.modules["coding_client.api.metrics"] = _real_api_metrics
        sys.modules["coding_client.api.validation"] = _real_api_validation
        sys.modules["coding_client.config"] = _real_config
        sys.modules["coding_client.auth"] = auth_module
        sys.modules["coding_client.auth.dependencies"] = auth_dependencies_module
        sys.modules["coding_client.auth.models"] = auth_models_module
        sys.modules["coding_client.auth.tenant"] = auth_tenant_module
        sys.modules["coding_client.auth.outbound"] = _real_auth_outbound
        sys.modules["coding_client.exceptions"] = exceptions_module
        sys.modules["coding_client.models"] = models_module
        sys.modules["coding_client.models.responses"] = responses_module

    if "models.database" not in sys.modules:
        models_database_module = types.ModuleType("models.database")

        class _Base:
            pass

        class _SchemaVersion:
            pass

        class _NodeAudit:
            pass

        class _Node:
            pass

        class _Edge:
            pass

        setattr(models_database_module, "Base", _Base)
        setattr(models_database_module, "SchemaVersion", _SchemaVersion)
        setattr(models_database_module, "NodeAudit", _NodeAudit)
        setattr(models_database_module, "Node", _Node)
        setattr(models_database_module, "Edge", _Edge)

        sys.modules["models.database"] = models_database_module


_install_test_stubs()


@pytest.fixture(autouse=True)
def _test_env(monkeypatch):
    """Set environment variables for all tests."""
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("ENABLE_AUTH", "false")
    monkeypatch.setenv("ENABLE_DOCS", "true")
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://ci_test@postgres:5432/coding_gateway_ci"
    )
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("SERVICE_NAME", "Code Graph Helper API")
    # Backend service URLs — required-in-production fields. Tests pin the same
    # in-cluster service-name placeholders the prod ConfigMap supplies so any
    # http-client adapter sees a parseable baseUrl.
    monkeypatch.setenv("ANALYSIS_URL", "http://coding-analysis:8688")
    monkeypatch.setenv("TESTING_URL", "http://coding-testing:8689")
    monkeypatch.setenv("AI_URL", "http://coding-ai:8690")
    monkeypatch.setenv("CONTAINERS_URL", "http://coding-containers:8691")
    monkeypatch.setenv("GITHUB_URL", "http://coding-github:8692")
    monkeypatch.setenv("SECURITY_URL", "http://coding-security:8693")
    monkeypatch.setenv("REALTIME_URL", "http://coding-realtime:8694")
    monkeypatch.setenv("PUBLIC_SERVER_URL", "https://coding.example.com")
    monkeypatch.setenv("OTEL_ENDPOINT", "http://otel-collector:4318")
    monkeypatch.setenv("PYTEST_IMAGE", "ghcr.io/nonagenticai/pytest-runner:latest")
    # Clear the lru_cache so settings reload with test env
    from config.settings import get_settings

    get_settings.cache_clear()


@pytest.fixture
def settings():
    """Return a fresh Settings instance with test overrides."""
    from config.settings import get_settings

    return get_settings()


@pytest.fixture
def mock_cache_manager():
    """Mock cache manager for tests."""
    cm = AsyncMock()
    cm.get = AsyncMock(return_value=None)
    cm.set = AsyncMock(return_value=True)
    cm.close = AsyncMock()
    cm.get_stats = MagicMock(return_value={"hits": 0, "misses": 0, "size": 0})
    cm.get_redis = AsyncMock(return_value=MagicMock())
    return cm


@pytest.fixture
def mock_db_session():
    """Mock database session."""
    session = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.close = AsyncMock()
    return session


@pytest.fixture(autouse=True)
def _stub_analysis_get_404(request, _test_env):
    """Default the coding-analysis proxy helpers to an "empty system".

    Every graph handler now forwards to coding-analysis over HTTP, which is
    unreachable in unit tests. Without these stubs the outer exception handler
    wraps the connection error as a 500 and tests expecting 404 / empty-list
    break. Defaults:

    - ``_analysis_get`` on the collection path (``/api/analysis/graphs``)
      returns an empty list (200); any per-container path returns 404.
    - ``_analysis_delete`` returns 404 ("nothing to delete").

    Tests that need specific upstream payloads override these locally with
    their own ``patch("routers.graphs._analysis_*", …)`` — the nested patch
    wins. Tests that want the real call opt out with
    ``@pytest.mark.no_analysis_stub``.

    Depends on ``_test_env`` so Settings env vars are in place before we
    import routers.graphs (which triggers config loading).
    """
    if "no_analysis_stub" in request.keywords:
        yield
        return

    import httpx as _httpx

    def _resp(status, body, path="/api/analysis/graphs/_stub"):
        req = _httpx.Request("GET", f"http://coding-analysis{path}")
        return _httpx.Response(status, json=body, request=req)

    async def _stub_get(path="/api/analysis/graphs", *args, **kwargs):
        if path.rstrip("/") == "/api/analysis/graphs":
            return _resp(200, {"graphs": [], "total": 0}, path)
        return _resp(404, {"detail": "not mocked"}, path)

    async def _stub_delete(path="/api/analysis/graphs/_stub", *args, **kwargs):
        return _resp(404, {"detail": "not mocked"}, path)

    # Patch by target string so the import happens lazily, after
    # Settings has been configured by _test_env.
    with (
        patch("routers.graphs._analysis_get", new=_stub_get),
        patch("routers.graphs._analysis_delete", new=_stub_delete),
    ):
        yield


@pytest.fixture
async def app(mock_cache_manager):
    """Create a test FastAPI application."""
    with patch("services.cache_manager.get_cache_manager", return_value=mock_cache_manager):
        from app_factory import create_app

        application = create_app()
        yield application


@pytest.fixture
async def client(app):
    """Async HTTP test client."""
    import httpx
    from httpx._transports.asgi import ASGITransport

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
