"""Tests for the security middleware dispatch pipeline and helpers.

Tests SecurityMiddleware.dispatch, RequestSecurityChecker, ResponseSecurityHandler,
and InputValidator by constructing real Request objects and calling the middleware
directly.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Response
from starlette.responses import Response as StarletteResponse

from middleware.security import SecurityMiddleware

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(
    path: str = "/api/items",
    method: str = "GET",
    headers: list[tuple[bytes, bytes]] | None = None,
    query_string: bytes = b"",
    client: tuple[str, int] = ("127.0.0.1", 1234),
):
    from fastapi import Request

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers or [],
        "query_string": query_string,
        "client": client,
        "scheme": "http",
        "server": ("testserver", 80),
    }
    return Request(scope)


def _middleware() -> SecurityMiddleware:
    return SecurityMiddleware(app=MagicMock())


def _ok_response() -> StarletteResponse:
    return StarletteResponse("ok", status_code=200)


# ---------------------------------------------------------------------------
# Allowed requests pass through
# ---------------------------------------------------------------------------


class TestAllowedRequests:
    @pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    async def test_allowed_methods_pass(self, method):
        mw = _middleware()
        req = _make_request("/api/items", method)
        call_next = AsyncMock(return_value=_ok_response())
        result = await mw.dispatch(req, call_next)
        assert result.status_code == 200
        call_next.assert_awaited_once()


# ---------------------------------------------------------------------------
# Blocked HTTP methods
# ---------------------------------------------------------------------------


class TestBlockedMethods:
    @pytest.mark.parametrize("method", ["TRACE", "CONNECT"])
    async def test_blocked_method_returns_405(self, method):
        mw = _middleware()
        req = _make_request("/api/items", method)
        call_next = AsyncMock()
        result = await mw.dispatch(req, call_next)
        assert result.status_code == 405
        call_next.assert_not_awaited()


# ---------------------------------------------------------------------------
# Request size checks
# ---------------------------------------------------------------------------


class TestRequestSize:
    async def test_oversized_request_returns_413(self):
        mw = _middleware()
        over_limit = str(10 * 1024 * 1024 + 1).encode()
        req = _make_request("/api/items", "POST", headers=[(b"content-length", over_limit)])
        call_next = AsyncMock()
        result = await mw.dispatch(req, call_next)
        assert result.status_code == 413
        call_next.assert_not_awaited()

    async def test_at_limit_request_passes(self):
        mw = _middleware()
        at_limit = str(10 * 1024 * 1024).encode()
        req = _make_request("/api/items", "POST", headers=[(b"content-length", at_limit)])
        call_next = AsyncMock(return_value=_ok_response())
        result = await mw.dispatch(req, call_next)
        assert result.status_code == 200


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------


class TestPathValidation:
    async def test_path_traversal_blocked(self):
        mw = _middleware()
        req = _make_request("/api/../etc/passwd", "GET")
        call_next = AsyncMock()
        result = await mw.dispatch(req, call_next)
        assert result.status_code == 400
        call_next.assert_not_awaited()


# ---------------------------------------------------------------------------
# Query parameter validation
# ---------------------------------------------------------------------------


class TestQueryParameterValidation:
    async def test_invalid_key_blocked(self):
        mw = _middleware()
        req = _make_request("/api/items", "GET", query_string=b"1badkey=value")
        call_next = AsyncMock()
        result = await mw.dispatch(req, call_next)
        assert result.status_code == 400
        call_next.assert_not_awaited()

    async def test_xss_in_value_blocked(self):
        mw = _middleware()
        req = _make_request(
            "/api/items",
            "GET",
            query_string=b"name=%3Cscript%3Ealert(1)%3C/script%3E",
        )
        call_next = AsyncMock()
        result = await mw.dispatch(req, call_next)
        assert result.status_code == 400
        call_next.assert_not_awaited()

    async def test_valid_query_passes(self):
        mw = _middleware()
        req = _make_request("/api/items", "GET", query_string=b"name=hello&page=1")
        call_next = AsyncMock(return_value=_ok_response())
        result = await mw.dispatch(req, call_next)
        assert result.status_code == 200


# ---------------------------------------------------------------------------
# Header validation
# ---------------------------------------------------------------------------


class TestHeaderValidation:
    async def test_null_byte_in_header_blocked(self):
        mw = _middleware()
        req = _make_request("/api/items", "GET", headers=[(b"x-custom", b"value\x00bad")])
        call_next = AsyncMock()
        result = await mw.dispatch(req, call_next)
        assert result.status_code == 400
        call_next.assert_not_awaited()


# ---------------------------------------------------------------------------
# JSON body validation
# ---------------------------------------------------------------------------


class TestJsonBodyValidation:
    async def test_empty_json_body_is_not_a_document(self):
        """POST with Content-Type: application/json and no body (security quick/sbom/image
        scans take none) was answered 400 "Invalid request body" (live 2026-10-12)."""
        mw = _middleware()
        req = _make_request(
            "/api/security/scan/c1/quick",
            "POST",
            headers=[(b"content-type", b"application/json"), (b"content-length", b"0")],
        )
        call_next = AsyncMock(return_value=_ok_response())
        with patch.object(
            mw._request_checker, "get_request_body", new_callable=AsyncMock, return_value=b""
        ):
            result = await mw.dispatch(req, call_next)
        assert result.status_code == 200
        call_next.assert_awaited_once()

    async def test_valid_json_passes(self):
        mw = _middleware()
        req = _make_request(
            "/api/items",
            "POST",
            headers=[
                (b"content-type", b"application/json"),
                (b"content-length", b"17"),
            ],
        )
        call_next = AsyncMock(return_value=_ok_response())
        with patch.object(
            mw._request_checker,
            "get_request_body",
            new_callable=AsyncMock,
            return_value=b'{"key":"value"}',
        ):
            result = await mw.dispatch(req, call_next)
        assert result.status_code == 200

    async def test_invalid_json_blocked(self):
        mw = _middleware()
        req = _make_request(
            "/api/items",
            "POST",
            headers=[
                (b"content-type", b"application/json"),
                (b"content-length", b"10"),
            ],
        )
        call_next = AsyncMock()
        with patch.object(
            mw._request_checker,
            "get_request_body",
            new_callable=AsyncMock,
            return_value=b"{invalid json",
        ):
            result = await mw.dispatch(req, call_next)
        assert result.status_code == 400
        call_next.assert_not_awaited()

    async def test_body_read_returns_none_passes(self):
        """When body read fails and returns None, request continues."""
        mw = _middleware()
        req = _make_request(
            "/api/items",
            "POST",
            headers=[(b"content-type", b"application/json")],
        )
        call_next = AsyncMock(return_value=_ok_response())
        with patch.object(
            mw._request_checker,
            "get_request_body",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await mw.dispatch(req, call_next)
        assert result.status_code == 200

    async def test_non_json_post_skips_body_validation(self):
        mw = _middleware()
        req = _make_request(
            "/api/items",
            "POST",
            headers=[(b"content-type", b"text/plain")],
        )
        call_next = AsyncMock(return_value=_ok_response())
        result = await mw.dispatch(req, call_next)
        assert result.status_code == 200


# ---------------------------------------------------------------------------
# Documentation endpoints (permissive path + rate limiting)
# ---------------------------------------------------------------------------


class TestDocEndpoints:
    @pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
    async def test_doc_endpoints_pass(self, path):
        mw = _middleware()
        req = _make_request(path, "GET")
        call_next = AsyncMock(return_value=_ok_response())
        result = await mw.dispatch(req, call_next)
        assert result.status_code == 200
        assert "x-content-type-options" in result.headers

    async def test_doc_rate_limit_exceeded(self):
        mw = _middleware()
        req = _make_request("/docs", "GET")
        ok = _ok_response()
        # Exhaust the 30-request limit
        for _ in range(30):
            await mw.dispatch(req, AsyncMock(return_value=_ok_response()))
        # 31st request should be rate-limited
        result = await mw.dispatch(req, AsyncMock(return_value=ok))
        assert result.status_code == 429


# ---------------------------------------------------------------------------
# Security and cache headers on responses
# ---------------------------------------------------------------------------


class TestResponseHeaders:
    async def test_security_headers_added(self):
        mw = _middleware()
        req = _make_request("/api/items", "GET")
        resp = Response()
        resp.status_code = 200
        call_next = AsyncMock(return_value=resp)
        result = await mw.dispatch(req, call_next)
        assert result.headers["x-content-type-options"] == "nosniff"
        assert result.headers["x-frame-options"] == "DENY"

    async def test_cache_headers_added(self):
        mw = _middleware()
        req = _make_request("/api/items", "GET")
        resp = Response()
        resp.status_code = 200
        call_next = AsyncMock(return_value=resp)
        result = await mw.dispatch(req, call_next)
        assert "cache-control" in result.headers


# ---------------------------------------------------------------------------
# ResponseSecurityHandler.sanitize_response_headers
# ---------------------------------------------------------------------------


class TestResponseSecurityHandler:
    def test_sensitive_headers_removed(self):
        from middleware.response_security import ResponseSecurityHandler

        handler = ResponseSecurityHandler()
        resp = Response()
        resp.headers["Server"] = "uvicorn"
        resp.headers["X-Powered-By"] = "Python"
        handler.sanitize_response_headers(resp)
        assert "Server" not in resp.headers
        assert "X-Powered-By" not in resp.headers


# ---------------------------------------------------------------------------
# RequestSecurityChecker standalone tests
# ---------------------------------------------------------------------------


class TestRequestSecurityChecker:
    def test_accept_header_json_accepted(self):
        from middleware.request_security import RequestSecurityChecker

        checker = RequestSecurityChecker()
        req = _make_request(headers=[(b"accept", b"application/json")])
        assert checker.validate_accept_header(req) is True

    def test_accept_header_wildcard_accepted(self):
        from middleware.request_security import RequestSecurityChecker

        checker = RequestSecurityChecker()
        req = _make_request(headers=[(b"accept", b"*/*")])
        assert checker.validate_accept_header(req) is True

    def test_accept_header_image_rejected(self):
        from middleware.request_security import RequestSecurityChecker

        checker = RequestSecurityChecker()
        req = _make_request(headers=[(b"accept", b"image/png")])
        assert checker.validate_accept_header(req) is False

    @pytest.mark.parametrize(
        "ct,expected",
        [
            ("application/json", True),
            ("text/plain", True),
            ("multipart/form-data; boundary=abc", True),
            ("application/xml", False),
        ],
    )
    def test_is_supported_media_type(self, ct, expected):
        from middleware.request_security import RequestSecurityChecker

        checker = RequestSecurityChecker()
        assert checker.is_supported_media_type(ct) is expected


# ---------------------------------------------------------------------------
# InputValidator standalone tests
# ---------------------------------------------------------------------------


class TestInputValidator:
    def test_safe_parameter_key(self):
        from middleware.input_validator import InputValidator

        iv = InputValidator()
        assert iv.is_safe_parameter_key("page") is True
        assert iv.is_safe_parameter_key("_hidden") is True
        assert iv.is_safe_parameter_key("1bad") is False
        assert iv.is_safe_parameter_key("") is False

    def test_is_safe_string_blocks_xss(self):
        from middleware.input_validator import InputValidator

        iv = InputValidator()
        assert iv.is_safe_string("hello world") is True
        assert iv.is_safe_string("<script>alert(1)</script>") is False

    def test_validate_url_path(self):
        from middleware.input_validator import InputValidator

        iv = InputValidator()
        ok, err = iv.validate_url_path("/api/items")
        assert ok is True
        ok, err = iv.validate_url_path("/api/../etc/passwd")
        assert ok is False
        assert "traversal" in err.lower()
