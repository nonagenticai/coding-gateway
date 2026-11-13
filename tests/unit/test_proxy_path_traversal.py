"""The proxy must refuse path traversal and forward the RAW (still-encoded) path.

The defect (auditor, measured 2026-09-22): the proxy forwarded the DECODED
path and httpx 0.28.1 resolves literal dot-segments, so
``/api/repositories/files/..%2Fapprovals`` left the gateway as
``/api/repositories/approvals`` -- the caller escaped the path it was routed on.

Every refusal test asserts the upstream client was NEVER called: a 400 that
still forwarded the request would be a refusal in name only.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import unquote

import httpx
import pytest


def _proxy():
    # Imported lazily: routers.proxy reads Settings at import time, and conftest
    # only sets the test environment once collection has finished.
    from routers import proxy

    return proxy


def _mock_backend() -> AsyncMock:
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(
        return_value=httpx.Response(
            200, content=b'{"ok": true}', headers={"content-type": "application/json"}
        )
    )
    return mock_client


async def _asgi_get(app, raw_path: str) -> tuple[int, dict]:
    """Drive the app with an exact raw path, as uvicorn would deliver it.

    An httpx test client normalises literal ``..`` client-side before sending,
    so it can never deliver the unnormalised path a real socket can. This goes
    underneath it.
    """
    raw = raw_path.encode("latin-1")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": unquote(raw_path),
        "raw_path": raw,
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"test")],
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
    }
    sent: list[dict] = []
    received = False

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], json.loads(body) if body else {}


TRAVERSAL_PATHS = [
    "/api/repositories/files/..%2Fapprovals",  # the measured case
    "/api/repositories/files/..%2fapprovals",  # lower-case escape
    "/api/repositories/%2e%2e/approvals",  # encoded dots
    "/api/repositories/%2E%2e/approvals",  # mixed-case encoded dots
    "/api/repositories/files/%2e%2e%2f%2e%2e%2fapprovals",
    "/api/repositories/files/..\\approvals",  # backslash
    "/api/repositories/files/..%5Capprovals",  # encoded backslash
    "/api/repositories/files/..%5capprovals",
    "/api/repositories/%252e%252e/approvals",  # double-encoded dots
    "/api/repositories/files/..%252Fapprovals",  # double-encoded slash
    "/api/repositories/%25252e%25252e/approvals",  # triple-encoded dots
    "/api/repositories/files%00/x",  # encoded NUL
    "/api/repositories/owner%2Frepo/files",  # encoded slash, no dots at all
    "/api/repositories/owner%2frepo/files",
    "/api/repositories/./files",  # single-dot segment
    "/api/repositories/%2e/files",
    "/api/repositories/files/../../approvals",  # literal (raw ASGI only)
    "/api/analysis/..",  # trailing dot-segment
]


class TestTraversalRefused:
    @pytest.mark.parametrize("raw_path", TRAVERSAL_PATHS)
    async def test_refused_with_400_and_upstream_never_called(self, app, raw_path):
        mock = _mock_backend()
        with patch("routers.proxy.get_proxy_client", return_value=mock):
            status, body = await _asgi_get(app, raw_path)
        assert status == 400, (raw_path, status, body)
        assert body["error"]["code"] == "HTTP_400"
        assert body["error"]["message"].startswith("Invalid request path:")
        mock.request.assert_not_called()

    async def test_measured_case_through_the_http_client(self, client):
        """Same refusal via the ordinary httpx test client (raw path kept encoded)."""
        mock = _mock_backend()
        with patch("routers.proxy.get_proxy_client", return_value=mock):
            resp = await client.get("/api/repositories/files/..%2Fapprovals")
        assert resp.status_code == 400
        mock.request.assert_not_called()

    async def test_prefix_escape_refused_even_if_the_scan_is_bypassed(self, app):
        """Defence in depth: disable the scan and the prefix check alone must hold."""
        mock = _mock_backend()
        with (
            patch("routers.proxy.get_proxy_client", return_value=mock),
            patch("routers.proxy._path_rejection_reason", return_value=None),
        ):
            status, body = await _asgi_get(app, "/api/repositories/../approvals")
        assert status == 400
        assert "escapes its route" in body["error"]["message"]
        mock.request.assert_not_called()


# Every prefix the routing tests exercise, forwarded to the backend unchanged.
LEGIT_PATHS = [
    "/api/analysis/x",
    "/api/bug-hunter/x",
    "/api/quality/x",
    "/api/quality/my-container/gate",
    "/api/tests/x",
    "/api/ai/x",
    "/api/workspaces/x",
    "/api/containers/x",
    "/api/repositories/x",
    "/api/repositories",
    "/api/webhooks/pr-actions/x",
    "/api/security/x",
    "/api/dependencies/x",
    "/api/ws-health/x",
    "/api/realtime/x",
    "/api/broadcast/x",
    "/api/stats/x",
    "/api/violations/x",
    "/api/global-stats/x",
    "/api/active-connections/x",
    "/api/websocket-docs/x",
    # encodings that must survive byte-for-byte
    "/api/repositories/my%20repo/files",
    "/api/repositories/a+b/files",
    "/api/repositories/a%2Bb/files",
    "/api/repositories/v1.2.3/files",
    "/api/repositories/...hidden/files",  # three dots is a name, not a dot-segment
    "/api/repositories/file.txt",
    "/api/repositories/%E2%9C%93",
]


class TestLegitimatePathsForwardRaw:
    @pytest.mark.parametrize("raw_path", LEGIT_PATHS)
    async def test_forwarded_unchanged(self, client, raw_path):
        mock = _mock_backend()
        with patch("routers.proxy.get_proxy_client", return_value=mock):
            resp = await client.get(raw_path)
        assert resp.status_code == 200, (raw_path, resp.text)
        url = mock.request.call_args.kwargs["url"]
        backend = _proxy()._resolve_backend(unquote(raw_path))
        assert backend is not None
        assert url == f"{backend[0]}{raw_path}"
        # ...and what httpx will actually put on the wire is still the raw path.
        assert httpx.URL(url).raw_path.decode() == raw_path

    async def test_query_string_still_appended(self, client):
        mock = _mock_backend()
        with patch("routers.proxy.get_proxy_client", return_value=mock):
            resp = await client.get("/api/repositories/my%20repo?q=a+b&x=%2F")
        assert resp.status_code == 200
        assert mock.request.call_args.kwargs["url"].endswith(
            "/api/repositories/my%20repo?q=a+b&x=%2F"
        )


class TestRejectionReason:
    @pytest.mark.parametrize("raw_path", TRAVERSAL_PATHS)
    def test_every_traversal_variant_has_a_reason(self, raw_path):
        assert _proxy()._path_rejection_reason(raw_path) is not None

    @pytest.mark.parametrize("raw_path", LEGIT_PATHS)
    def test_no_legitimate_path_has_a_reason(self, raw_path):
        assert _proxy()._path_rejection_reason(raw_path) is None

    def test_over_encoded_path_refused(self):
        # "A" percent-encoded six times: still changing after the last decode pass.
        deep = "/api/x/%" + "25" * 5 + "41"
        assert _proxy()._path_rejection_reason(deep) == "path is percent-encoded too many times"


class TestWebSocketTraversal:
    def _ws(self, raw_path: str):
        return SimpleNamespace(
            scope={"raw_path": raw_path.encode("latin-1")},
            url=SimpleNamespace(path=unquote(raw_path)),
            close=AsyncMock(),
        )

    @pytest.mark.parametrize(
        "raw_path",
        ["/api/quality/ws/..", "/api/realtime/ws/..%2F..%2Fadmin", "/api/realtime/ws/%252e%252e/x"],
    )
    async def test_upgrade_denied(self, raw_path):
        ws = self._ws(raw_path)
        assert await _proxy()._refuse_ws_traversal(ws) is True
        ws.close.assert_awaited_once()
        assert ws.close.call_args.kwargs["code"] == 1008

    async def test_legit_upgrade_passes(self):
        ws = self._ws("/api/quality/ws/my-container")
        assert await _proxy()._refuse_ws_traversal(ws) is False
        ws.close.assert_not_called()
