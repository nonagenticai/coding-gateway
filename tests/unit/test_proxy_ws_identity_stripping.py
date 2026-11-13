"""WebSocket proxy must not forward client-supplied identity headers.

The HTTP proxy path strips ``_CLIENT_SPOOFABLE_IDENTITY`` and re-injects the
``require_tenant``-resolved tenant. The WebSocket path used to forward the raw
upgrade headers verbatim, so any caller reaching the gateway WS route without
traversing Istio's sanitisation could:

  * set ``x-tenant-id`` and read another tenant's stream (cross-tenant BOLA on
    the websocket surface), and
  * set ``x-user-roles: admin`` and escalate at any backend that trusts it.

These tests assert on the headers ACTUALLY handed to ``websockets.connect``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from starlette.websockets import WebSocketState

# A realistic UUIDv7 license tenant (production shape) that the gateway resolves.
RESOLVED_TENANT = "0195f48b-d000-7000-8000-000000000001"
# The tenant an attacker forges on the upgrade handshake.
FORGED_TENANT = "0195f48b-d000-7000-8000-0000000000ff"


def _make_mock_client_ws(headers: dict[str, str] | None = None) -> AsyncMock:
    client_ws = AsyncMock()
    client_ws.accept = AsyncMock()
    client_ws.client_state = WebSocketState.CONNECTED
    client_ws.close = AsyncMock()
    client_ws.headers = headers or {}
    client_ws.state = MagicMock()
    client_ws.state.user = None
    # No client→backend traffic; first receive is a disconnect.
    client_ws.receive = AsyncMock(side_effect=[{"type": "websocket.disconnect"}])
    return client_ws


def _make_mock_backend_ws() -> AsyncMock:
    backend_ws = AsyncMock()
    backend_ws.send = AsyncMock()
    backend_ws.close = AsyncMock()
    backend_ws.__aiter__ = MagicMock(return_value=iter([]))
    backend_ws.__aenter__ = AsyncMock(return_value=backend_ws)
    backend_ws.__aexit__ = AsyncMock(return_value=False)
    backend_ws.close_code = 1000
    backend_ws.close_reason = ""
    return backend_ws


async def _run_proxy(client_ws) -> MagicMock:
    """Drive `_proxy_websocket` with a stubbed `websockets` module.

    Returns the `websockets.connect` mock so the caller can inspect the headers
    that were actually offered to the backend on the upgrade handshake.
    """
    from routers.proxy import _proxy_websocket

    connect = MagicMock(return_value=_make_mock_backend_ws())
    with patch.dict("sys.modules", {"websockets": MagicMock(connect=connect)}):
        await _proxy_websocket(client_ws, "ws://backend:8080/ws")
    return connect


def _sent_headers(connect: MagicMock) -> dict[str, str]:
    return connect.call_args.kwargs["additional_headers"]


class TestWebSocketForgedIdentityIsStripped:
    @pytest.mark.asyncio
    async def test_forged_tenant_does_not_reach_the_upstream(self):
        """A client-supplied x-tenant-id on the upgrade must never be forwarded;
        the gateway-resolved tenant wins (cross-tenant BOLA guard)."""
        client_ws = _make_mock_client_ws(
            {"x-tenant-id": FORGED_TENANT, "accept": "application/json"}
        )

        with patch("routers.proxy.require_tenant", AsyncMock(return_value=RESOLVED_TENANT)):
            connect = await _run_proxy(client_ws)

        headers = _sent_headers(connect)
        assert headers["x-tenant-id"] == RESOLVED_TENANT
        assert headers["x-tenant-id"] != FORGED_TENANT
        # Unrelated headers still ride along.
        assert headers["accept"] == "application/json"

    @pytest.mark.asyncio
    async def test_forged_roles_and_user_headers_do_not_reach_the_upstream(self):
        """x-user-roles / x-user-id / x-user-email / x-organization-id supplied by
        the client must be dropped — the gateway has no auth context on the WS
        path, so they are re-injected from nothing, not from the client."""
        client_ws = _make_mock_client_ws(
            {
                "x-user-id": "spoofed",
                "x-user-roles": "admin",
                "x-user-name": "evil",
                "x-user-email": "evil@example.com",
                "x-organization-id": "victim-org",
                "x-tenant-id": FORGED_TENANT,
            }
        )

        with patch("routers.proxy.require_tenant", AsyncMock(return_value=RESOLVED_TENANT)):
            connect = await _run_proxy(client_ws)

        headers = _sent_headers(connect)
        for spoofed in (
            "x-user-id",
            "x-user-roles",
            "x-user-name",
            "x-user-email",
            "x-organization-id",
        ):
            assert spoofed not in headers, f"{spoofed} must not reach the backend"
        assert headers["x-tenant-id"] == RESOLVED_TENANT

    @pytest.mark.asyncio
    async def test_gateway_auth_context_is_reinjected_when_present(self):
        """When the gateway DOES have an auth context, its values are injected —
        but the tenant still comes from the resolver, not from user.tenant_id
        (which is DEFAULT_TENANT_ID for the synthetic trusted-network user)."""
        client_ws = _make_mock_client_ws({"x-user-roles": "admin"})
        client_ws.state.user = MagicMock(
            sub="real-user",
            tenant_id="n8n-dev",  # synthetic default that must NOT win
            preferred_username="real",
            email="real@example.com",
        )

        with patch("routers.proxy.require_tenant", AsyncMock(return_value=RESOLVED_TENANT)):
            connect = await _run_proxy(client_ws)

        headers = _sent_headers(connect)
        assert headers["x-user-id"] == "real-user"
        assert headers["x-user-name"] == "real"
        assert headers["x-user-email"] == "real@example.com"
        assert headers["x-tenant-id"] == RESOLVED_TENANT
        assert headers["x-tenant-id"] != "n8n-dev"
        # The client's forged roles are still gone.
        assert "x-user-roles" not in headers


class TestWebSocketFailsClosedWithoutTenant:
    @pytest.mark.asyncio
    async def test_unresolvable_tenant_rejects_the_upgrade(self):
        """require_tenant raising 401 must reject the handshake, NOT proxy the
        connection with whatever x-tenant-id the client supplied."""
        client_ws = _make_mock_client_ws({"x-tenant-id": FORGED_TENANT})

        with patch(
            "routers.proxy.require_tenant",
            AsyncMock(side_effect=HTTPException(status_code=401, detail="Tenant required")),
        ):
            connect = await _run_proxy(client_ws)

        connect.assert_not_called()
        client_ws.accept.assert_not_called()
        client_ws.close.assert_awaited_once_with(code=1008, reason="Tenant context required")

    @pytest.mark.asyncio
    async def test_resolver_blowing_up_rejects_the_upgrade(self):
        """An unexpected resolver failure (e.g. introspection error) fails closed
        rather than degrading to the client-supplied header."""
        client_ws = _make_mock_client_ws({"x-tenant-id": FORGED_TENANT})

        with patch(
            "routers.proxy.require_tenant",
            AsyncMock(side_effect=RuntimeError("introspection exploded")),
        ):
            connect = await _run_proxy(client_ws)

        connect.assert_not_called()
        client_ws.accept.assert_not_called()
        client_ws.close.assert_awaited_once_with(code=1008, reason="Tenant context required")

    @pytest.mark.asyncio
    async def test_empty_tenant_rejects_the_upgrade(self):
        """A resolver returning an empty string is not a tenant context."""
        client_ws = _make_mock_client_ws({"x-tenant-id": FORGED_TENANT})

        with patch("routers.proxy.require_tenant", AsyncMock(return_value="")):
            connect = await _run_proxy(client_ws)

        connect.assert_not_called()
        client_ws.accept.assert_not_called()
        client_ws.close.assert_awaited_once_with(code=1008, reason="Tenant context required")
