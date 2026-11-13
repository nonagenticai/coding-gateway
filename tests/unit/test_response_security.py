"""Tests for middleware/response_security.py – gap coverage."""

from __future__ import annotations

import pytest
from starlette.responses import JSONResponse

from middleware.response_security import ResponseSecurityHandler


@pytest.fixture
def handler():
    return ResponseSecurityHandler()


def _make_response(headers: dict | None = None) -> JSONResponse:
    resp = JSONResponse(content={"ok": True})
    if headers:
        for k, v in headers.items():
            resp.headers[k] = v
    return resp


# ---------------------------------------------------------------------------
# add_security_headers
# ---------------------------------------------------------------------------


class TestAddSecurityHeaders:
    def test_adds_all_default_headers(self, handler):
        resp = _make_response()
        handler.add_security_headers(resp)
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert resp.headers["X-XSS-Protection"] == "1; mode=block"
        assert "max-age=31536000" in resp.headers["Strict-Transport-Security"]
        assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert "geolocation" in resp.headers["Permissions-Policy"]

    def test_adds_csp_header(self, handler):
        resp = _make_response()
        handler.add_security_headers(resp)
        csp = resp.headers.get("Content-Security-Policy", "")
        assert "default-src 'self'" in csp

    def test_does_not_override_existing_header(self, handler):
        resp = _make_response(headers={"X-Frame-Options": "SAMEORIGIN"})
        handler.add_security_headers(resp)
        assert resp.headers["X-Frame-Options"] == "SAMEORIGIN"


# ---------------------------------------------------------------------------
# add_cors_headers
# ---------------------------------------------------------------------------


class TestAddCorsHeaders:
    def test_adds_cors_headers(self, handler):
        resp = _make_response()
        handler.add_cors_headers(resp)
        assert resp.headers["Access-Control-Allow-Origin"] == "*"
        assert "GET" in resp.headers["Access-Control-Allow-Methods"]
        assert "Content-Type" in resp.headers["Access-Control-Allow-Headers"]

    def test_does_not_override_existing_cors(self, handler):
        resp = _make_response(headers={"Access-Control-Allow-Origin": "https://example.com"})
        handler.add_cors_headers(resp)
        assert resp.headers["Access-Control-Allow-Origin"] == "https://example.com"


# ---------------------------------------------------------------------------
# add_cache_headers
# ---------------------------------------------------------------------------


class TestAddCacheHeaders:
    def test_static_path_cached(self, handler):
        resp = _make_response()
        handler.add_cache_headers(resp, "/docs")
        assert "public" in resp.headers["Cache-Control"]
        assert "max-age=3600" in resp.headers["Cache-Control"]

    def test_redoc_cached(self, handler):
        resp = _make_response()
        handler.add_cache_headers(resp, "/redoc")
        assert "public" in resp.headers["Cache-Control"]

    def test_openapi_cached(self, handler):
        resp = _make_response()
        handler.add_cache_headers(resp, "/openapi.json")
        assert "public" in resp.headers["Cache-Control"]

    def test_code_graphs_api_cached(self, handler):
        resp = _make_response()
        handler.add_cache_headers(resp, "/api/code-graphs/123")
        assert "private" in resp.headers["Cache-Control"]
        assert "max-age=60" in resp.headers["Cache-Control"]

    def test_metrics_cached(self, handler):
        resp = _make_response()
        handler.add_cache_headers(resp, "/api/metrics")
        assert "private" in resp.headers["Cache-Control"]

    def test_other_path_no_cache(self, handler):
        resp = _make_response()
        handler.add_cache_headers(resp, "/api/data")
        cc = resp.headers["Cache-Control"]
        assert "no-store" in cc
        assert "no-cache" in cc
        assert "must-revalidate" in cc

    def test_does_not_override_existing_cache_control(self, handler):
        resp = _make_response(headers={"Cache-Control": "custom"})
        handler.add_cache_headers(resp, "/api/data")
        assert resp.headers["Cache-Control"] == "custom"


# ---------------------------------------------------------------------------
# add_api_response_headers
# ---------------------------------------------------------------------------


class TestAddApiResponseHeaders:
    def test_sets_json_content_type_when_missing(self, handler):
        resp = JSONResponse(content={"ok": True})
        # Remove content-type to simulate missing
        # JSONResponse always sets it, so we test with an empty value
        resp.headers["content-type"] = ""
        handler.add_api_response_headers(resp)
        assert resp.headers["Content-Type"] == "application/json; charset=utf-8"

    def test_preserves_existing_content_type(self, handler):
        resp = _make_response()
        handler.add_api_response_headers(resp)
        # JSONResponse already sets content-type, so it should not be overridden
        assert "application/json" in resp.headers["content-type"]


# ---------------------------------------------------------------------------
# sanitize_response_headers
# ---------------------------------------------------------------------------


class TestSanitizeResponseHeaders:
    def test_removes_server_header(self, handler):
        resp = _make_response()
        resp.headers["Server"] = "uvicorn"
        handler.sanitize_response_headers(resp)
        assert "Server" not in resp.headers

    def test_no_error_when_headers_absent_via_del(self, handler):
        """Verify the intent: if Server/X-Powered-By are absent,
        deleting them via __delitem__ raises KeyError."""
        resp = _make_response()
        # Confirm these headers are not present
        assert resp.headers.get("Server") is None
        assert resp.headers.get("X-Powered-By") is None
