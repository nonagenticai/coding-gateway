"""Tests for cross-cutting security components.

Tests are organized by component (SecurityUtils, RequestSecurityChecker,
ResponseSecurityHandler, ValidationMiddleware, exception handlers) with
each test verifying a single behavior.
"""

from __future__ import annotations

import importlib
import json
import sys
import time as _time
import uuid as _uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from coding_client.exceptions import ValidationException
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from starlette.responses import JSONResponse as _JSONResponse
from starlette.responses import Response as StarletteResponse

from middleware.request_security import RequestSecurityChecker
from middleware.response_security import ResponseSecurityHandler
from middleware.security_utils import SecurityUtils
from middleware.validation import SecurityValidationException, ValidationMiddleware


def register_exception_handlers(app, **kwargs):
    """Inline KAG-standard exception handlers for testing (conftest stubs the real one)."""

    def _rid(request):
        return getattr(request.state, "request_id", None) or str(_uuid.uuid4())

    def _ts():
        return _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())

    def _err(sc, *, code, message, request_id, details=None, headers=None):
        body = {"code": code, "message": message, "timestamp": _ts(), "request_id": request_id}
        if details is not None:
            body["details"] = details
        return _JSONResponse(status_code=sc, content={"error": body}, headers=headers)

    @app.exception_handler(Exception)
    async def _global(request, exc):
        rid = _rid(request)
        if isinstance(exc, HTTPException):
            if isinstance(exc.detail, dict):
                msg = exc.detail.get("message", str(exc.detail))
            else:
                msg = str(exc.detail)
            return _err(
                exc.status_code,
                code=f"HTTP_{exc.status_code}",
                message=msg,
                request_id=rid,
                details=exc.detail.get("details") if isinstance(exc.detail, dict) else None,
            )
        return _err(
            500,
            code="INTERNAL_SERVER_ERROR",
            message="An unexpected error occurred",
            request_id=rid,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request, exc):
        rid = _rid(request)
        errors = [
            {
                "field": ".".join(str(part) for part in e.get("loc", [])),
                "message": e.get("msg", ""),
                "type": e.get("type", ""),
                "input": e.get("input"),
            }
            for e in exc.errors()
        ]
        return _err(
            422,
            code="HTTP_422",
            message="Request validation failed",
            request_id=rid,
            details=errors,
        )


def _request(
    path: str = "/api/items",
    method: str = "GET",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers or [],
        "query_string": b"",
        "client": ("127.0.0.1", 1234),
        "scheme": "http",
        "server": ("testserver", 80),
    }
    return Request(scope)


# ---------------------------------------------------------------------------
# main module import
# ---------------------------------------------------------------------------


def test_main_creates_app_on_import():
    module_name = "main"
    original = sys.modules.pop(module_name, None)
    try:
        with patch("app_factory.create_app", return_value="sentinel-app") as create_app:
            imported = importlib.import_module(module_name)

        assert imported.app == "sentinel-app"
        create_app.assert_called_once_with()
    finally:
        sys.modules.pop(module_name, None)
        if original is not None:
            sys.modules[module_name] = original


# ---------------------------------------------------------------------------
# SecurityUtils — error response construction
# ---------------------------------------------------------------------------


class TestSecurityUtilsErrorResponse:
    def test_create_error_response_status_code(self):
        resp = SecurityUtils.create_error_response(403, "blocked")
        assert resp.status_code == 403

    def test_create_error_response_body_contains_error_message(self):
        resp = SecurityUtils.create_error_response(403, "blocked")
        body = json.loads(resp.body)
        assert body["error"] == "blocked"
        assert body["error_code"] == "SECURITY_ERROR"
        assert body["status_code"] == 403

    def test_security_error_headers_include_deny_frame(self):
        headers = SecurityUtils.get_security_error_headers()
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Cache-Control"] == "no-store"


# ---------------------------------------------------------------------------
# SecurityUtils — data masking and sanitization
# ---------------------------------------------------------------------------


