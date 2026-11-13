"""Security utility functions for the coding-gateway.

Adapted from the monolith's ``middleware/security_utils.py``.
Provides static helpers for error responses, logging, data masking,
and request info extraction.

The monolith used ``uuid_v7``; this module uses stdlib ``uuid4`` instead.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime

from fastapi import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# Characters considered "safe" in filenames
_SAFE_FILENAME_RE = re.compile(r"^[a-zA-Z0-9._\-]+$")


class SecurityUtils:
    """Stateless security utility methods."""

    @staticmethod
    def create_error_response(status_code: int, message: str) -> JSONResponse:
        return JSONResponse(
            status_code=status_code,
            content={
                "error": message,
                "error_code": "SECURITY_ERROR",
                "status_code": status_code,
            },
        )

    @staticmethod
    def get_security_error_headers() -> dict[str, str]:
        return {
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Cache-Control": "no-store",
        }

    @staticmethod
    def log_security_decision(
        decision: str,
        reason: str,
        request: Request | None = None,
    ) -> None:
        extra: dict[str, str] = {"decision": decision, "reason": reason}
        if request:
            extra["method"] = request.method
            extra["path"] = request.url.path
            extra["client"] = request.client.host if request.client else "unknown"
        if decision == "BLOCK":
            logger.warning("security_decision", extra=extra)
        else:
            logger.debug("security_decision", extra=extra)

    @staticmethod
    def get_character_description(char: str) -> str:
        code = ord(char)
        names = {0: "NULL", 7: "BEL", 8: "BS", 11: "VT", 12: "FF", 127: "DEL"}
        return names.get(code, f"0x{code:02X}")

    @staticmethod
    def mask_sensitive_data(data: str, visible_chars: int = 4) -> str:
        if len(data) <= visible_chars:
            return "*" * len(data)
        return data[:visible_chars] + "*" * (len(data) - visible_chars)

    @staticmethod
    def sanitize_log_data(data: str, max_length: int = 200) -> str:
        sanitized = data.replace("\n", "\\n").replace("\r", "\\r").replace("\x00", "\\x00")
        if len(sanitized) > max_length:
            return sanitized[:max_length] + "...[truncated]"
        return sanitized

    @staticmethod
    def validate_request_timing(request: Request) -> bool:
        """Placeholder for request-timing validation (replay-attack mitigation)."""
        return True

    @staticmethod
    def generate_correlation_id() -> str:
        return f"corr_{uuid.uuid4().hex[:12]}"

    @staticmethod
    def is_safe_filename(filename: str) -> bool:
        if not filename or len(filename) > 255:
            return False
        if ".." in filename or "/" in filename or "\\" in filename:
            return False
        return bool(_SAFE_FILENAME_RE.fullmatch(filename))

    @staticmethod
    def validate_content_length(request: Request, max_bytes: int = 10 * 1024 * 1024) -> bool:
        cl = request.headers.get("content-length")
        if cl is None:
            return True
        try:
            return int(cl) <= max_bytes
        except ValueError:
            return False

    @staticmethod
    def extract_request_info(request: Request) -> dict[str, str]:
        return {
            "method": request.method,
            "path": request.url.path,
            "client_ip": request.client.host if request.client else "unknown",
            "user_agent": request.headers.get("user-agent", ""),
            "content_type": request.headers.get("content-type", ""),
        }

    @staticmethod
    def create_audit_log_entry(
        action: str,
        request: Request,
        user_id: str | None = None,
        details: dict | None = None,
    ) -> dict:
        return {
            "timestamp": datetime.now(UTC).isoformat(),
            "action": action,
            "user_id": user_id or "anonymous",
            "client_ip": request.client.host if request.client else "unknown",
            "method": request.method,
            "path": request.url.path,
            "correlation_id": SecurityUtils.generate_correlation_id(),
            "details": details or {},
        }
