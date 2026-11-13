"""Tests for proxy client lifecycle and WebSocket route registration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx

# ---------------------------------------------------------------------------
# get_proxy_client lifecycle
# ---------------------------------------------------------------------------


class TestGetProxyClient:
    """Lazy creation and reuse of the shared httpx.AsyncClient."""

    async def test_creates_client_on_first_call(self):
        import routers.proxy as pm

        original = pm._client
        pm._client = None
        try:
            client = await pm.get_proxy_client()
            assert isinstance(client, httpx.AsyncClient)
            assert not client.is_closed
        finally:
            if pm._client is not None and pm._client is not original:
                await pm._client.aclose()
            pm._client = original

    async def test_returns_same_client_on_subsequent_calls(self):
        import routers.proxy as pm

        original = pm._client
        pm._client = None
        try:
            first = await pm.get_proxy_client()
            second = await pm.get_proxy_client()
            assert first is second
        finally:
            if pm._client is not None and pm._client is not original:
                await pm._client.aclose()
            pm._client = original

    async def test_recreates_client_if_closed(self):
        import routers.proxy as pm

        original = pm._client
        pm._client = None
        try:
            first = await pm.get_proxy_client()
            await first.aclose()
            assert first.is_closed
            second = await pm.get_proxy_client()
            assert second is not first
            assert isinstance(second, httpx.AsyncClient)
            assert not second.is_closed
        finally:
            if pm._client is not None and pm._client is not original:
                await pm._client.aclose()
            pm._client = original


# ---------------------------------------------------------------------------
# close_proxy_client lifecycle
# ---------------------------------------------------------------------------


class TestCloseProxyClient:
    """Shutdown of the shared client."""

    async def test_closes_the_client(self):
        import routers.proxy as pm

        original = pm._client
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.aclose = AsyncMock()
        pm._client = mock_client
        try:
            await pm.close_proxy_client()
            mock_client.aclose.assert_awaited_once()
            assert pm._client is None
        finally:
            pm._client = original

    async def test_idempotent_when_already_none(self):
        import routers.proxy as pm

        original = pm._client
        pm._client = None
        try:
            await pm.close_proxy_client()  # must not raise
            assert pm._client is None
        finally:
            pm._client = original

    async def test_idempotent_when_already_closed(self):
        import routers.proxy as pm

        original = pm._client
        mock_client = MagicMock()
        mock_client.is_closed = True
        mock_client.aclose = AsyncMock()
        pm._client = mock_client
        try:
            await pm.close_proxy_client()  # must not raise
            # Client was already closed, so aclose should NOT have been called
            mock_client.aclose.assert_not_awaited()
        finally:
            pm._client = original


# ---------------------------------------------------------------------------
# WebSocket routes are registered on the router
# ---------------------------------------------------------------------------


class TestWebSocketRoutes:
    """WebSocket endpoints must be registered on the proxy router."""

    def test_realtime_ws_route_registered(self):
        from routers.proxy import router

        ws_paths = [
            r.path for r in router.routes if hasattr(r, "path") and getattr(r, "path", None)
        ]
        assert "/api/realtime/ws/{path:path}" in ws_paths

    def test_broadcast_subscribe_route_registered(self):
        from routers.proxy import router

        ws_paths = [
            r.path for r in router.routes if hasattr(r, "path") and getattr(r, "path", None)
        ]
        assert "/api/broadcast/subscribe" in ws_paths