class TestSecurityUtilsDataMasking:
    def test_mask_long_string_shows_prefix(self):
        result = SecurityUtils.mask_sensitive_data("secret-token", visible_chars=3)
        assert result == "sec*********"
        assert result.startswith("sec")
        assert "*" in result

    def test_mask_short_string_fully_masked(self):
        result = SecurityUtils.mask_sensitive_data("abc", visible_chars=4)
        assert result == "***"
        assert "a" not in result

    def test_sanitize_log_data_replaces_newlines_and_null(self):
        result = SecurityUtils.sanitize_log_data("line1\nline2\r\x00end")
        assert "\\n" in result
        assert "\\r" in result
        assert "\\x00" in result

    def test_sanitize_log_data_truncates_long_input(self):
        result = SecurityUtils.sanitize_log_data("x" * 300, max_length=5)
        assert result.endswith("...[truncated]")
        # Visible portion is at most max_length chars before the truncation marker
        assert len(result) == 5 + len("...[truncated]")


# ---------------------------------------------------------------------------
# SecurityUtils — character description
# ---------------------------------------------------------------------------


class TestSecurityUtilsCharDescription:
    def test_del_character(self):
        assert SecurityUtils.get_character_description("\x7f") == "DEL"

    def test_null_character(self):
        assert SecurityUtils.get_character_description("\x00") == "NULL"


# ---------------------------------------------------------------------------
# SecurityUtils — request inspection
# ---------------------------------------------------------------------------


class TestSecurityUtilsRequestInspection:
    def test_validate_request_timing_returns_true(self):
        req = _request(headers=[(b"content-length", b"12")])
        assert SecurityUtils.validate_request_timing(req) is True

    def test_correlation_id_format(self):
        cid = SecurityUtils.generate_correlation_id()
        assert cid.startswith("corr_")
        assert len(cid) == 17

    def test_safe_filename_accepts_valid(self):
        assert SecurityUtils.is_safe_filename("safe.txt") is True

    def test_safe_filename_rejects_traversal(self):
        assert SecurityUtils.is_safe_filename("../unsafe.txt") is False

    def test_validate_content_length_within_limit(self):
        req = _request(headers=[(b"content-length", b"12")])
        assert SecurityUtils.validate_content_length(req, max_bytes=20) is True

    def test_validate_content_length_non_numeric_rejected(self):
        req = _request(headers=[(b"content-length", b"bad")])
        assert SecurityUtils.validate_content_length(req) is False

    def test_extract_request_info_client_ip(self):
        req = _request(
            headers=[
                (b"user-agent", b"pytest"),
                (b"content-type", b"application/json"),
            ]
        )
        info = SecurityUtils.extract_request_info(req)
        assert info["client_ip"] == "127.0.0.1"
        assert info["method"] == "GET"
        assert info["path"] == "/api/items"
        assert info["user_agent"] == "pytest"

    def test_audit_log_entry_includes_action_and_details(self):
        req = _request(headers=[(b"user-agent", b"pytest")])
        entry = SecurityUtils.create_audit_log_entry("access", req, details={"ok": True})
        assert entry["action"] == "access"
        assert entry["details"] == {"ok": True}
        assert "timestamp" in entry
        assert entry["correlation_id"].startswith("corr_")


# ---------------------------------------------------------------------------
# RequestSecurityChecker — individual validations
# ---------------------------------------------------------------------------


class TestRequestSecurityCheckerPermissive:
    def test_code_graphs_endpoint_is_permissive(self):
        checker = RequestSecurityChecker()
        assert checker.is_permissive_endpoint("/api/code-graphs/build") is True

    def test_analysis_endpoint_is_permissive(self):
        checker = RequestSecurityChecker()
        assert checker.is_permissive_endpoint("/api/analysis/run") is True


class TestRequestSecurityCheckerMethod:
    def test_get_allowed(self):
        checker = RequestSecurityChecker()
        assert checker.validate_http_method(_request("/x", "GET")) is True

    def test_trace_blocked(self):
        checker = RequestSecurityChecker()
        assert checker.validate_http_method(_request("/api/health", "TRACE")) is False


class TestRequestSecurityCheckerSize:
    def test_permissive_endpoint_allows_large_body(self):
        checker = RequestSecurityChecker()
        req = _request("/api/analysis/run", "POST", [(b"content-length", b"52428800")])
        assert checker.validate_request_size(req) is True

    def test_standard_endpoint_rejects_over_10mb(self):
        checker = RequestSecurityChecker()
        req = _request(headers=[(b"content-length", b"10485761")])
        assert checker.validate_request_size(req) is False

    def test_non_numeric_content_length_rejected(self):
        checker = RequestSecurityChecker()
        req = _request(headers=[(b"content-length", b"bad")])
        assert checker.validate_request_size(req) is False


