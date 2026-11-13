"""Response security headers for the coding-gateway.

Adapted from the monolith's ``middleware/response_security.py``.
Adds standard security headers and cache-control policies to all
outbound responses.
"""

from __future__ import annotations

import logging

from fastapi import Response

logger = logging.getLogger(__name__)

# Standard security headers applied to every response
_DEFAULT_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), camera=(), microphone=()",
}

_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src 'self' data: https://fastapi.tiangolo.com; "
    "font-src 'self' https://cdn.jsdelivr.net; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)

# Paths whose responses may be cached briefly
_CACHEABLE_PREFIXES = ("/api/code-graphs/", "/api/metrics")
_STATIC_PREFIXES = ("/docs", "/redoc", "/openapi.json")


class ResponseSecurityHandler:
    """Add security and cache-control headers to responses."""

    def add_security_headers(self, response: Response) -> None:
        for name, value in _DEFAULT_HEADERS.items():
            response.headers.setdefault(name, value)
        response.headers.setdefault("Content-Security-Policy", _CSP)

    def add_cors_headers(self, response: Response) -> None:
        """Set permissive CORS headers (already handled by CORSMiddleware in most cases)."""
        response.headers.setdefault("Access-Control-Allow-Origin", "*")
        response.headers.setdefault(
            "Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS, PATCH"
        )
        response.headers.setdefault(
            "Access-Control-Allow-Headers", "Content-Type, Authorization, X-Tenant-Id"
        )

    def add_cache_headers(self, response: Response, path: str) -> None:
        """Set cache-control headers based on path."""
        if any(path.startswith(p) for p in _STATIC_PREFIXES):
            response.headers.setdefault("Cache-Control", "public, max-age=3600")
        elif any(path.startswith(p) for p in _CACHEABLE_PREFIXES):
            response.headers.setdefault("Cache-Control", "private, max-age=60")
        else:
            response.headers.setdefault("Cache-Control", "no-store, no-cache, must-revalidate")

    def add_api_response_headers(self, response: Response) -> None:
        """Ensure JSON content-type on API responses."""
        ct = response.headers.get("content-type", "")
        if not ct:
            response.headers["Content-Type"] = "application/json; charset=utf-8"

    def sanitize_response_headers(self, response: Response) -> None:
        """Remove potentially sensitive headers."""
        for header in ("Server", "X-Powered-By"):
            if header in response.headers:
                del response.headers[header]
