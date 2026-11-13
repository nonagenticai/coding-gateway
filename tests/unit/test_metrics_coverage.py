"""Coverage tests for metrics router internals.

Targets: _get_metrics_service (coding-analysis proxy), _resolve_sort_key,
metrics_index with psutil, and exception paths in entity/container endpoints.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx


def _make_proxy_client(status_code: int):
    """Fake proxy client whose .get() returns a response with *status_code*."""
    req = httpx.Request("GET", "http://coding-analysis/api/analysis/graphs/x")
    resp = httpx.Response(status_code, json={"detail": "stub"}, request=req)
    fake_client = AsyncMock()
    fake_client.get = AsyncMock(return_value=resp)
    return fake_client


# ---------------------------------------------------------------------------
# _get_metrics_service — graphs now live in coding-analysis (HTTP)
# ---------------------------------------------------------------------------


class TestGetMetricsService:
    async def test_returns_none_when_no_container(self):
        """No container_name → None (no ambient first-available graph anymore)."""
        from routers.metrics import _get_metrics_service

        result = await _get_metrics_service()
        assert result is None

    async def test_returns_none_when_container_not_found(self):
        """coding-analysis 404 for the container → None."""
        from routers.metrics import _get_metrics_service

        fake_client = _make_proxy_client(404)
        with patch("routers.metrics.get_proxy_client", AsyncMock(return_value=fake_client)):
            result = await _get_metrics_service(container_name="nonexistent")
            assert result is None

    async def test_returns_proxy_when_container_exists(self):
        """coding-analysis 200 for the container → an _AnalysisMetricsProxy."""
        from routers.metrics import _AnalysisMetricsProxy, _get_metrics_service

        fake_client = _make_proxy_client(200)
        with patch("routers.metrics.get_proxy_client", AsyncMock(return_value=fake_client)):
            result = await _get_metrics_service(container_name="mycontainer")
            assert isinstance(result, _AnalysisMetricsProxy)

    async def test_returns_none_on_proxy_exception(self):
        """When the coding-analysis call raises, service returns None."""
        from routers.metrics import _get_metrics_service

        fake_client = AsyncMock()
        fake_client.get = AsyncMock(side_effect=RuntimeError("connection refused"))
        with patch("routers.metrics.get_proxy_client", AsyncMock(return_value=fake_client)):
            result = await _get_metrics_service(container_name="mycontainer")
            assert result is None


# ---------------------------------------------------------------------------
# _resolve_sort_key
# ---------------------------------------------------------------------------


class TestResolveSortKey:
    def test_known_keys_map_correctly(self):
        from routers.metrics import _resolve_sort_key

        assert _resolve_sort_key("degree") == "degree_centrality"
        assert _resolve_sort_key("degree_centrality") == "degree_centrality"
        assert _resolve_sort_key("in_degree") == "in_degree"
        assert _resolve_sort_key("out_degree") == "out_degree"
        assert _resolve_sort_key("total_degree") == "total_degree"

    def test_betweenness_closeness_importance_pagerank_fallback(self):
        """These metrics fall back to degree_centrality in the sort key map."""
        from routers.metrics import _resolve_sort_key

        assert _resolve_sort_key("betweenness") == "degree_centrality"
        assert _resolve_sort_key("closeness") == "degree_centrality"
        assert _resolve_sort_key("importance") == "degree_centrality"
        assert _resolve_sort_key("pagerank") == "degree_centrality"

    def test_unknown_key_defaults_to_degree_centrality(self):
        from routers.metrics import _resolve_sort_key

        assert _resolve_sort_key("unknown_metric") == "degree_centrality"
        assert _resolve_sort_key("") == "degree_centrality"


# ---------------------------------------------------------------------------
# metrics_index with psutil
# ---------------------------------------------------------------------------


class TestMetricsIndexWithPsutil:
    async def test_metrics_index_includes_memory_info(self, client):
        """System metrics include memory details from psutil."""
        resp = await client.get("/api/health/metrics/")
        assert resp.status_code == 200
        data = resp.json()
        system = data["metrics"]["system"]
        # psutil is available in the test environment
        assert "memory" in system
        assert "platform" in system
        assert "python_version" in system


# ---------------------------------------------------------------------------
# Entity rankings exception path -- the old test was broken (pass in with)
# ---------------------------------------------------------------------------


class TestEntityRankingsExceptions:
    async def test_entity_rankings_with_all_valid_metrics(self, client):
        """All five valid metrics are accepted."""
        resp = await client.get(
            "/api/health/metrics/entity-rankings?metrics=pagerank,betweenness,degree,closeness,importance"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["rankings"]) == 5
        assert data["data_available"] is False

    async def test_entity_rankings_metric_names_lowercased(self, client):
        """Metric names are lowercased before validation."""
        resp = await client.get("/api/health/metrics/entity-rankings?metrics=PageRank,DEGREE")
        assert resp.status_code == 200
        data = resp.json()
        assert "pagerank" in data["rankings"]
        assert "degree" in data["rankings"]


# ---------------------------------------------------------------------------
# Container endpoint exceptions
# ---------------------------------------------------------------------------


class TestContainerMetricsExceptions:
    async def test_container_entity_metrics_exception(self, client):
        """Container entity metrics returns 500 on unexpected service error."""
        svc = AsyncMock()
        svc.get_function_network_metrics = AsyncMock(side_effect=RuntimeError("boom"))
        with patch("routers.metrics._get_metrics_service", return_value=svc):
            resp = await client.get("/api/health/metrics/container/c1/entity/func1/metrics")
            assert resp.status_code == 500
            data = resp.json()
            assert "detail" in data
            assert "metrics" in data["detail"].lower()

    async def test_container_overview_exception(self, client):
        """Container overview returns 500 on unexpected error."""
        svc = AsyncMock()
        svc.get_graph_metrics = AsyncMock(side_effect=RuntimeError("boom"))
        with patch("routers.metrics._get_metrics_service", return_value=svc):
            resp = await client.get("/api/health/metrics/container/c1/overview")
            assert resp.status_code == 500
            data = resp.json()
            assert "detail" in data
            assert "overview" in data["detail"].lower()

    async def test_container_health_exception(self, client):
        """Container health returns 500 on unexpected error."""
        svc = AsyncMock()
        svc.get_graph_metrics = AsyncMock(side_effect=RuntimeError("boom"))
        with patch("routers.metrics._get_metrics_service", return_value=svc):
            resp = await client.get("/api/health/metrics/container/c1/health")
            assert resp.status_code == 500
            data = resp.json()
            assert "detail" in data
            assert "health" in data["detail"].lower()

    async def test_container_refresh_exception(self, client):
        """Container refresh returns 500 on unexpected error."""
        svc = AsyncMock()
        svc.calculate_centrality_metrics = AsyncMock(side_effect=RuntimeError("boom"))
        with patch("routers.metrics._get_metrics_service", return_value=svc):
            resp = await client.post("/api/health/metrics/container/c1/refresh")
            assert resp.status_code == 500
            data = resp.json()
            assert "detail" in data
            assert "metrics" in data["detail"].lower()
