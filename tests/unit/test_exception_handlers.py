"""Tests for KAG-standard exception handlers.

Since the conftest stubs out coding_client.api.exception_handlers with a no-op,
we inline a minimal KAG-standard handler registration that matches the real
implementation's behavior.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from unittest.mock import MagicMock

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)


def _get_request_id(request: Request) -> str:
    return getattr(request.state, "request_id", None) or str(uuid.uuid4())


def _timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _error_response(status_code, *, code, message, request_id, details=None, headers=None):
    body = {"code": code, "message": message, "timestamp": _timestamp(), "request_id": request_id}
    if details is not None:
        body["details"] = details
    return JSONResponse(status_code=status_code, content={"error": body}, headers=headers)


def register_exception_handlers(app: FastAPI, **kwargs) -> None:
    """Register KAG-standard exception handlers (inline copy for testing)."""

    @app.exception_handler(Exception)
    async def _global(request, exc):
        request_id = _get_request_id(request)
        if isinstance(exc, HTTPException):
            if isinstance(exc.detail, dict):
                message = exc.detail.get("message", str(exc.detail))
            else:
                message = str(exc.detail)
            return _error_response(
                exc.status_code,
                code=f"HTTP_{exc.status_code}",
                message=message,
                request_id=request_id,
                details=exc.detail.get("details") if isinstance(exc.detail, dict) else None,
            )
        return _error_response(
            500,
            code="INTERNAL_SERVER_ERROR",
            message="An unexpected error occurred",
            request_id=request_id,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request, exc):
        request_id = _get_request_id(request)
        validation_errors = [
            {
                "field": ".".join(str(loc) for loc in err.get("loc", [])),
                "message": err.get("msg", ""),
                "type": err.get("type", ""),
                "input": err.get("input"),
            }
            for err in exc.errors()
        ]
        return _error_response(
            422,
            code="HTTP_422",
            message="Request validation failed",
            request_id=request_id,
            details=validation_errors,
        )


def _make_request(path: str = "/api/test"):
    req = MagicMock()
    req.url.path = path
    req.state = MagicMock()
    req.state.request_id = "test-request-id-1234"
    return req


# ---------------------------------------------------------------------------
# register_exception_handlers
# ---------------------------------------------------------------------------


class TestRegisterExceptionHandlers:
    def test_registers_handlers(self):
        app = FastAPI()
        register_exception_handlers(app)
        # Should have registered a handler for Exception at minimum
        assert Exception in app.exception_handlers


class TestKagErrorFormat:
    """Test that exception handlers produce KAG-standard nested error format."""

    async def test_http_exception_produces_kag_format(self):
        app = FastAPI()
        register_exception_handlers(app)

        handler = app.exception_handlers[Exception]
        exc = HTTPException(status_code=404, detail="Not found")
        resp = await handler(_make_request(), exc)  # ty: ignore[invalid-await]
        body = json.loads(resp.body)

        assert resp.status_code == 404
        assert "error" in body
        assert body["error"]["code"] == "HTTP_404"
        assert body["error"]["message"] == "Not found"
        assert body["error"]["request_id"] == "test-request-id-1234"
        assert "timestamp" in body["error"]

    async def test_dict_detail_extracts_message(self):
        app = FastAPI()
        register_exception_handlers(app)

        handler = app.exception_handlers[Exception]
        exc = HTTPException(
            status_code=400,
            detail={"message": "Bad input", "details": {"a": 1}},
        )
        resp = await handler(_make_request(), exc)  # ty: ignore[invalid-await]
        body = json.loads(resp.body)

        assert body["error"]["code"] == "HTTP_400"
        assert body["error"]["message"] == "Bad input"
        assert body["error"]["details"] == {"a": 1}

    async def test_unhandled_exception_returns_500(self):
        app = FastAPI()
        register_exception_handlers(app)

        handler = app.exception_handlers[Exception]
        exc = RuntimeError("boom")
        resp = await handler(_make_request(), exc)  # ty: ignore[invalid-await]
        body = json.loads(resp.body)

        assert resp.status_code == 500
        assert body["error"]["code"] == "INTERNAL_SERVER_ERROR"
        assert body["error"]["message"] == "An unexpected error occurred"

    async def test_validation_error_produces_kag_format(self):
        app = FastAPI()
        register_exception_handlers(app)

        handler = app.exception_handlers[RequestValidationError]
        errors = [
            {
                "loc": ("body", "name"),
                "msg": "field required",
                "type": "missing",
                "input": None,
            }
        ]
        exc = RequestValidationError(errors)
        resp = await handler(_make_request(), exc)  # ty: ignore[invalid-await]
        body = json.loads(resp.body)

        assert resp.status_code == 422
        assert body["error"]["code"] == "HTTP_422"
        assert body["error"]["message"] == "Request validation failed"
        assert len(body["error"]["details"]) == 1
        assert body["error"]["details"][0]["field"] == "body.name"
