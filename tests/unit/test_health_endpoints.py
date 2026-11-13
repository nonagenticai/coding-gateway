"""Extended health endpoint tests — edge cases, degraded states, aliases, and metrics."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx

# ---------------------------------------------------------------------------
# Liveness is independent of backends
# ---------------------------------------------------------------------------


class TestLivenessIndependence:
    """Liveness probe must not call any backend — it only checks process health."""

    async def test_liveness_does_not_call_backends(self, client):
        """Patch httpx to explode if called; liveness must still pass."""
        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.side_effect = RuntimeError("should not be called")
            resp = await client.get("/api/health/live")
            assert resp.status_code == 200
            assert resp.json()["health"]["status"] == "healthy"

    async def test_readiness_does_not_call_backends(self, client):
        """Readiness also doesn't call backends (gateway-only check)."""
        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.side_effect = RuntimeError("should not be called")
            resp = await client.get("/api/health/ready")
            assert resp.status_code == 200
            assert resp.json()["health"]["status"] == "healthy"


# ---------------------------------------------------------------------------
# K8s alias paths
# ---------------------------------------------------------------------------


class TestHealthAliases:
    """The router registers /health/ready and /health/live aliases."""

    async def test_health_ready_alias_returns_200(self, client):
        resp = await client.get("/api/health/health/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert data["health"]["status"] == "healthy"

    async def test_health_live_alias_returns_200(self, client):
        resp = await client.get("/api/health/health/live")
        assert resp.status_code == 200
        data = resp.json()
        assert data["health"]["status"] == "healthy"

    async def test_alias_matches_canonical_body(self, client):
        """Alias response should have the same structure as canonical."""
        canonical = await client.get("/api/health/ready")
        alias = await client.get("/api/health/health/ready")
        c_data = canonical.json()
        a_data = alias.json()
        assert set(c_data.keys()) == set(a_data.keys())
        assert c_data["health"]["status"] == a_data["health"]["status"]


# ---------------------------------------------------------------------------
# Health check (main) — degraded scenarios
# ---------------------------------------------------------------------------


class TestHealthCheckDegradedScenarios:
    """Various backend failure modes should all result in 'degraded' status."""

    async def _make_mock_client(self, fake_get):
        mock_client = AsyncMock()
        mock_client.get = fake_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        return mock_client

    async def test_timeout_causes_degraded(self, client):
        async def fake_get(url):
            raise httpx.ReadTimeout("read timed out")

        mock_client = await self._make_mock_client(fake_get)
        with patch("routers.health._health_http_client", mock_client):
            resp = await client.get("/api/health")
            assert resp.status_code == 200
            assert resp.json()["health"]["status"] == "degraded"

    async def test_404_from_backend_causes_degraded(self, client):
        """A backend returning 404 is still >= 400 so counts as unhealthy."""
        mock_response = MagicMock(status_code=404)
        mock_client = await self._make_mock_client(AsyncMock(return_value=mock_response))
        with patch("routers.health._health_http_client", mock_client):
            resp = await client.get("/api/health")
            assert resp.status_code == 200
            assert resp.json()["health"]["status"] == "degraded"

    async def test_mixed_healthy_and_failed_backends(self, client):
        """Only some backends fail — should still degrade."""
        call_count = 0

        async def fake_get(url):
            nonlocal call_count
            call_count += 1
            # Fail every 3rd backend
            return MagicMock(status_code=500 if call_count % 3 == 0 else 200)

        mock_client = await self._make_mock_client(fake_get)
        with patch("routers.health._health_http_client", mock_client):
            resp = await client.get("/api/health")
            assert resp.status_code == 200
            assert resp.json()["health"]["status"] == "degraded"

    async def test_all_healthy_returns_healthy(self, client):
        mock_response = MagicMock(status_code=200)
        mock_client = await self._make_mock_client(AsyncMock(return_value=mock_response))
        with patch("routers.health._health_http_client", mock_client):
            resp = await client.get("/api/health")
            assert resp.status_code == 200
            assert resp.json()["health"]["status"] == "healthy"


# ---------------------------------------------------------------------------
# Timestamp and version fields
# ---------------------------------------------------------------------------


class TestHealthResponseMetadata:
    async def test_timestamp_is_iso_format(self, client):
        mock_response = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("routers.health._health_http_client", mock_client):
            resp = await client.get("/api/health")
            data = resp.json()
            # Top-level timestamp
            assert "T" in data["timestamp"]
            # Nested health timestamp
            assert "T" in data["health"]["timestamp"]

    async def test_version_reflects_env(self, client):
        mock_response = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("routers.health._health_http_client", mock_client):
            resp = await client.get("/api/health")
            data = resp.json()
            assert data["health"]["version"] == "1.0.0"
            assert data["health"]["environment"] == "test"

    async def test_request_id_format(self, client):
        mock_response = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("routers.health._health_http_client", mock_client):
            resp = await client.get("/api/health")
            data = resp.json()
            assert isinstance(data["request_id"], str) and len(data["request_id"]) > 0
            assert len(data["request_id"]) > 4


# ---------------------------------------------------------------------------
# No auth required for health endpoints
# ---------------------------------------------------------------------------


class TestHealthNoAuth:
    """Readiness and liveness must NOT require authentication."""

    async def test_ready_no_auth_header_needed(self, client):
        """No Authorization header — should still return 200."""
        resp = await client.get("/api/health/ready")
        assert resp.status_code == 200

    async def test_live_no_auth_header_needed(self, client):
        resp = await client.get("/api/health/live")
        assert resp.status_code == 200

    async def test_root_health_no_auth_header_needed(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# System metrics endpoint
# ---------------------------------------------------------------------------


class TestSystemMetrics:
    async def test_without_psutil_returns_null_metrics(self, client):
        with patch("routers.health.HAS_PSUTIL", False):
            resp = await client.get("/api/health/system-metrics")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "success"
            assert "psutil not available" in data["message"]
            assert data["cpu_percent"] is None
            assert data["memory_percent"] is None
            assert data["memory_used_mb"] is None
            assert data["memory_total_mb"] is None
            assert data["disk_percent"] is None


# ---------------------------------------------------------------------------
# Health router metrics endpoint (/api/health/metrics)
# ---------------------------------------------------------------------------


class TestHealthRouterMetrics:
    async def test_without_psutil_returns_stub_metrics(self, client):
        with patch("routers.health.HAS_PSUTIL", False):
            resp = await client.get("/api/health/metrics")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "success"
            assert "metrics" in data
            metrics = data["metrics"]
            assert "system" in metrics
            assert "network" in metrics
            assert "process" in metrics
            # System stub has expected keys
            assert metrics["system"]["cpu_percent"] == 0.0
            assert "python_version" in metrics["system"]

    async def test_has_collection_time(self, client):
        with patch("routers.health.HAS_PSUTIL", False):
            resp = await client.get("/api/health/metrics")
            data = resp.json()
            assert "collection_time" in data
            assert "T" in data["collection_time"]

    async def test_has_timestamp_and_request_id(self, client):
        with patch("routers.health.HAS_PSUTIL", False):
            resp = await client.get("/api/health/metrics")
            data = resp.json()
            assert "timestamp" in data
            assert isinstance(data["request_id"], str) and len(data["request_id"]) > 0
