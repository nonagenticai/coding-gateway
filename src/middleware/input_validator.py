"""Input validation utilities for the coding-gateway.

Adapted from the monolith's ``middleware/input_validator.py``.
Validates URL paths, query parameters, JSON bodies and detects
XSS / path-traversal / dangerous patterns.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import unquote

logger = logging.getLogger(__name__)

# ── Patterns ──────────────────────────────────────────────────────────

# Dangerous patterns in input
_DANGEROUS_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?:\.\./|\.\.\\)", re.IGNORECASE),  # Path traversal
    re.compile(r"(?:;|\||`|&&|\$\()", re.IGNORECASE),  # Command injection
    re.compile(
        r"(?:union\s+select|drop\s+table|insert\s+into|delete\s+from)", re.IGNORECASE
    ),  # SQL injection
]

# XSS patterns
_XSS_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"<script[^>]*>", re.IGNORECASE),
    re.compile(r"javascript:", re.IGNORECASE),
    re.compile(r"on\w+\s*=", re.IGNORECASE),
    re.compile(r"<iframe", re.IGNORECASE),
    re.compile(r"<object", re.IGNORECASE),
]

# Control characters that are NOT safe (NULL, 0x01-0x08, VT, FF, 0x0E-0x1F, DEL)
_UNSAFE_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Safe control characters: TAB (0x09), LF (0x0A), CR (0x0D)
_SAFE_CONTROL_CHARS = frozenset({"\t", "\n", "\r"})

# Valid parameter key pattern
_PARAM_KEY_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.\-\[\]]*$")

# Fields that contain code (exempt from XSS checks)
_CODE_FIELDS: frozenset[str] = frozenset(
    {
        "code",
        "source_code",
        "content",
        "snippet",
        "stdout",
        "stderr",
        "output",
        "raw_output",
        "sql_query",
        "query",
        "pattern",
    }
)


class InputValidator:
    """Validates and sanitises user-supplied input."""

    # ── URL path ──────────────────────────────────────────────────

    @staticmethod
    def safely_decode_url_path(path: str) -> str:
        try:
            return unquote(path)
        except Exception:
            return path

    @staticmethod
    def safely_decode_query_param(value: str) -> str:
        try:
            return unquote(value)
        except Exception:
            return value

    @staticmethod
    def contains_only_safe_control_chars(text: str) -> bool:
        """Return True if the text has no *unsafe* control characters."""
        return not bool(_UNSAFE_CONTROL_RE.search(text))

    @staticmethod
    def is_safe_parameter_key(key: str) -> bool:
        if not key or len(key) > 128:
            return False
        return bool(_PARAM_KEY_RE.fullmatch(key))

    def validate_url_path(self, path: str) -> tuple[bool, str | None]:
        """Validate a URL path.  Returns (ok, error_message)."""
        if not path:
            return False, "Empty path"

        decoded = self.safely_decode_url_path(path)

        # Path traversal
        if ".." in decoded:
            return False, "Path traversal detected"

        # NULL bytes
        if "\x00" in decoded:
            return False, "NULL byte in path"

        # Dangerous patterns
        for pattern in _DANGEROUS_PATTERNS:
            if pattern.search(decoded):
                return False, "Dangerous pattern detected in path"

        return True, None

    def is_safe_string(self, value: str) -> bool:
        """Check whether a string is safe for general use."""
        if not self.contains_only_safe_control_chars(value):
            return False
        for pattern in _XSS_PATTERNS:
            if pattern.search(value):
                return False
        return True

    def is_safe_code_string(self, value: str) -> bool:
        """Check whether a string is safe when treated as *code* content.

        Code strings are exempt from XSS checks but still reject unsafe
        control characters.
        """
        return self.contains_only_safe_control_chars(value)

    # ── JSON body ─────────────────────────────────────────────────

    def validate_json_body(self, body: bytes) -> tuple[bool, str | None]:
        """Validate a JSON request body.  Returns (ok, error_message)."""
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return False, f"Invalid JSON: {exc}"

        if isinstance(data, dict):
            return self._validate_json_recursive(data, depth=0)
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    ok, err = self._validate_json_recursive(item, depth=0)
                    if not ok:
                        return ok, err
        return True, None

    def _validate_json_recursive(
        self,
        data: dict,
        depth: int = 0,
        max_depth: int = 20,
    ) -> tuple[bool, str | None]:
        if depth > max_depth:
            return False, "JSON nesting too deep"

        for key, value in data.items():
            # Validate key
            if not self.contains_only_safe_control_chars(key):
                return False, f"Unsafe control characters in key: {key}"

            if isinstance(value, str):
                if self._is_code_field(key):
                    if not self.is_safe_code_string(value):
                        return False, f"Unsafe control characters in code field: {key}"
                else:
                    if not self.is_safe_string(value):
                        return False, f"Unsafe content in field: {key}"

            elif isinstance(value, dict):
                ok, err = self._validate_json_recursive(value, depth + 1, max_depth)
                if not ok:
                    return ok, err

            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, str):
                        if not self.contains_only_safe_control_chars(item):
                            return False, f"Unsafe control characters in list item for key: {key}"
                    elif isinstance(item, dict):
                        ok, err = self._validate_json_recursive(item, depth + 1, max_depth)
                        if not ok:
                            return ok, err

        return True, None

    @staticmethod
    def _is_code_field(key: str) -> bool:
        return key.lower() in _CODE_FIELDS
