"""Tests for middleware.input_validator — InputValidator."""

from __future__ import annotations

import json

import pytest

from middleware.input_validator import InputValidator


@pytest.fixture
def validator() -> InputValidator:
    return InputValidator()


# ── Clean input passes ──────────────────────────────────────────────


def test_clean_url_path_passes(validator):
    ok, err = validator.validate_url_path("/api/items")
    assert ok is True
    assert err is None


def test_clean_string_passes(validator):
    assert validator.is_safe_string("hello world") is True


def test_clean_json_body_passes(validator):
    body = json.dumps({"name": "test", "value": 42}).encode()
    ok, err = validator.validate_json_body(body)
    assert ok is True
    assert err is None


def test_clean_parameter_key(validator):
    assert validator.is_safe_parameter_key("page") is True
    assert validator.is_safe_parameter_key("filter_name") is True
    assert validator.is_safe_parameter_key("items[0]") is True
    assert validator.is_safe_parameter_key("some.nested") is True


# ── Control chars in URL rejected ───────────────────────────────────


def test_null_byte_in_path_rejected(validator):
    ok, err = validator.validate_url_path("/api/items\x00")
    assert ok is False
    assert "NULL byte" in err


def test_control_chars_in_string_rejected(validator):
    assert validator.is_safe_string("hello\x01world") is False


def test_control_chars_detection(validator):
    assert validator.contains_only_safe_control_chars("hello") is True
    assert validator.contains_only_safe_control_chars("line\ttab") is True
    assert validator.contains_only_safe_control_chars("line\nnewline") is True
    assert validator.contains_only_safe_control_chars("line\rreturn") is True
    # Unsafe control chars
    assert validator.contains_only_safe_control_chars("\x00null") is False
    assert validator.contains_only_safe_control_chars("\x01soh") is False
    assert validator.contains_only_safe_control_chars("\x7fDEL") is False


# ── Invalid body ────────────────────────────────────────────────────


def test_xss_in_body_field_rejected(validator):
    body = json.dumps({"name": "<script>alert(1)</script>"}).encode()
    ok, err = validator.validate_json_body(body)
    assert ok is False
    assert "Unsafe content" in err


def test_control_chars_in_body_key_rejected(validator):
    body = json.dumps({"\x00key": "value"}).encode()
    ok, err = validator.validate_json_body(body)
    assert ok is False
    assert "Unsafe control characters in key" in err


def test_control_chars_in_body_list_item_rejected(validator):
    body = json.dumps({"items": ["good", "\x01bad"]}).encode()
    ok, err = validator.validate_json_body(body)
    assert ok is False
    assert "Unsafe control characters in list item" in err


def test_nested_dict_with_xss_rejected(validator):
    body = json.dumps({"outer": {"inner": "<script>alert(1)</script>"}}).encode()
    ok, err = validator.validate_json_body(body)
    assert ok is False


def test_deeply_nested_body_rejected(validator):
    """JSON nesting deeper than max_depth is rejected."""
    data = {"level": {}}
    current = data["level"]
    for _ in range(25):
        current["nested"] = {}
        current = current["nested"]
    body = json.dumps(data).encode()
    ok, err = validator.validate_json_body(body)
    assert ok is False
    assert "nesting too deep" in err


def test_code_field_exempted_from_xss(validator):
    """Fields in _CODE_FIELDS are exempt from XSS checks."""
    body = json.dumps({"code": "<script>alert(1)</script>"}).encode()
    ok, err = validator.validate_json_body(body)
    assert ok is True
    assert err is None


def test_code_field_still_rejects_control_chars(validator):
    """Code fields still reject unsafe control characters."""
    body = json.dumps({"code": "print('hello')\x01"}).encode()
    ok, err = validator.validate_json_body(body)
    assert ok is False
    assert "Unsafe control characters in code field" in err


def test_list_body_passes(validator):
    """Top-level JSON array with valid dicts passes."""
    body = json.dumps([{"name": "a"}, {"name": "b"}]).encode()
    ok, err = validator.validate_json_body(body)
    assert ok is True


