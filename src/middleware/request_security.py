"""Request-level security checks for the coding-gateway.

Adapted from the monolith's ``middleware/request_security.py``.
Validates HTTP methods, request size, headers, and content types.
"""

from __future__ import annotations

import logging

from fastapi import Request

logger = logging.getLogger(__name__)

# Endpoints that accept larger / less-restricted payloads
_PERMISSIVE_ENDPOINTS: frozenset[str] = frozenset(
    {
        "/api/validate",
        "/api/analysis",
        "/api/code-graphs",
    }
)

_ALLOWED_METHODS: frozenset[str] = frozenset(
    {
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "OPTIONS",
        "HEAD",
    }
)

_SUPPORTED_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "application/json",
        "text/plain",
        "multipart/form-data",
        "application/x-www-form-urlencoded",
    }
)

# 10 MB default, 50 MB for permissive
_MAX_BODY_SIZE = 10 * 1024 * 1024
_MAX_BODY_SIZE_PERMISSIVE = 50 * 1024 * 1024


class RequestSecurityChecker:
    """Stateless helpers for validating inbound request properties."""

    def is_permissive_endpoint(self, path: str) -> bool:
        return any(path.startswith(p) for p in _PERMISSIVE_ENDPOINTS)

    def validate_http_method(self, request: Request) -> bool:
        return request.method.upper() in _ALLOWED_METHODS

    def validate_request_size(self, request: Request) -> bool:
        content_length = request.headers.get("content-length")
        if content_length is None:
            return True
        try:
            size = int(content_length)
        except ValueError:
            return False
        limit = (
            _MAX_BODY_SIZE_PERMISSIVE
            if self.is_permissive_endpoint(request.url.path)
            else _MAX_BODY_SIZE
        )
        return size <= limit

    def validate_custom_headers(self, request: Request) -> bool:
        """Reject headers with NULL bytes or excessively long values."""
        for name, value in request.headers.items():
            if "\x00" in value:
                logger.warning("NULL byte in header %s", name)
                return False
            if len(value) > 8192:
                logger.warning("Header %s too long (%d bytes)", name, len(value))
                return False
        return True

    def validate_accept_header(self, request: Request) -> bool:
        accept = request.headers.get("accept", "*/*")
        return "application/json" in accept or "*/*" in accept or "text/" in accept

    def is_supported_media_type(self, content_type: str) -> bool:
        base = content_type.split(";")[0].strip().lower()
        return base in _SUPPORTED_CONTENT_TYPES

    async def get_request_body(self, request: Request) -> bytes | None:
        """Read and return the request body, or None on failure."""
        try:
            return await request.body()
        except Exception as e:
            logger.warning("Failed to read request body: %s", e)
            return None
