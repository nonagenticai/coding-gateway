"""Tests for middleware/security_utils.py – gap coverage."""

from __future__ import annotations

from unittest.mock import MagicMock

from middleware.security_utils import SecurityUtils


def _make_request(
    method: str = "GET",
    path: str = "/api/test",
    client_host: str = "10.0.0.1",
    headers: dict | None = None,
):
    req = MagicMock()
    req.method = method
    req.url.path = path
    req.client.host = client_host
    req.headers = headers or {}
    return req


# ---------------------------------------------------------------------------
# create_error_response
# ---------------------------------------------------------------------------


class TestCreateErrorResponse:
    def test_basic_error(self):
        resp = SecurityUtils.create_error_response(403, "Forbidden")
        assert resp.status_code == 403
        import json

        body = json.loads(resp.body)
        assert body["error"] == "Forbidden"
        assert body["error_code"] == "SECURITY_ERROR"
        assert body["status_code"] == 403


# ---------------------------------------------------------------------------
# get_security_error_headers
# ---------------------------------------------------------------------------


class TestSecurityErrorHeaders:
    def test_contains_expected_headers(self):
        headers = SecurityUtils.get_security_error_headers()
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["Cache-Control"] == "no-store"


# ---------------------------------------------------------------------------
# log_security_decision
# ---------------------------------------------------------------------------


class TestLogSecurityDecision:
    def test_block_logs_warning(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="middleware.security_utils"):
            SecurityUtils.log_security_decision("BLOCK", "bad path", _make_request())
        assert any("security_decision" in r.message for r in caplog.records)

    def test_allow_logs_debug(self, caplog):
        import logging

        with caplog.at_level(logging.DEBUG, logger="middleware.security_utils"):
            SecurityUtils.log_security_decision("ALLOW", "ok", _make_request())

    def test_no_request(self):
        # Should not raise when request is None
        SecurityUtils.log_security_decision("BLOCK", "reason")


# ---------------------------------------------------------------------------
# get_character_description
# ---------------------------------------------------------------------------


class TestCharacterDescription:
    def test_null(self):
        assert SecurityUtils.get_character_description("\x00") == "NULL"

    def test_bel(self):
        assert SecurityUtils.get_character_description("\x07") == "BEL"

    def test_del(self):
        assert SecurityUtils.get_character_description("\x7f") == "DEL"

    def test_unknown(self):
        desc = SecurityUtils.get_character_description("\x0e")
        assert desc == "0x0E"


# ---------------------------------------------------------------------------
# mask_sensitive_data
# ---------------------------------------------------------------------------


class TestMaskSensitiveData:
    def test_long_string(self):
        result = SecurityUtils.mask_sensitive_data("mysecretkey", visible_chars=4)
        assert result == "myse*******"

    def test_short_string(self):
        result = SecurityUtils.mask_sensitive_data("abc", visible_chars=4)
        assert result == "***"

    def test_exact_length(self):
        result = SecurityUtils.mask_sensitive_data("abcd", visible_chars=4)
        assert result == "****"


# ---------------------------------------------------------------------------
# sanitize_log_data
# ---------------------------------------------------------------------------


class TestSanitizeLogData:
    def test_replaces_newlines(self):
        result = SecurityUtils.sanitize_log_data("line1\nline2\rend")
        assert "\\n" in result
        assert "\\r" in result

    def test_replaces_null(self):
        result = SecurityUtils.sanitize_log_data("hello\x00world")
        assert "\\x00" in result

    def test_truncates_long_data(self):
        result = SecurityUtils.sanitize_log_data("x" * 300, max_length=200)
        assert result.endswith("...[truncated]")
        assert len(result) == 200 + len("...[truncated]")


# ---------------------------------------------------------------------------
# validate_request_timing
# ---------------------------------------------------------------------------


class TestValidateRequestTiming:
    def test_always_true(self):
        assert SecurityUtils.validate_request_timing(_make_request()) is True