class TestRequestSecurityCheckerHeaders:
    def test_clean_headers_accepted(self):
        checker = RequestSecurityChecker()
        assert checker.validate_custom_headers(_request(headers=[(b"x-test", b"ok")])) is True

    def test_null_byte_in_header_rejected(self):
        checker = RequestSecurityChecker()
        assert checker.validate_custom_headers(_request(headers=[(b"x-test", b"bad\x00")])) is False

    def test_json_accept_header_accepted(self):
        checker = RequestSecurityChecker()
        req = _request(headers=[(b"accept", b"application/json")])
        assert checker.validate_accept_header(req) is True

    def test_image_accept_header_rejected(self):
        checker = RequestSecurityChecker()
        req = _request(headers=[(b"accept", b"image/png")])
        assert checker.validate_accept_header(req) is False

    def test_json_with_charset_is_supported_media_type(self):
        checker = RequestSecurityChecker()
        assert checker.is_supported_media_type("application/json; charset=utf-8") is True


class TestRequestSecurityCheckerBody:
    async def test_successful_body_read(self):
        checker = RequestSecurityChecker()
        request = AsyncMock()
        request.body.return_value = b"payload"
        result = await checker.get_request_body(request)
        assert result == b"payload"

    async def test_body_read_failure_returns_none(self):
        checker = RequestSecurityChecker()
        request = AsyncMock()
        request.body.side_effect = RuntimeError("read failed")
        result = await checker.get_request_body(request)
        assert result is None


# ---------------------------------------------------------------------------
# ResponseSecurityHandler — header injection and sanitization
# ---------------------------------------------------------------------------


class TestResponseSecurityHandlerHeaders:
    def test_csp_header_added(self):
        handler = ResponseSecurityHandler()
        response = Response()
        handler.add_security_headers(response)
        assert "Content-Security-Policy" in response.headers
        assert "default-src" in response.headers["Content-Security-Policy"]

    def test_cors_allow_origin_added(self):
        handler = ResponseSecurityHandler()
        response = Response()
        handler.add_cors_headers(response)
        assert response.headers["Access-Control-Allow-Origin"] == "*"

    def test_docs_path_gets_public_cache(self):
        handler = ResponseSecurityHandler()
        response = Response()
        handler.add_cache_headers(response, "/docs")
        assert response.headers["Cache-Control"] == "public, max-age=3600"

    def test_api_response_headers_set_content_type(self):
        handler = ResponseSecurityHandler()
        response = Response()
        handler.add_api_response_headers(response)
        assert response.headers["Content-Type"] == "application/json; charset=utf-8"

    def test_server_header_sanitized(self):
        handler = ResponseSecurityHandler()
        response = Response()
        response.headers["Server"] = "uvicorn"
        response.headers["X-Powered-By"] = "python"
        handler.sanitize_response_headers(response)
        assert "Server" not in response.headers
        assert "X-Powered-By" not in response.headers

    def test_full_pipeline_applies_all_headers(self):
        """Verify that running all handlers in sequence produces correct state."""
        handler = ResponseSecurityHandler()
        response = Response()
        response.headers["Server"] = "uvicorn"
        response.headers["X-Powered-By"] = "python"

        handler.add_security_headers(response)
        handler.add_cors_headers(response)
        handler.add_cache_headers(response, "/docs")
        handler.add_api_response_headers(response)
        handler.sanitize_response_headers(response)

        # Security headers present
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        # CORS present
        assert response.headers["Access-Control-Allow-Origin"] == "*"
        # Cache set for docs
        assert response.headers["Cache-Control"] == "public, max-age=3600"
        # Content type set
        assert response.headers["Content-Type"] == "application/json; charset=utf-8"
        # Sensitive headers removed
        assert "Server" not in response.headers
        assert "X-Powered-By" not in response.headers


# ---------------------------------------------------------------------------
# ValidationMiddleware — preprocessing and error responses
# ---------------------------------------------------------------------------


