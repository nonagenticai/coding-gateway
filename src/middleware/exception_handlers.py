"""Global exception handlers for the coding-gateway (KAG standard).

Delegates to coding_client.api.exception_handlers for the nested
``{error: {code, message, timestamp, request_id, details}}`` envelope.
"""

from __future__ import annotations

from coding_client.api.exception_handlers import register_exception_handlers as _register
from fastapi import FastAPI


def register_exception_handlers(app: FastAPI) -> None:
    """Attach KAG-standard exception handlers to the FastAPI application."""
    from config.settings import get_settings

    settings = get_settings()
    debug = getattr(settings, "debug", False)
    if isinstance(debug, str):
        debug = debug.lower() == "true"
    _register(app, debug=debug)
