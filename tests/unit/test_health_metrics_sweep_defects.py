"""Regression tests for the 2026-10-12 live read-op sweep defects D8, D9, D11, D12, D13.

Every fake backend here behaves like the real one where it matters: it scopes by
the ``X-Tenant-Id`` it receives and refuses a call that carries no credential.
The caller's tenant (TENANT_A) and the gateway's own service tenant (TENANT_B)
are deliberately DIFFERENT, so a route that asks "as the gateway" instead of "as
the caller" fails here instead of passing by coincidence (the conftest stubs both
to ``"n8n-dev"``, which is how D13 went unnoticed).
"""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

TENANT_A = "0195f48b-d000-7000-8000-00000000000a"  # the caller (licence-resolved)
TENANT_B = "0195f48b-d000-7000-8000-00000000000b"  # the gateway's own service tenant
SERVICE_TOKEN = "gw-own-licence-for-tests"
OWNED = {"ws-a": TENANT_A, "ws-b": TENANT_B}

_RealAsyncClient = httpx.AsyncClient


@pytest.fixture
def as_tenant_a(app, monkeypatch):
    """Caller resolves to TENANT_A; the gateway's own tenant is TENANT_B."""
    from auth.dependencies import require_tenant

    monkeypatch.setenv("CODING_SERVICE_TOKEN", SERVICE_TOKEN)
    monkeypatch.setattr("clients.outbound_identity.DEFAULT_TENANT_ID", TENANT_B)
    app.dependency_overrides[require_tenant] = lambda: TENANT_A
    yield
    app.dependency_overrides.pop(require_tenant, None)


def _tenant_scoped_backend(seen: list[httpx.Request], ok_body: dict) -> Callable:
    """A backend that 401s without a credential and 404s another tenant's name."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.headers.get("authorization") != f"Bearer {SERVICE_TOKEN}":
            return httpx.Response(401, json={"detail": "Authentication required"})
        name = request.url.path.split("/")[-1]
        if name in ("statistics", "live", "ready"):
            name = request.url.path.split("/")[-2]
        if OWNED.get(name) != request.headers.get("x-tenant-id"):
            return httpx.Response(404, json={"detail": f"Container '{name}' not found"})
        return httpx.Response(200, json=ok_body)

    return handler


def _patch_health_httpx(handler: Callable):
    def factory(*args, **kwargs):
        return _RealAsyncClient(transport=httpx.MockTransport(handler))

    return patch("routers.health.httpx.AsyncClient", side_effect=factory)


# ---------------------------------------------------------------------------
# D11 -- health container proxies must carry identity, scoped to the caller
# ---------------------------------------------------------------------------


class TestD11ContainerHealthCarriesIdentity:
    @pytest.mark.parametrize("suffix", ["/statistics", "/live", ""])
    async def test_valid_licence_gets_200_not_401(self, client, as_tenant_a, suffix):
        """Before the fix the gateway sent no credential and got 401 for every call."""
        seen: list[httpx.Request] = []
        with _patch_health_httpx(_tenant_scoped_backend(seen, {"name": "ws-a"})):
            resp = await client.get(f"/api/health/container/ws-a{suffix}")

        assert resp.status_code == 200, resp.text
        assert len(seen) == 1
        assert seen[0].headers["authorization"] == f"Bearer {SERVICE_TOKEN}"
        assert seen[0].headers["x-tenant-id"] == TENANT_A

    async def test_client_supplied_tenant_header_is_not_forwarded(self, client, as_tenant_a):
        """The forwarded tenant is the one the gateway resolved, never the wire's."""
        seen: list[httpx.Request] = []
        with _patch_health_httpx(_tenant_scoped_backend(seen, {})):
            await client.get(
                "/api/health/container/ws-a/statistics", headers={"x-tenant-id": TENANT_B}
            )
        assert seen[0].headers["x-tenant-id"] == TENANT_A

    @pytest.mark.parametrize("suffix", ["/statistics"])
    async def test_other_tenants_container_is_refused(self, client, as_tenant_a, suffix):
        """ws-b belongs to the gateway's own tenant: the caller must not read it."""
        seen: list[httpx.Request] = []
        with _patch_health_httpx(_tenant_scoped_backend(seen, {"leak": True})):
            resp = await client.get(f"/api/health/container/ws-b{suffix}")

        assert resp.status_code == 404
        assert "leak" not in resp.text

    @pytest.mark.parametrize("suffix", ["", "/live"])
    async def test_other_tenants_container_is_not_found_in_envelope(
        self, client, as_tenant_a, suffix
    ):
        seen: list[httpx.Request] = []
        with _patch_health_httpx(_tenant_scoped_backend(seen, {"leak": True})):
            resp = await client.get(f"/api/health/container/ws-b{suffix}")

        assert resp.status_code == 404
        assert resp.json()["error_code"] == "not_found"


# ---------------------------------------------------------------------------
# D12 -- the error envelope's status and code follow the backend's answer
# ---------------------------------------------------------------------------