class TestValidationMiddlewarePreprocessing:
    def test_double_slashes_collapsed(self):
        mw = ValidationMiddleware(app=MagicMock())
        assert mw._preprocess_request("//api//items/%7E/") == "/api/items/~"

    def test_null_byte_in_url_raises_security_exception(self):
        mw = ValidationMiddleware(app=MagicMock())

        class _FakeRequest:
            url = "http://test/bad\x00path"

        with pytest.raises(SecurityValidationException, match="invalid control characters"):
            mw._validate_request_security(_FakeRequest())

    def test_validation_error_response_contains_error_code(self):
        exc = SecurityValidationException(detail="blocked", error_code="BAD")
        resp = ValidationMiddleware._create_validation_error_response(exc)
        body = json.loads(resp.body)
        assert resp.status_code == 400
        assert body["error"] == "blocked"
        assert body["error_code"] == "BAD"

    def test_http_error_response_preserves_status(self):
        exc = HTTPException(status_code=409, detail="conflict")
        resp = ValidationMiddleware._create_http_error_response(exc)
        body = json.loads(resp.body)
        assert resp.status_code == 409
        assert body["error"] == "conflict"

    def test_system_error_response_returns_500(self):
        exc = RuntimeError("boom")
        resp = ValidationMiddleware._create_system_error_response(exc)
        body = json.loads(resp.body)
        assert resp.status_code == 500
        assert body["error_code"] == "INTERNAL_ERROR"
        assert "request_id" in body


class TestValidationMiddlewareDispatch:
    async def test_excluded_path_skips_validation(self):
        mw = ValidationMiddleware(app=MagicMock(), exclude_paths=frozenset({"/skip"}))
        ok_response = StarletteResponse("ok", status_code=200)
        result = await mw.dispatch(_request("/skip"), AsyncMock(return_value=ok_response))
        assert result.status_code == 200

    async def test_http_exception_in_call_next_returns_correct_status(self):
        mw = ValidationMiddleware(app=MagicMock(), exclude_paths=frozenset({"/skip"}))
        result = await mw.dispatch(
            _request("/api/test"),
            AsyncMock(side_effect=HTTPException(status_code=418, detail="teapot")),
        )
        assert result.status_code == 418
        body = json.loads(result.body)
        assert body["error"] == "teapot"

    async def test_runtime_error_in_call_next_returns_500(self):
        mw = ValidationMiddleware(app=MagicMock(), exclude_paths=frozenset({"/skip"}))
        result = await mw.dispatch(
            _request("/api/test"),
            AsyncMock(side_effect=RuntimeError("boom")),
        )
        assert result.status_code == 500
        body = json.loads(result.body)
        assert body["error_code"] == "INTERNAL_ERROR"


# ---------------------------------------------------------------------------
# KAG-format exception handlers (via register_exception_handlers)
# ---------------------------------------------------------------------------


class TestKagExceptionHandlersIntegration:
    async def test_http_exception_returns_kag_format(self):
        app = FastAPI()
        register_exception_handlers(app)
        handler = app.exception_handlers[Exception]
        req = _request("/boom")
        exc = HTTPException(status_code=404, detail="Not found")
        resp = await handler(req, exc)  # ty: ignore[invalid-await]
        assert resp.status_code == 404
        body = json.loads(resp.body)
        assert body["error"]["code"] == "HTTP_404"
        assert body["error"]["message"] == "Not found"

    async def test_validation_exception_returns_kag_format(self):
        app = FastAPI()
        register_exception_handlers(app)
        handler = app.exception_handlers[Exception]
        req = _request("/boom")
        exc = ValidationException("bad field", error_code="CUSTOM", field="name", details={"a": 1})
        resp = await handler(req, exc)  # ty: ignore[invalid-await]
        assert resp.status_code == 400
        body = json.loads(resp.body)
        assert body["error"]["code"] == "HTTP_400"
        assert body["error"]["message"] == "bad field"

    async def test_unhandled_returns_500_kag_format(self):
        app = FastAPI()
        register_exception_handlers(app)
        handler = app.exception_handlers[Exception]
        req = _request("/boom")
        resp = await handler(req, RuntimeError("boom"))  # ty: ignore[invalid-await]
        assert resp.status_code == 500
        body = json.loads(resp.body)
        assert body["error"]["code"] == "INTERNAL_SERVER_ERROR"

    async def test_validation_error_returns_422_kag_format(self):
        app = FastAPI()
        register_exception_handlers(app)
        handler = app.exception_handlers[RequestValidationError]
        req = _request("/boom")
        exc = RequestValidationError(
            [{"type": "missing", "loc": ("body", "field"), "msg": "Field required", "input": None}]
        )
        resp = await handler(req, exc)  # ty: ignore[invalid-await]
        assert resp.status_code == 422
        body = json.loads(resp.body)
        assert body["error"]["code"] == "HTTP_422"
        assert len(body["error"]["details"]) == 1

    def test_registers_handlers(self):
        app = FastAPI()
        register_exception_handlers(app)
        assert Exception in app.exception_handlers
