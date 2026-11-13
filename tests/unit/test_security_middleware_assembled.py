"""SecurityMiddleware behaviour on the ASSEMBLED app.

Why this file exists
--------------------
``tests/unit/test_request_security_middleware.py`` instantiates
``SecurityMiddleware(app=MagicMock())`` directly and drives ``dispatch()``.
That proves the *class* works, but it passes identically whether or not
``app_factory._add_middlewares`` ever registers it — which is exactly how the
middleware stayed unregistered (and therefore inert in production) while CI
stayed green.

Every test here goes through ``create_app()`` and, where behaviour is claimed,
through a real ``TestClient`` request. A registration-only assertion is not
enough: it would still pass if the middleware were registered but broken.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI, Request
from starlette.testclient import TestClient

# A GET route with no auth/DB/cache dependencies — the cheapest real trip
# through the whole middleware stack.
_PROBE_PATH = "/api/health/ready"


def _middleware_names(app: FastAPI) -> list[str]:
    """Outermost-first list of registered middleware class names.

    ``app.user_middleware`` is ordered outermost-first: ``add_middleware``
    inserts at index 0, so the LAST registered middleware runs FIRST.
    """
    return [m.cls.__name__ for m in app.user_middleware if hasattr(m, "cls")]  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]


@pytest.fixture
def secure_app(monkeypatch, mock_cache_manager):
    """Assembled app with ENABLE_AUTH=true (the README's gating condition).

    The autouse ``_test_env`` fixture pins ENABLE_AUTH=false, so it must be
    overridden *and* the settings lru_cache cleared before create_app().
    """
    from config.settings import get_settings

    monkeypatch.setenv("ENABLE_AUTH", "true")
    get_settings.cache_clear()
    try:
        with patch("services.cache_manager.get_cache_manager", return_value=mock_cache_manager):
            from app_factory import create_app

            yield create_app()
    finally:
        get_settings.cache_clear()


@pytest.fixture
def secure_client(secure_app):
    """TestClient that does NOT run lifespan (no cache/DB startup needed)."""
    return TestClient(secure_app, raise_server_exceptions=False)


class TestSecurityMiddlewareRegistration:
    def test_registered_in_assembled_app(self, secure_app):
        assert "SecurityMiddleware" in _middleware_names(secure_app)

    def test_is_outermost(self, secure_app):
        """README documents SecurityMiddleware as request-processing step #1.

        Outermost == index 0 of user_middleware == registered last.
        """
        assert _middleware_names(secure_app)[0] == "SecurityMiddleware"

    def test_documented_runtime_order_holds(self, secure_app):
        """Security -> CORS -> RequestLogging -> Auth (outermost first)."""
        names = _middleware_names(secure_app)
        expected = ["SecurityMiddleware", "CORSMiddleware"]
        assert names[: len(expected)] == expected
        # Auth is registered first, so it must be the innermost of the four.
        auth_idx = next(
            i for i, n in enumerate(names) if n in ("AuthMiddleware", "_PassthroughMiddleware")
        )
        logging_idx = next(
            i
            for i, n in enumerate(names)
            if n in ("RequestLoggingMiddleware", "_RequestLoggingMiddleware")
        )
        assert names.index("CORSMiddleware") < logging_idx < auth_idx

    def test_not_registered_when_auth_disabled(self, mock_cache_manager):
        """ENABLE_AUTH=false (the autouse default) — README gates it on auth."""
        with patch("services.cache_manager.get_cache_manager", return_value=mock_cache_manager):
            from app_factory import create_app

            app = create_app()
        assert "SecurityMiddleware" not in _middleware_names(app)


class TestSecurityHeadersOnRealRequests:
    """Registration is not enough — the headers must reach the wire."""

    def test_security_headers_present(self, secure_client):
        resp = secure_client.get(_PROBE_PATH)
        assert resp.status_code == 200, resp.text
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["x-frame-options"] == "DENY"
        assert "max-age=31536000" in resp.headers["strict-transport-security"]
        assert resp.headers["referrer-policy"] == "strict-origin-when-cross-origin"
        assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]

    def test_cache_control_applied(self, secure_client):
        resp = secure_client.get(_PROBE_PATH)
        assert resp.headers["cache-control"] == "no-store, no-cache, must-revalidate"

    def test_headers_absent_without_the_middleware(self, mock_cache_manager):
        """Counterpart proof: with ENABLE_AUTH=false the headers are gone.

        Guards against the assertions above passing for some unrelated reason
        (e.g. another layer happening to set the same header).
        """
        with patch("services.cache_manager.get_cache_manager", return_value=mock_cache_manager):
            from app_factory import create_app

            app = create_app()
        resp = TestClient(app, raise_server_exceptions=False).get(_PROBE_PATH)
        assert resp.status_code == 200, resp.text
        assert "x-content-type-options" not in resp.headers
        assert "content-security-policy" not in resp.headers


class TestRequestValidationOnRealRequests:
    def test_path_traversal_blocked(self, secure_client):
        """Percent-encoded traversal — the form that actually reaches the app.

        A literal ``../`` is collapsed by httpx/TestClient (and by most proxies)
        before it is ever sent, so encoding it is both the realistic attack and
        the only way to drive the validator's ``unquote`` branch end-to-end.
        """
        resp = secure_client.get("/api/health/%2e%2e%2f%2e%2e%2fetc/passwd")
        assert resp.status_code == 400
        assert "Invalid request path" in resp.text

    def test_xss_query_value_blocked(self, secure_client):
        resp = secure_client.get(_PROBE_PATH, params={"q": "<script>alert(1)</script>"})
        assert resp.status_code == 400
        assert "Invalid query parameter value" in resp.text

    def test_bad_query_key_blocked(self, secure_client):
        resp = secure_client.get(f"{_PROBE_PATH}?9bad$key=1")
        assert resp.status_code == 400
        assert "Invalid query parameter name" in resp.text

    def test_oversized_body_rejected(self, secure_client):
        """content-length above the 10MB non-permissive cap -> 413."""
        resp = secure_client.post(
            "/api/graphs",
            content=b"{}",
            headers={
                "content-type": "application/json",
                "content-length": str(11 * 1024 * 1024),
            },
        )
        assert resp.status_code == 413

    def test_benign_request_passes_through(self, secure_client):
        resp = secure_client.get(_PROBE_PATH, params={"verbose": "true"})
        assert resp.status_code == 200, resp.text


class TestBodyIsReplayedDownstream:
    """BaseHTTPMiddleware reads the body; the route must still see it.

    SecurityMiddleware calls ``await request.body()`` on POST/PUT/PATCH. If
    Starlette's _CachedRequest replay ever regresses, the downstream handler
    would see an empty body — a silent, total breakage of every write path on
    the gateway. This test would catch that.
    """

    def _app_with_echo(self, mock_cache_manager) -> FastAPI:
        from config.settings import get_settings

        get_settings.cache_clear()
        with patch("services.cache_manager.get_cache_manager", return_value=mock_cache_manager):
            from app_factory import create_app

            app = create_app()

        @app.post("/_test/echo")
        async def _echo(request: Request):  # pragma: no cover - exercised via client
            return {"body": (await request.body()).decode()}

        # The gateway installs a proxy catch-all; a route appended after
        # create_app() would never be reached. Move it to the front.
        app.routes.insert(0, app.routes.pop())
        return app

    def test_json_body_reaches_the_route(self, monkeypatch, mock_cache_manager):
        from config.settings import get_settings

        monkeypatch.setenv("ENABLE_AUTH", "true")
        get_settings.cache_clear()
        try:
            app = self._app_with_echo(mock_cache_manager)
            assert "SecurityMiddleware" in _middleware_names(app)
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post("/_test/echo", json={"name": "hello"})
            assert resp.status_code == 200, resp.text
            assert resp.json()["body"] == '{"name":"hello"}'
        finally:
            get_settings.cache_clear()

    def test_malicious_json_body_blocked(self, monkeypatch, mock_cache_manager):
        from config.settings import get_settings

        monkeypatch.setenv("ENABLE_AUTH", "true")
        get_settings.cache_clear()
        try:
            app = self._app_with_echo(mock_cache_manager)
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post("/_test/echo", json={"name": "<script>alert(1)</script>"})
            assert resp.status_code == 400
            assert "Invalid request body" in resp.text
        finally:
            get_settings.cache_clear()


class TestDocsRateLimit:
    def test_docs_rate_limited_after_30_requests(self, secure_client):
        codes = [secure_client.get("/openapi.json").status_code for _ in range(31)]
        assert codes[:30] == [200] * 30, codes[:30]
        assert codes[30] == 429

    def test_docs_response_carries_security_headers(self, secure_client):
        resp = secure_client.get("/openapi.json")
        assert resp.status_code == 200
        assert resp.headers["x-content-type-options"] == "nosniff"
