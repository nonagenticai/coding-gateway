"""Tests for app_factory.py — verifies create_app() builds a working FastAPI app
with routes registered, middleware configured, and docs enabled."""

from __future__ import annotations

from fastapi import FastAPI


def _route_paths(routes) -> list[str]:
    """All effective paths reachable from ``routes``.

    fastapi>=0.137 stopped flattening ``include_router()`` sub-routes into
    ``app.routes``: they are wrapped in a private ``_IncludedRouter`` that has
    no ``.path`` (https://github.com/fastapi/fastapi/releases/tag/0.137.0), so
    the old ``[r.path for r in app.routes]`` raises
    ``AttributeError: '_IncludedRouter' object has no attribute 'path'``.
    ``fastapi.routing.iter_route_contexts`` is the public accessor (added
    0.137.2) that resolves either shape to a flat sequence of contexts
    carrying the real, fully-prefixed ``.path``.
    """
    try:
        from fastapi.routing import iter_route_contexts
    except ImportError:  # fastapi < 0.137.2: app.routes is already flat
        return [r.path for r in routes if hasattr(r, "path")]
    return [ctx.path for ctx in iter_route_contexts(routes) if ctx.path is not None]


class TestCreateApp:
    """create_app() returns a fully configured FastAPI instance."""

    async def test_returns_fastapi_instance(self, app):
        assert isinstance(app, FastAPI)

    async def test_root_health_route_exists(self, app):
        paths = _route_paths(app.routes)
        assert "/health" in paths

    async def test_root_route_exists(self, app):
        paths = _route_paths(app.routes)
        assert "/" in paths

    async def test_health_api_routes_registered(self, app):
        paths = _route_paths(app.routes)
        assert any("/api/health" in p for p in paths)

    async def test_cache_routes_registered(self, app):
        paths = _route_paths(app.routes)
        assert any("/api/cache" in p for p in paths)

    async def test_grammars_routes_registered(self, app):
        paths = _route_paths(app.routes)
        assert any("/api/grammars" in p for p in paths)

    async def test_visualization_routes_registered(self, app):
        paths = _route_paths(app.routes)
        assert any("/api/visualization" in p for p in paths)

    async def test_validation_routes_registered(self, app):
        paths = _route_paths(app.routes)
        assert any("/api/validation" in p for p in paths)

    async def test_graphs_routes_registered(self, app):
        paths = _route_paths(app.routes)
        assert any("/api/graphs" in p for p in paths)

    async def test_jobs_routes_registered(self, app):
        paths = _route_paths(app.routes)
        assert any("/api/jobs" in p for p in paths)


class TestDocsEnabled:
    """In test environment, OpenAPI docs endpoints should be accessible."""

    async def test_docs_url_configured(self, app):
        assert app.docs_url == "/docs"

    async def test_redoc_url_configured(self, app):
        assert app.redoc_url == "/redoc"

    async def test_openapi_url_configured(self, app):
        assert app.openapi_url == "/openapi.json"

    async def test_docs_accessible_via_http(self, client):
        resp = await client.get("/docs")
        # Docs page returns HTML
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    async def test_redoc_accessible_via_http(self, client):
        resp = await client.get("/redoc")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    async def test_openapi_json_accessible(self, client):
        resp = await client.get("/openapi.json")
        assert resp.status_code == 200
        data = resp.json()
        assert "paths" in data
        assert "info" in data


class TestCorsMiddleware:
    """CORS middleware is configured and responds to cross-origin requests."""

    async def test_cors_headers_on_preflight(self, client):
        resp = await client.options(
            "/health",
            headers={
                "Origin": "http://example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.status_code == 200
        assert "access-control-allow-origin" in resp.headers

    async def test_cors_allows_expected_methods(self, client):
        resp = await client.options(
            "/health",
            headers={
                "Origin": "http://example.com",
                "Access-Control-Request-Method": "POST",
            },
        )
        allowed = resp.headers.get("access-control-allow-methods", "")
        assert "GET" in allowed
        assert "POST" in allowed

    async def test_cors_exposes_all_headers(self, client):
        """KAG standard: expose_headers=["*"]."""
        resp = await client.get(
            "/health",
            headers={"Origin": "http://example.com"},
        )
        exposed = resp.headers.get("access-control-expose-headers", "")
        assert exposed == "*"


class TestAppMetadata:
    """App title, version, and description are set correctly."""

    async def test_title(self, app):
        assert app.title == "Code Graph Helper API"

    async def test_version(self, app):
        assert app.version == "1.0.0"

    async def test_description_not_empty(self, app):
        assert app.description
        assert len(app.description) > 50


class TestRoutesServeTraffic:
    """Verify the app actually serves traffic on key routes."""

    async def test_root_returns_json(self, client):
        resp = await client.get("/")
        assert resp.status_code == 200
        data = resp.json()
        assert "service" in data
        assert "version" in data

    async def test_health_returns_json(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

    async def test_ready_returns_json(self, client):
        resp = await client.get("/api/health/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["health"]["status"] == "healthy"
