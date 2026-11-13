"""WS proxy close-code propagation.

When the backend WS closes with a structured code + reason (e.g.
coding-analysis emits ``code=1008, reason="Container validation failed: …"``
for a missing container), the gateway must forward that exact code + reason
to the client. Without this, downstream consumers (e.g. fr-backend's
``/quality`` proxy) cannot translate upstream policy violations into typed
frames for the frontend and the user sees a generic abnormal close
followed by a 5× reconnect storm.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.websockets import WebSocketState


def _make_mock_client_ws(connected: bool = True) -> AsyncMock:
    client_ws = AsyncMock()
    client_ws.accept = AsyncMock()
    client_ws.client_state = WebSocketState.CONNECTED if connected else WebSocketState.DISCONNECTED
    client_ws.close = AsyncMock()
    client_ws.headers = {}
    client_ws.state = MagicMock()
    client_ws.state.user = None
    # No client→backend traffic; first receive is a disconnect.
    client_ws.receive = AsyncMock(
        side_effect=[
            {"type": "websocket.disconnect"},
        ]
    )
    return client_ws


def _make_mock_backend_ws(close_code: int | None, close_reason: str | None = None) -> AsyncMock:
    mock_backend_ws = AsyncMock()
    mock_backend_ws.send = AsyncMock()
    mock_backend_ws.close = AsyncMock()
    # Backend immediately closed → empty message stream.
    mock_backend_ws.__aiter__ = MagicMock(return_value=iter([]))
    mock_backend_ws.__aenter__ = AsyncMock(return_value=mock_backend_ws)
    mock_backend_ws.__aexit__ = AsyncMock(return_value=False)
    # The websockets library populates close_code / close_reason on the
    # connection object after the WS closes. Set as plain attributes so
    # `getattr` returns the literal value (AsyncMock would return a child
    # mock and break the `is None` check).
    mock_backend_ws.close_code = close_code
    mock_backend_ws.close_reason = close_reason
    return mock_backend_ws


class TestProxyWebSocketCloseCodePropagation:
    @pytest.mark.asyncio
    async def test_propagates_1008_with_reason(self):
        """Backend close(code=1008, reason='…') → client close(code=1008, reason='…')."""
        from routers.proxy import _proxy_websocket

        client_ws = _make_mock_client_ws()
        mock_backend_ws = _make_mock_backend_ws(
            close_code=1008,
            close_reason="Container validation failed: Container 'main' not found",
        )

        with patch.dict(
            "sys.modules",
            {"websockets": MagicMock(connect=MagicMock(return_value=mock_backend_ws))},
        ):
            await _proxy_websocket(client_ws, "ws://backend:8080/ws")

        client_ws.close.assert_awaited_once_with(
            code=1008,
            reason="Container validation failed: Container 'main' not found",
        )

    @pytest.mark.asyncio
    async def test_propagates_1000_normal_close(self):
        """Backend close(code=1000) → client close(code=1000)."""
        from routers.proxy import _proxy_websocket

        client_ws = _make_mock_client_ws()
        mock_backend_ws = _make_mock_backend_ws(close_code=1000, close_reason="")

        with patch.dict(
            "sys.modules",
            {"websockets": MagicMock(connect=MagicMock(return_value=mock_backend_ws))},
        ):
            await _proxy_websocket(client_ws, "ws://backend:8080/ws")

        client_ws.close.assert_awaited_once_with(code=1000, reason="")

    @pytest.mark.asyncio
    async def test_none_close_code_uses_default_close(self):
        """Backend dropped without a Close frame → client gets generic close()."""
        from routers.proxy import _proxy_websocket

        client_ws = _make_mock_client_ws()
        mock_backend_ws = _make_mock_backend_ws(close_code=None, close_reason=None)

        with patch.dict(
            "sys.modules",
            {"websockets": MagicMock(connect=MagicMock(return_value=mock_backend_ws))},
        ):
            await _proxy_websocket(client_ws, "ws://backend:8080/ws")

        client_ws.close.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_disconnected_client_skips_close(self):
        """If the client already disconnected, don't attempt to close it again."""
        from routers.proxy import _proxy_websocket

        client_ws = _make_mock_client_ws(connected=False)
        mock_backend_ws = _make_mock_backend_ws(close_code=1008, close_reason="whatever")

        with patch.dict(
            "sys.modules",
            {"websockets": MagicMock(connect=MagicMock(return_value=mock_backend_ws))},
        ):
            await _proxy_websocket(client_ws, "ws://backend:8080/ws")

        client_ws.close.assert_not_called()
