"""Security middleware for the coding-gateway.

Adapted from the monolith's ``middleware/security_middleware.py``.
Provides defence-in-depth validation of incoming requests (URL paths,
query parameters, headers, JSON bodies) and adds security response
headers.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse

from .input_validator import InputValidator
from .request_security import RequestSecurityChecker
from .response_security import ResponseSecurityHandler
from .security_utils import SecurityUtils

logger = logging.getLogger(__name__)


class SecurityMiddleware(BaseHTTPMiddleware):
    """Comprehensive security validation middleware."""

    def __init__(self, app, **kwargs):
        super().__init__(app, **kwargs)
        self._input_validator = InputValidator()
        self._request_checker = RequestSecurityChecker()
        self._response_handler = ResponseSecurityHandler()
        self._doc_access: dict[str, list[float]] = defaultdict(list)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path

        # Skip security checks for documentation endpoints (with rate limiting)
        if path in ("/docs", "/redoc", "/openapi.json"):
            if not self._check_doc_rate_limit(request):
                return JSONResponse(
                    status_code=429, content={"error": "Rate limit exceeded for docs"}
                )
            response = await call_next(request)
            self._response_handler.add_security_headers(response)
            return response

        # Validate HTTP method
        if not self._request_checker.validate_http_method(request):
            return SecurityUtils.create_error_response(405, "Method not allowed")

        # Validate request size
        if not self._request_checker.validate_request_size(request):
            return SecurityUtils.create_error_response(413, "Request entity too large")

        # Validate URL path
        path_ok, path_err = self._input_validator.validate_url_path(path)
        if not path_ok:
            SecurityUtils.log_security_decision("BLOCK", f"Invalid URL path: {path_err}", request)
            return SecurityUtils.create_error_response(400, "Invalid request path")

        # Validate query parameters
        for key, value in request.query_params.items():
            if not self._input_validator.is_safe_parameter_key(key):
                return SecurityUtils.create_error_response(400, "Invalid query parameter name")
            if not self._input_validator.is_safe_string(value):
                return SecurityUtils.create_error_response(400, "Invalid query parameter value")

        # Validate custom headers
        if not self._request_checker.validate_custom_headers(request):
            return SecurityUtils.create_error_response(400, "Invalid request headers")

        # Validate JSON body for POST/PUT/PATCH
        if request.method in ("POST", "PUT", "PATCH"):
            content_type = request.headers.get("content-type", "")
            if "application/json" in content_type:
                body = await self._request_checker.get_request_body(request)
                # An empty body is not a JSON document to validate: endpoints that take
                # no body (security quick/sbom/image scans) were answered 400 "Invalid
                # request body" whenever a client sent Content-Type: application/json.
                if body:
                    body_ok, body_err = self._input_validator.validate_json_body(body)
                    if not body_ok:
                        SecurityUtils.log_security_decision(
                            "BLOCK", f"Invalid JSON body: {body_err}", request
                        )
                        return SecurityUtils.create_error_response(400, "Invalid request body")

        # Process request
        response = await call_next(request)

        # Add security headers
        self._response_handler.add_security_headers(response)
        self._response_handler.add_cache_headers(response, path)

        return response

    def _check_doc_rate_limit(self, request: Request) -> bool:
        """Simple sliding-window rate limit for doc endpoints."""
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()
        window = 60.0  # 1 minute
        max_requests = 30

        # Prune old entries
        self._doc_access[client_ip] = [t for t in self._doc_access[client_ip] if now - t < window]

        if len(self._doc_access[client_ip]) >= max_requests:
            return False

        self._doc_access[client_ip].append(now)
        return True