# ---------------------------------------------------------------------------
# generate_correlation_id
# ---------------------------------------------------------------------------


class TestGenerateCorrelationId:
    def test_format(self):
        cid = SecurityUtils.generate_correlation_id()
        assert cid.startswith("corr_")
        assert len(cid) == 17  # corr_ + 12 hex


# ---------------------------------------------------------------------------
# is_safe_filename
# ---------------------------------------------------------------------------


class TestIsSafeFilename:
    def test_valid_filename(self):
        assert SecurityUtils.is_safe_filename("test.py") is True

    def test_filename_with_dash(self):
        assert SecurityUtils.is_safe_filename("my-file.txt") is True

    def test_empty_filename(self):
        assert SecurityUtils.is_safe_filename("") is False

    def test_too_long(self):
        assert SecurityUtils.is_safe_filename("a" * 256) is False

    def test_path_traversal(self):
        assert SecurityUtils.is_safe_filename("../etc/passwd") is False

    def test_forward_slash(self):
        assert SecurityUtils.is_safe_filename("path/file") is False

    def test_backslash(self):
        assert SecurityUtils.is_safe_filename("path\\file") is False

    def test_special_chars_rejected(self):
        assert SecurityUtils.is_safe_filename("file name.txt") is False


# ---------------------------------------------------------------------------
# validate_content_length
# ---------------------------------------------------------------------------


class TestValidateContentLength:
    def test_no_header_passes(self):
        req = _make_request(headers={})
        assert SecurityUtils.validate_content_length(req) is True

    def test_within_limit(self):
        req = _make_request(headers={"content-length": "1000"})
        assert SecurityUtils.validate_content_length(req) is True

    def test_exceeds_limit(self):
        req = _make_request(headers={"content-length": str(11 * 1024 * 1024)})
        assert SecurityUtils.validate_content_length(req) is False

    def test_custom_limit(self):
        req = _make_request(headers={"content-length": "500"})
        assert SecurityUtils.validate_content_length(req, max_bytes=100) is False

    def test_invalid_value(self):
        req = _make_request(headers={"content-length": "not-a-number"})
        assert SecurityUtils.validate_content_length(req) is False


# ---------------------------------------------------------------------------
# extract_request_info
# ---------------------------------------------------------------------------


class TestExtractRequestInfo:
    def test_extracts_fields(self):
        req = _make_request(
            method="POST",
            path="/api/data",
            headers={"user-agent": "TestBot", "content-type": "application/json"},
        )
        info = SecurityUtils.extract_request_info(req)
        assert info["method"] == "POST"
        assert info["path"] == "/api/data"
        assert info["client_ip"] == "10.0.0.1"
        assert info["user_agent"] == "TestBot"
        assert info["content_type"] == "application/json"

    def test_no_client(self):
        req = _make_request()
        req.client = None
        info = SecurityUtils.extract_request_info(req)
        assert info["client_ip"] == "unknown"


# ---------------------------------------------------------------------------
# create_audit_log_entry
# ---------------------------------------------------------------------------


class TestCreateAuditLogEntry:
    def test_entry_fields(self):
        req = _make_request(method="DELETE", path="/api/graph/1")
        entry = SecurityUtils.create_audit_log_entry("DELETE_GRAPH", req, user_id="u123")
        assert entry["action"] == "DELETE_GRAPH"
        assert entry["user_id"] == "u123"
        assert entry["method"] == "DELETE"
        assert entry["path"] == "/api/graph/1"
        assert "timestamp" in entry
        assert entry["correlation_id"].startswith("corr_")

    def test_anonymous_user(self):
        entry = SecurityUtils.create_audit_log_entry("VIEW", _make_request())
        assert entry["user_id"] == "anonymous"

    def test_with_details(self):
        entry = SecurityUtils.create_audit_log_entry(
            "ACTION", _make_request(), details={"key": "val"}
        )
        assert entry["details"] == {"key": "val"}
