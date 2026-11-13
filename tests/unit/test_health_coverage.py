"""Additional health endpoint coverage — psutil branches, container proxy endpoints,
detailed health, and _proxy_to_containers helper."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException
from fastapi.responses import Response

# ---------------------------------------------------------------------------
# Helper: inject a mock psutil into routers.health
# ---------------------------------------------------------------------------


def _make_mock_psutil():
    """Create a mock psutil with all needed sub-objects."""
    mock_vm = MagicMock()
    mock_vm.percent = 45.0
    mock_vm.used = 4 * 1024 * 1024 * 1024
    mock_vm.total = 16 * 1024 * 1024 * 1024
    mock_vm._asdict.return_value = {"total": 16000000000, "used": 4000000000, "percent": 45.0}

    mock_swap = MagicMock()
    mock_swap._asdict.return_value = {"total": 4000000000}

    mock_disk = MagicMock()
    mock_disk.percent = 60.0
    mock_disk._asdict.return_value = {"total": 500000000000, "percent": 60.0}

    mock_net = MagicMock()
    mock_net._asdict.return_value = {"bytes_sent": 1000}

    mock_proc = MagicMock()
    mock_proc.cpu_percent.return_value = 5.0
    mock_mem_info = MagicMock()
    mock_mem_info._asdict.return_value = {"rss": 100000}
    mock_proc.memory_info.return_value = mock_mem_info
    mock_proc.num_threads.return_value = 4
    mock_proc.create_time.return_value = 1700000000.0
    mock_proc.open_files.return_value = [MagicMock(), MagicMock()]

    p = MagicMock()
    p.cpu_percent.return_value = 12.5
    p.cpu_count.return_value = 8
    p.virtual_memory.return_value = mock_vm
    p.swap_memory.return_value = mock_swap
    p.disk_usage.return_value = mock_disk
    p.boot_time.return_value = 1700000000.0
    p.net_io_counters.return_value = mock_net
    p.Process.return_value = mock_proc
    return p


def _patch_psutil(hmod, mock_psutil):
    """Context-manager-style helper to swap psutil on routers.health module."""

    class _Ctx:
        def __init__(self):
            self.old_psutil = getattr(hmod, "psutil", None)
            self.old_has = hmod.HAS_PSUTIL

        def __enter__(self):
            hmod.psutil = mock_psutil
            hmod.HAS_PSUTIL = True
            return self

        def __exit__(self, *args):
            hmod.HAS_PSUTIL = self.old_has
            if self.old_psutil is not None:
                hmod.psutil = self.old_psutil

    return _Ctx()


# ---------------------------------------------------------------------------
# System metrics with psutil available
# ---------------------------------------------------------------------------


class TestSystemMetricsWithPsutil:
    async def test_returns_real_cpu_memory_disk(self, client):
        import routers.health as hmod

        with _patch_psutil(hmod, _make_mock_psutil()):
            resp = await client.get("/api/health/system-metrics")
            assert resp.status_code == 200
            data = resp.json()
            assert data["cpu_percent"] == 12.5
            assert data["memory_percent"] == 45.0
            assert data["disk_percent"] == 60.0
            assert data["memory_used_mb"] is not None
            assert data["memory_total_mb"] is not None


# ---------------------------------------------------------------------------
# Health router metrics with psutil
# ---------------------------------------------------------------------------


class TestHealthRouterMetricsWithPsutil:
    async def test_returns_system_network_process_sections(self, client):
        import routers.health as hmod

        with _patch_psutil(hmod, _make_mock_psutil()):
            resp = await client.get("/api/health/metrics")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "success"
            metrics = data["metrics"]
            assert "system" in metrics
            assert "network" in metrics
            assert "process" in metrics
            assert metrics["system"]["cpu_count"] == 8

    async def test_psutil_exception_fallback(self, client):
        """When psutil methods raise, fallback values are used."""
        import routers.health as hmod

        broken_psutil = MagicMock()
        broken_psutil.cpu_count.side_effect = RuntimeError("fail")
        broken_psutil.virtual_memory.side_effect = RuntimeError("fail")
        broken_psutil.swap_memory.side_effect = RuntimeError("fail")
        broken_psutil.disk_usage.side_effect = RuntimeError("fail")
        broken_psutil.net_io_counters.side_effect = RuntimeError("fail")
        broken_psutil.Process.side_effect = RuntimeError("fail")

        with _patch_psutil(hmod, broken_psutil):
            resp = await client.get("/api/health/metrics")
            assert resp.status_code == 200
            data = resp.json()
            assert data["metrics"]["system"]["cpu_percent"] == 0.0
            assert data["metrics"]["network"] == {}
            assert data["metrics"]["process"]["cpu_percent"] == 0.0


# ---------------------------------------------------------------------------
# Detailed health
# ---------------------------------------------------------------------------


def _mock_httpx_all_200():
    """Return patch context for httpx where all backends return 200."""
    mock_response = MagicMock(status_code=200)
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return patch("routers.health._health_http_client", mock_client)


class TestDetailedHealth:
    async def test_all_healthy_returns_full_structure(self, client):
        with _mock_httpx_all_200():
            resp = await client.get("/api/health/detailed")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "success"
            assert data["system"]["status"] == "healthy"
            assert "resources" in data
            assert "database" in data
            assert "components" in data
            assert "containers" in data

    async def test_backend_failure_causes_degraded(self, client):
        call_count = 0

        async def fake_get(url):
            nonlocal call_count
            call_count += 1
            return MagicMock(status_code=503 if call_count == 2 else 200)

        mock_client = AsyncMock()
        mock_client.get = fake_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("routers.health._health_http_client", mock_client):
            resp = await client.get("/api/health/detailed")
            assert resp.status_code == 200
            assert resp.json()["system"]["status"] == "degraded"

    async def test_has_timestamp_and_request_id(self, client):
        with _mock_httpx_all_200():
            resp = await client.get("/api/health/detailed")
            data = resp.json()
            assert "timestamp" in data
            assert isinstance(data["request_id"], str) and len(data["request_id"]) > 0

    async def test_components_include_api_server(self, client):
        with _mock_httpx_all_200():
            resp = await client.get("/api/health/detailed")
            data = resp.json()
            assert isinstance(data["components"], list)
            assert data["components"][0]["name"] == "API Server"
            assert data["components"][0]["status"] == "healthy"

    async def test_resources_have_expected_keys(self, client):
        with _mock_httpx_all_200():
            resp = await client.get("/api/health/detailed")
            res = resp.json()["resources"]
            assert "cpu_usage" in res
            assert "memory_usage" in res
            assert "disk_usage" in res

    async def test_404_from_backend_counts_as_degraded(self, client):
        mock_response = MagicMock(status_code=404)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("routers.health._health_http_client", mock_client):
            resp = await client.get("/api/health/detailed")
            assert resp.json()["system"]["status"] == "degraded"

    async def test_detailed_with_psutil(self, client):
        import routers.health as hmod

        mock_response = MagicMock(status_code=200)
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_response)
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)

        with _patch_psutil(hmod, _make_mock_psutil()):
            with patch("routers.health.httpx.AsyncClient", return_value=mock_http):
                resp = await client.get("/api/health/detailed")
                assert resp.status_code == 200
                data = resp.json()
                assert data["resources"]["cpu_usage"] == 12.5
                assert data["resources"]["memory_usage"] == 45.0


# ---------------------------------------------------------------------------
# Container proxy endpoints
# ---------------------------------------------------------------------------


class TestUnhealthyContainers:
    async def test_proxy_success(self, client):
        backend_resp = Response(
            content=json.dumps({"count": 2, "unhealthy_containers": ["c1", "c2"]}).encode(),
            status_code=200,
            media_type="application/json",
        )
        with patch("routers.health._proxy_to_containers", return_value=backend_resp):
            resp = await client.get("/api/health/containers/unhealthy")
            assert resp.status_code == 200

    async def test_fallback_on_502(self, client):
        with patch(
            "routers.health._proxy_to_containers",
            side_effect=HTTPException(status_code=502, detail="unreachable"),
        ):
            resp = await client.get("/api/health/containers/unhealthy")
            assert resp.status_code == 200
            data = resp.json()
            assert data["count"] == 0
            assert data["unhealthy_containers"] == []

    async def test_non_200_falls_back(self, client):
        backend_resp = Response(content=b"error", status_code=500, media_type="text/plain")
        with patch("routers.health._proxy_to_containers", return_value=backend_resp):
            resp = await client.get("/api/health/containers/unhealthy")
            assert resp.status_code == 200
            assert resp.json()["count"] == 0


class TestContainerHealth:
    async def test_success_returns_backend_response(self, client):
        backend_resp = Response(
            content=json.dumps({"container_name": "test", "status": "healthy"}).encode(),
            status_code=200,
            media_type="application/json",
        )
        with patch("routers.health._proxy_to_containers", return_value=backend_resp):
            resp = await client.get("/api/health/container/test")
            assert resp.status_code == 200

    async def test_unreachable_returns_503(self, client):
        """Unreachable is "I could not look" (503), never "it does not exist"."""
        with patch(
            "routers.health._proxy_to_containers",
            side_effect=HTTPException(status_code=502, detail="unreachable"),
        ):
            resp = await client.get("/api/health/container/missing")
            assert resp.status_code == 503
            data = resp.json()
            assert data["error_code"] == "service_unavailable"
            assert data["status"] == "error"

    async def test_backend_500_returns_502_with_error_body(self, client):
        backend_resp = Response(content=b"error", status_code=500, media_type="text/plain")
        with patch("routers.health._proxy_to_containers", return_value=backend_resp):
            resp = await client.get("/api/health/container/test")
            assert resp.status_code == 502
            data = resp.json()
            assert data["error_code"] == "bad_gateway"


class TestContainerHistoryRemoved:
    async def test_history_route_is_not_published(self, client):
        """coding-containers has no history route, so the gateway publishes none."""
        resp = await client.get("/api/health/container/test/history")
        assert resp.status_code == 404
        spec = (await client.get("/openapi.json")).json()
        assert "/api/health/container/{name}/history" not in spec["paths"]


class TestContainerReadiness:
    async def test_success(self, client):
        backend_resp = Response(
            content=b'{"ready": true}', status_code=200, media_type="application/json"
        )
        with patch("routers.health._proxy_to_containers", return_value=backend_resp):
            resp = await client.get("/api/health/container/test/ready")
            assert resp.status_code == 200

    async def test_unreachable_returns_503(self, client):
        with patch(
            "routers.health._proxy_to_containers",
            side_effect=HTTPException(status_code=502, detail="unreachable"),
        ):
            resp = await client.get("/api/health/container/test/ready")
            assert resp.status_code == 503

    async def test_404_becomes_503(self, client):
        backend_resp = Response(
            content=b'{"error": "not found"}', status_code=404, media_type="application/json"
        )
        with patch("routers.health._proxy_to_containers", return_value=backend_resp):
            resp = await client.get("/api/health/container/test/ready")
            assert resp.status_code == 503

    async def test_502_becomes_503(self, client):
        backend_resp = Response(
            content=b'{"error": "bad gw"}', status_code=502, media_type="application/json"
        )
        with patch("routers.health._proxy_to_containers", return_value=backend_resp):
            resp = await client.get("/api/health/container/test/ready")
            assert resp.status_code == 503


class TestContainerLiveness:
    async def test_success(self, client):
        backend_resp = Response(
            content=b'{"alive": true}', status_code=200, media_type="application/json"
        )
        with patch("routers.health._proxy_to_containers", return_value=backend_resp):
            resp = await client.get("/api/health/container/test/live")
            assert resp.status_code == 200

    async def test_unreachable_returns_503(self, client):
        with patch(
            "routers.health._proxy_to_containers",
            side_effect=HTTPException(status_code=502, detail="unreachable"),
        ):
            resp = await client.get("/api/health/container/test/live")
            assert resp.status_code == 503
            assert resp.json()["error_code"] == "service_unavailable"

    async def test_backend_500_wraps_as_502(self, client):
        backend_resp = Response(
            content=b'{"error": "fail"}', status_code=500, media_type="application/json"
        )
        with patch("routers.health._proxy_to_containers", return_value=backend_resp):
            resp = await client.get("/api/health/container/test/live")
            assert resp.status_code == 502
            data = resp.json()
            assert data["status"] == "error"
            assert data["error_code"] == "bad_gateway"


class TestContainerStatistics:
    async def test_proxies_to_backend(self, client):
        backend_resp = Response(
            content=b'{"stats": {}}', status_code=200, media_type="application/json"
        )
        with patch("routers.health._proxy_to_containers", return_value=backend_resp):
            resp = await client.get("/api/health/container/test/statistics")
            assert resp.status_code == 200


class TestBatchContainerHealth:
    async def test_success(self, client):
        backend_resp = Response(
            content=json.dumps(
                {
                    "status": "success",
                    "containers": [],
                    "total_requested": 2,
                    "total_found": 2,
                    "total_not_found": 0,
                }
            ).encode(),
            status_code=200,
            media_type="application/json",
        )
        with patch("routers.health._proxy_to_containers", return_value=backend_resp):
            resp = await client.post(
                "/api/health/containers/batch",
                json={"container_names": ["c1", "c2"]},
            )
            assert resp.status_code == 200

    async def test_fallback_on_unreachable(self, client):
        with patch(
            "routers.health._proxy_to_containers",
            side_effect=HTTPException(status_code=502, detail="unreachable"),
        ):
            resp = await client.post(
                "/api/health/containers/batch",
                json={"container_names": ["c1", "c2"]},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["total_requested"] == 2
            assert data["total_found"] == 0
            assert data["total_not_found"] == 2
            assert len(data["containers"]) == 2
            # Each container marked as not_found
            for c in data["containers"]:
                assert c["status"] == "not_found"
                assert c["healthy"] is False

    async def test_no_body_returns_empty(self, client):
        with patch(
            "routers.health._proxy_to_containers",
            side_effect=HTTPException(status_code=502, detail="unreachable"),
        ):
            resp = await client.post("/api/health/containers/batch")
            assert resp.status_code == 200
            assert resp.json()["total_requested"] == 0

    async def test_non_200_falls_back(self, client):
        backend_resp = Response(content=b"error", status_code=500, media_type="text/plain")
        with patch("routers.health._proxy_to_containers", return_value=backend_resp):
            resp = await client.post(
                "/api/health/containers/batch",
                json={"container_names": ["c1"]},
            )
            assert resp.status_code == 200
            assert resp.json()["total_not_found"] == 1


# ---------------------------------------------------------------------------
# _proxy_to_containers helper
# ---------------------------------------------------------------------------


class TestProxyToContainers:
    async def test_connect_error_raises_502(self):
        mock_client = AsyncMock()
        mock_client.request = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("routers.health.httpx.AsyncClient", return_value=mock_client):
            from routers.health import _proxy_to_containers

            with pytest.raises(HTTPException) as exc_info:
                await _proxy_to_containers("GET", "/api/health/test", tenant_id="t-1")
            assert exc_info.value.status_code == 502
            assert "unreachable" in str(exc_info.value.detail)

    async def test_timeout_raises_504(self):
        mock_client = AsyncMock()
        mock_client.request = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("routers.health.httpx.AsyncClient", return_value=mock_client):
            from routers.health import _proxy_to_containers

            with pytest.raises(HTTPException) as exc_info:
                await _proxy_to_containers("GET", "/api/health/test", tenant_id="t-1")
            assert exc_info.value.status_code == 504
            assert "timed out" in str(exc_info.value.detail)

    async def test_success_returns_response(self):
        mock_response = MagicMock()
        mock_response.content = b'{"ok": true}'
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("routers.health.httpx.AsyncClient", return_value=mock_client):
            from routers.health import _proxy_to_containers

            result = await _proxy_to_containers("GET", "/api/health/test", tenant_id="t-1")
            assert result.status_code == 200
            assert result.body == b'{"ok": true}'