def test_list_body_with_bad_item_rejected(validator):
    body = json.dumps([{"name": "<script>bad</script>"}]).encode()
    ok, err = validator.validate_json_body(body)
    assert ok is False


# ── Malformed JSON ──────────────────────────────────────────────────


def test_malformed_json_rejected(validator):
    ok, err = validator.validate_json_body(b"{not json")
    assert ok is False
    assert "Invalid JSON" in err


def test_unicode_decode_error_rejected(validator):
    ok, err = validator.validate_json_body(b"\xff\xfe")
    assert ok is False
    assert "Invalid JSON" in err


# ── Truncation (long param key) ────────────────────────────────────


def test_oversized_parameter_key_rejected(validator):
    assert validator.is_safe_parameter_key("a" * 129) is False


def test_empty_parameter_key_rejected(validator):
    assert validator.is_safe_parameter_key("") is False


def test_parameter_key_starting_with_digit_rejected(validator):
    assert validator.is_safe_parameter_key("1abc") is False


# ── Excluded routes (N/A for InputValidator, but safe_decode) ───────


def test_safely_decode_url_path(validator):
    assert validator.safely_decode_url_path("/api/%7Eitems") == "/api/~items"


def test_safely_decode_url_path_fallback(validator):
    # Even a plain string returns unchanged
    assert validator.safely_decode_url_path("/plain") == "/plain"


def test_safely_decode_query_param(validator):
    assert validator.safely_decode_query_param("hello%20world") == "hello world"


# ── Exception path ──────────────────────────────────────────────────


def test_path_traversal_detected(validator):
    ok, err = validator.validate_url_path("/api/../etc/passwd")
    assert ok is False
    assert "Path traversal" in err


def test_command_injection_detected(validator):
    ok, err = validator.validate_url_path("/api/items;ls")
    assert ok is False
    assert "Dangerous pattern" in err


def test_sql_injection_in_path_detected(validator):
    ok, err = validator.validate_url_path("/api/items/union select")
    assert ok is False
    assert "Dangerous pattern" in err


def test_empty_path_rejected(validator):
    ok, err = validator.validate_url_path("")
    assert ok is False
    assert "Empty path" in err


# ── XSS patterns ────────────────────────────────────────────────────


def test_javascript_protocol_rejected(validator):
    assert validator.is_safe_string("javascript:alert(1)") is False


def test_onload_event_rejected(validator):
    assert validator.is_safe_string('onload="alert(1)"') is False


def test_iframe_rejected(validator):
    assert validator.is_safe_string("<iframe src='evil'>") is False


def test_object_tag_rejected(validator):
    assert validator.is_safe_string("<object data='evil'>") is False


# ── Code field detection ────────────────────────────────────────────


def test_is_code_field_case_insensitive(validator):
    assert validator._is_code_field("Code") is True
    assert validator._is_code_field("SOURCE_CODE") is True
    assert validator._is_code_field("content") is True
    assert validator._is_code_field("notacodefield") is False


# ── is_safe_code_string ─────────────────────────────────────────────


def test_safe_code_string_allows_xss_patterns(validator):
    # Code strings skip XSS checks
    assert validator.is_safe_code_string("<script>alert(1)</script>") is True


def test_safe_code_string_rejects_control_chars(validator):
    assert validator.is_safe_code_string("code\x00here") is False


# ── JSON body with list items containing nested dicts ───────────────


def test_list_with_nested_dict_validated(validator):
    body = json.dumps({"items": [{"name": "ok"}, {"bad": "<script>x</script>"}]}).encode()
    ok, err = validator.validate_json_body(body)
    assert ok is False


# ── Top-level list with scalars passes ──────────────────────────────


def test_scalar_list_body_passes(validator):
    body = json.dumps([1, 2, 3]).encode()
    ok, err = validator.validate_json_body(body)
    assert ok is True
