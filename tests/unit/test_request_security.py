"""Tests for middleware/request_security.py – gap coverage."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from middleware.request_security import (
    _ALLOWED_METHODS,
    _MAX_BODY_SIZE,
    _MAX_BODY_SIZE_PERMISSIVE,
    _PERMISSIVE_ENDPOINTS,
    _SUPPORTED_CONTENT_TYPES,
    RequestSecurityChecker,
)


def _make_request(
    method: str = "GET",
    path: str = "/api/test",
    headers: dict | None = None,
):
    req = MagicMock()
    req.method = method
    req.url.path = path
    req.headers = MagicMock()
    hdr = headers or {}
    req.headers.get = lambda k, default=None: hdr.get(k, default)
    req.headers.items = lambda: hdr.items()
    return req


@pytest.fixture
def checker():
    return RequestSecurityChecker()


# ---------------------------------------------------------------------------
# Permissive endpoints
# ---------------------------------------------------------------------------


class TestPermissiveEndpoint:
    def test_validate_is_permissive(self, checker):
        assert checker.is_permissive_endpoint("/api/validate") is True

    def test_analysis_is_permissive(self, checker):
        assert checker.is_permissive_endpoint("/api/analysis/run") is True

    def test_code_graphs_is_permissive(self, checker):
        assert checker.is_permissive_endpoint("/api/code-graphs/1") is True

    def test_non_permissive(self, checker):
        assert checker.is_permissive_endpoint("/api/hello") is False


# ---------------------------------------------------------------------------
# HTTP method validation
# ---------------------------------------------------------------------------


class TestValidateHttpMethod:
    def test_get_allowed(self, checker):
        assert checker.validate_http_method(_make_request("GET")) is True

    def test_post_allowed(self, checker):
        assert checker.validate_http_method(_make_request("POST")) is True

    def test_options_allowed(self, checker):
        assert checker.validate_http_method(_make_request("OPTIONS")) is True

    def test_trace_blocked(self, checker):
        assert checker.validate_http_method(_make_request("TRACE")) is False

    def test_connect_blocked(self, checker):
        assert checker.validate_http_method(_make_request("CONNECT")) is False

    def test_case_insensitive(self, checker):
        """Method is upper-cased before comparison."""
        req = _make_request("get")
        req.method = "get"
        # The checker calls .upper()
        assert checker.validate_http_method(req) is True


# ---------------------------------------------------------------------------
# Request size validation
# ---------------------------------------------------------------------------


class TestValidateRequestSize:
    def test_no_content_length_passes(self, checker):
        req = _make_request(headers={})
        assert checker.validate_request_size(req) is True

    def test_small_passes(self, checker):
        req = _make_request(headers={"content-length": "1000"})
        assert checker.validate_request_size(req) is True

    def test_exceeds_normal_limit(self, checker):
        req = _make_request(headers={"content-length": str(_MAX_BODY_SIZE + 1)})
        assert checker.validate_request_size(req) is False

    def test_permissive_endpoint_higher_limit(self, checker):
        req = _make_request(
            path="/api/validate",
            headers={"content-length": str(_MAX_BODY_SIZE + 1)},
        )
        assert checker.validate_request_size(req) is True

    def test_permissive_exceeds(self, checker):
        req = _make_request(
            path="/api/validate",
            headers={"content-length": str(_MAX_BODY_SIZE_PERMISSIVE + 1)},
        )
        assert checker.validate_request_size(req) is False

    def test_invalid_content_length(self, checker):
        req = _make_request(headers={"content-length": "abc"})
        assert checker.validate_request_size(req) is False


# ---------------------------------------------------------------------------
# Custom header validation
# ---------------------------------------------------------------------------


class TestValidateCustomHeaders:
    def test_clean_headers_pass(self, checker):
        req = _make_request(headers={"x-custom": "value", "accept": "application/json"})
        assert checker.validate_custom_headers(req) is True

    def test_null_byte_in_header_blocked(self, checker):
        req = _make_request(headers={"x-custom": "val\x00ue"})
        assert checker.validate_custom_headers(req) is False

    def test_oversized_header_blocked(self, checker):
        req = _make_request(headers={"x-custom": "x" * 8193})
        assert checker.validate_custom_headers(req) is False

    def test_exact_limit_passes(self, checker):
        req = _make_request(headers={"x-custom": "x" * 8192})
        assert checker.validate_custom_headers(req) is True


# ---------------------------------------------------------------------------
# Accept header validation
# ---------------------------------------------------------------------------


class TestValidateAcceptHeader:
    def test_json_accept(self, checker):
        req = _make_request(headers={"accept": "application/json"})
        assert checker.validate_accept_header(req) is True

    def test_wildcard_accept(self, checker):
        req = _make_request(headers={"accept": "*/*"})
        assert checker.validate_accept_header(req) is True

    def test_text_accept(self, checker):
        req = _make_request(headers={"accept": "text/html"})
        assert checker.validate_accept_header(req) is True

    def test_default_accept(self, checker):
        req = _make_request(headers={})
        assert checker.validate_accept_header(req) is True

    def test_xml_only_rejected(self, checker):
        req = _make_request(headers={"accept": "application/xml"})
        assert checker.validate_accept_header(req) is False


# ---------------------------------------------------------------------------
# Supported media types
# ---------------------------------------------------------------------------


class TestSupportedMediaType:
    def test_json(self, checker):
        assert checker.is_supported_media_type("application/json") is True

    def test_json_with_charset(self, checker):
        assert checker.is_supported_media_type("application/json; charset=utf-8") is True

    def test_form_data(self, checker):
        assert checker.is_supported_media_type("multipart/form-data") is True

    def test_urlencoded(self, checker):
        assert checker.is_supported_media_type("application/x-www-form-urlencoded") is True

    def test_xml_unsupported(self, checker):
        assert checker.is_supported_media_type("application/xml") is False


# ---------------------------------------------------------------------------
# get_request_body
# ---------------------------------------------------------------------------


class TestGetRequestBody:
    async def test_successful_body_read(self, checker):
        from unittest.mock import AsyncMock

        req = MagicMock()
        req.body = AsyncMock(return_value=b'{"key": "value"}')
        result = await checker.get_request_body(req)
        assert result == b'{"key": "value"}'

    async def test_body_read_failure(self, checker):
        from unittest.mock import AsyncMock

        req = MagicMock()
        req.body = AsyncMock(side_effect=Exception("read error"))
        result = await checker.get_request_body(req)
        assert result is None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_allowed_methods(self):
        assert "GET" in _ALLOWED_METHODS
        assert "POST" in _ALLOWED_METHODS
        assert "DELETE" in _ALLOWED_METHODS
        assert "TRACE" not in _ALLOWED_METHODS

    def test_permissive_endpoints(self):
        assert "/api/validate" in _PERMISSIVE_ENDPOINTS
        assert "/api/analysis" in _PERMISSIVE_ENDPOINTS

    def test_supported_content_types(self):
        assert "application/json" in _SUPPORTED_CONTENT_TYPES
        assert "text/plain" in _SUPPORTED_CONTENT_TYPES
