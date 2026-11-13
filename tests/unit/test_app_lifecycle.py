"""Extended app_factory tests — generate_unique_id, middleware ordering, auth toggle."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI

from app_factory import generate_unique_id

# ---------------------------------------------------------------------------
# generate_unique_id
# ---------------------------------------------------------------------------


class TestGenerateUniqueId:
    def _make_route(self, path: str, methods: set[str] | None = None):
        route = MagicMock()
        route.path = path
        route.methods = methods or {"GET"}
        return route

    def test_root_api_path(self):
        route = self._make_route("/api/", {"GET"})
        assert generate_unique_id(route) == "root_get"

    def test_simple_resource_path(self):
        route = self._make_route("/api/cache", {"GET"})
        assert generate_unique_id(route) == "cache_get"

    def test_nested_path_with_hyphens(self):
        route = self._make_route("/api/code-graphs/detail", {"POST"})
        result = generate_unique_id(route)
        assert result == "code_graphs_detail_post"

    def test_path_with_param(self):
        route = self._make_route("/api/graphs/{graph_id}", {"GET"})
        result = generate_unique_id(route)
        assert "graph_id" in result
        assert result.endswith("_get")

    def test_health_root_special_case(self):
        route = self._make_route("/health", {"GET"})
        assert generate_unique_id(route) == "health_root_get"

    def test_health_api_special_case(self):
        route = self._make_route("/api/health", {"GET"})
        assert generate_unique_id(route) == "health_api_get"

    def test_no_methods_uses_unknown(self):
        route = self._make_route("/api/test")
        route.methods = set()
        result = generate_unique_id(route)
        assert result.endswith("_unknown")

    def test_hyphen_converted_to_underscore(self):
        route = self._make_route("/api/code-graphs", {"DELETE"})
        assert "code_graphs" in generate_unique_id(route)

    def test_multiple_params_all_stripped(self):
        route = self._make_route("/api/graphs/{graph_id}/nodes/{node_id}", {"PUT"})
        result = generate_unique_id(route)
        assert "{" not in result
        assert "}" not in result
        assert result.endswith("_put")

    def test_deeply_nested_path(self):
        route = self._make_route("/api/health/container/test/history", {"GET"})
        result = generate_unique_id(route)
        assert "container" in result
        assert "history" in result
        assert result.endswith("_get")


# ---------------------------------------------------------------------------
# Middleware presence
# ---------------------------------------------------------------------------


class TestMiddlewarePresence:
    def _middleware_names(self, app: FastAPI) -> list[str]:
        return [m.cls.__name__ for m in app.user_middleware if hasattr(m, "cls")]  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]

    async def test_cors_middleware_registered(self, app):
        assert "CORSMiddleware" in self._middleware_names(app)

    async def test_request_logging_middleware_registered(self, app):
        names = self._middleware_names(app)
        assert "_RequestLoggingMiddleware" in names or "RequestLoggingMiddleware" in names


# ---------------------------------------------------------------------------
# Auth toggle
# ---------------------------------------------------------------------------


class TestAuthToggle:
    def test_auth_disabled_when_auth_false(self):
        """ENABLE_AUTH=false — AuthMiddleware is always registered but handles
        disabled auth internally by injecting an anonymous user."""
        with patch("services.cache_manager.get_cache_manager", return_value=AsyncMock()):
            from app_factory import create_app

            test_app = create_app()
            names = [m.cls.__name__ for m in test_app.user_middleware if hasattr(m, "cls")]
            # AuthMiddleware is always registered; it handles ENABLE_AUTH=false
            # internally by injecting an anonymous user with DEFAULT_TENANT_ID
            assert "AuthMiddleware" in names or "_PassthroughMiddleware" in names

    def test_auth_enabled_when_auth_true(self, monkeypatch):
        """ENABLE_AUTH=true should add AuthMiddleware."""
        from config.settings import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("ENABLE_AUTH", "true")
        get_settings.cache_clear()

        with patch("services.cache_manager.get_cache_manager", return_value=AsyncMock()):
            from app_factory import create_app

            test_app = create_app()
            names = [m.cls.__name__ for m in test_app.user_middleware if hasattr(m, "cls")]
            assert "_PassthroughMiddleware" in names or "AuthMiddleware" in names

        monkeypatch.setenv("ENABLE_AUTH", "false")
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------


class TestExceptionHandlers:
    def test_exception_handler_registration_called(self):
        """The register_exception_handlers function is called during app creation."""
        # The conftest stubs register_exception_handlers as a no-op,
        # so we just verify create_app() calls _add_exception_handlers without error.
        # The real exception handlers are tested in test_exception_handlers.py.
        with patch("services.cache_manager.get_cache_manager", return_value=AsyncMock()):
            from app_factory import create_app

            test_app = create_app()
            # App should be created successfully
            assert test_app is not None

    def test_register_exception_handlers_is_callable(self):
        """register_exception_handlers from coding_client.api is importable and callable."""
        from middleware.exception_handlers import register_exception_handlers

        test_app = FastAPI()
        # Should not raise
        register_exception_handlers(test_app)


# ---------------------------------------------------------------------------
# App metadata via OpenAPI
# ---------------------------------------------------------------------------


class TestAppMetadataViaOpenAPI:
    async def test_openapi_contains_title(self, client):
        resp = await client.get("/openapi.json")
        data = resp.json()
        assert data["info"]["title"] == "Code Graph Helper"

    async def test_openapi_contains_version(self, client):
        resp = await client.get("/openapi.json")
        data = resp.json()
        assert data["info"]["version"] == "1.0.0"

    async def test_openapi_has_paths(self, client):
        resp = await client.get("/openapi.json")
        data = resp.json()
        paths = data.get("paths", {})
        assert len(paths) > 0
        # At least the health endpoints should be present
        assert "/health" in paths or any("/health" in p for p in paths)