class TestD12FaithfulContainerErrors:
    @pytest.mark.parametrize(
        ("backend", "status", "code"),
        [
            (401, 401, "unauthorized"),
            (403, 403, "forbidden"),
            (404, 404, "not_found"),
            (500, 502, "bad_gateway"),
            (503, 503, "service_unavailable"),
        ],
    )
    @pytest.mark.parametrize("suffix", ["", "/live"])
    async def test_status_and_error_code(self, client, as_tenant_a, suffix, backend, status, code):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(backend, json={"detail": "x"})

        with _patch_health_httpx(handler):
            resp = await client.get(f"/api/health/container/ws-a{suffix}")

        assert resp.status_code == status
        body = resp.json()
        assert body["error_code"] == code
        # monolith-compatible envelope is preserved
        for field in ("status", "message", "timestamp", "details", "request_id", "path", "method"):
            assert field in body
        assert body["path"] == f"/api/health/container/ws-a{suffix}"

    @pytest.mark.parametrize("suffix", ["", "/live"])
    async def test_unreachable_is_503_not_not_found(self, client, as_tenant_a, suffix):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        with _patch_health_httpx(handler):
            resp = await client.get(f"/api/health/container/ws-a{suffix}")

        assert resp.status_code == 503
        assert resp.json()["error_code"] == "service_unavailable"


# ---------------------------------------------------------------------------
# D8 / D9 -- metrics empty states are typed, never 500
# ---------------------------------------------------------------------------


def _analysis_client(seen: list[httpx.Request]) -> httpx.AsyncClient:
    centrality = {"metrics": {"f1": {"degree_centrality": 0.5}, "f2": {"degree_centrality": 0.1}}}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        name = request.url.path.split("/api/analysis/graphs/")[1].split("/")[0]
        if OWNED.get(name) != request.headers.get("x-tenant-id"):
            return httpx.Response(404, json={"detail": "not found"})
        if request.url.path.endswith("/metrics/centrality"):
            return httpx.Response(200, json=centrality)
        return httpx.Response(200, json={"container_name": name, "exists": True})

    return _RealAsyncClient(transport=httpx.MockTransport(handler))


class TestD8Centrality:
    async def test_no_container_is_typed_404_not_500(self, client, as_tenant_a):
        resp = await client.post("/api/health/metrics/centrality")
        assert resp.status_code == 404
        assert "container_name" in resp.json()["detail"]

    async def test_container_graph_is_computed_for_the_caller(self, client, as_tenant_a):
        seen: list[httpx.Request] = []
        with patch(
            "routers.metrics.get_proxy_client", AsyncMock(return_value=_analysis_client(seen))
        ):
            resp = await client.post(
                "/api/health/metrics/centrality?container_name=ws-a&entity_type=function"
            )

        assert resp.status_code == 200, resp.text
        assert resp.json()["entities_processed"] == 2
        assert all(r.headers["x-tenant-id"] == TENANT_A for r in seen)

    async def test_other_tenants_graph_is_404(self, client, as_tenant_a):
        seen: list[httpx.Request] = []
        with patch(
            "routers.metrics.get_proxy_client", AsyncMock(return_value=_analysis_client(seen))
        ):
            resp = await client.post("/api/health/metrics/centrality?container_name=ws-b")
        assert resp.status_code == 404

    async def test_upstream_5xx_is_502(self, client, as_tenant_a):
        svc = MagicMock()
        req = httpx.Request("GET", "http://coding-analysis/x")
        err = httpx.HTTPStatusError("boom", request=req, response=httpx.Response(500, request=req))
        svc.calculate_centrality_metrics = AsyncMock(side_effect=err)
        with patch("routers.metrics._get_metrics_service", AsyncMock(return_value=svc)):
            resp = await client.post("/api/health/metrics/centrality?container_name=ws-a")
        assert resp.status_code == 502


class TestD9MultiLanguagePerformance:
    async def test_empty_state_matches_sibling_typed_404(self, client):
        overall = await client.get("/api/health/metrics/multi-language/performance")
        sibling = await client.get("/api/health/metrics/multi-language/languages/python/stats")
        assert overall.status_code == 404
        assert sibling.status_code == 404


# ---------------------------------------------------------------------------
# D13 -- cache eviction existence check asks as the CALLER's tenant
# ---------------------------------------------------------------------------


class TestD13EvictionExistenceCheck:
    @pytest.fixture
    def cache_manager(self):
        cm = MagicMock()
        cm.get_eviction_history = AsyncMock(return_value={"evictions": [], "total": 0})
        return cm

    async def test_callers_workspace_is_found(self, client, as_tenant_a, cache_manager):
        seen: list[httpx.Request] = []
        with (
            patch("routers.proxy.get_proxy_client", AsyncMock(return_value=_analysis_client(seen))),
            patch("routers.cache.get_cache_manager", AsyncMock(return_value=cache_manager)),
        ):
            resp = await client.get("/api/cache/evictions/ws-a")

        assert resp.status_code == 200, resp.text
        assert seen[0].headers["x-tenant-id"] == TENANT_A

    async def test_gateway_tenants_workspace_is_not_lent_to_caller(
        self, client, as_tenant_a, cache_manager
    ):
        seen: list[httpx.Request] = []
        with (
            patch("routers.proxy.get_proxy_client", AsyncMock(return_value=_analysis_client(seen))),
            patch("routers.cache.get_cache_manager", AsyncMock(return_value=cache_manager)),
        ):
            resp = await client.get("/api/cache/evictions/ws-b")

        assert resp.status_code == 404
        cache_manager.get_eviction_history.assert_not_awaited()
