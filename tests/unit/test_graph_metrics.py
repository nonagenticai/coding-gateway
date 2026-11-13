"""Tests for metrics router -- graph summary, entity metrics, centrality, dashboard.

Sends real HTTP requests via the async test client and asserts on actual
response bodies. Service layer is mocked where a real SQLite graph DB
is not available, but every assertion checks concrete output values.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_metrics_service(
    graph_metrics=None,
    function_metrics=None,
    centrality_result=None,
):
    """Return a mock CodeGraphMetricsService with realistic return values."""
    svc = AsyncMock()

    if graph_metrics is None:
        gm = MagicMock()
        gm.node_types = {"function": 10, "class": 5}
        gm.relationship_types = {"calls": 20}
        gm.density = 0.42
        gm.node_count = 15
        gm.edge_count = 20
        graph_metrics = gm

    svc.get_graph_metrics = AsyncMock(return_value=graph_metrics)
    svc.get_function_network_metrics = AsyncMock(
        return_value=function_metrics
        or {
            "degree_centrality": 0.8,
            "betweenness_centrality": 0.3,
            "closeness_centrality": 0.5,
            "pagerank": 0.1,
            "network_importance": 0.7,
            "in_degree": 3,
            "out_degree": 4,
            "total_degree": 7,
        }
    )
    svc.calculate_centrality_metrics = AsyncMock(
        return_value=centrality_result
        or {
            "func_a": {
                "degree_centrality": 0.9,
                "in_degree": 5,
                "out_degree": 3,
                "total_degree": 8,
            },
            "func_b": {
                "degree_centrality": 0.4,
                "in_degree": 2,
                "out_degree": 1,
                "total_degree": 3,
            },
        }
    )
    return svc


# ---------------------------------------------------------------------------
# System metrics root
# ---------------------------------------------------------------------------


class TestMetricsIndex:
    async def test_returns_200_with_system_metrics(self, client):
        """GET /api/health/metrics/ returns system metrics payload."""
        resp = await client.get("/api/health/metrics/")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["message"] == "System metrics retrieved"
        assert "timestamp" in data
        assert "request_id" in data
        assert isinstance(data["request_id"], str) and len(data["request_id"]) > 0
        metrics = data["metrics"]
        assert "system" in metrics
        assert "cpu_count" in metrics["system"]
        assert isinstance(metrics["system"]["cpu_count"], int)
        assert metrics["system"]["cpu_count"] >= 1

    async def test_system_metrics_contain_network_and_process(self, client):
        """System metrics include network and process sections."""
        resp = await client.get("/api/health/metrics/")
        assert resp.status_code == 200
        metrics = resp.json()["metrics"]
        assert "network" in metrics
        assert "process" in metrics
        proc = metrics["process"]
        assert "cpu_percent" in proc
        assert "num_threads" in proc
        assert isinstance(proc["num_threads"], int)

    async def test_trailing_slash_and_no_slash_both_work(self, client):
        """Both /api/health/metrics and /api/health/metrics/ return 200."""
        resp_slash = await client.get("/api/health/metrics/")
        resp_no_slash = await client.get("/api/health/metrics")
        # The no-slash variant is handled by the health router; both should succeed
        assert resp_slash.status_code == 200
        assert resp_no_slash.status_code == 200


# ---------------------------------------------------------------------------
# Graph summary
# ---------------------------------------------------------------------------


class TestGraphSummary:
    async def test_graph_summary_populated(self, client):
        """Graph summary returns entity/relationship counts from the service."""
        svc = _mock_metrics_service()
        with patch("routers.metrics._get_metrics_service", return_value=svc):
            resp = await client.get("/api/health/metrics/graph-summary")

        assert resp.status_code == 200
        data = resp.json()
        assert data["entity_counts"] == {"function": 10, "class": 5}
        assert data["relationship_counts"] == {"calls": 20}
        assert data["graph_density"] == 0.42
        assert data["language_distribution"] == {}
        assert "analysis_timestamp" in data
        assert len(data["analysis_timestamp"]) > 0

    async def test_graph_summary_empty_when_no_service(self, client):
        """Graph summary returns zero defaults when no DB is available."""
        with patch("routers.metrics._get_metrics_service", return_value=None):
            resp = await client.get("/api/health/metrics/graph-summary")

        assert resp.status_code == 200
        data = resp.json()
        assert data["entity_counts"] == {}
        assert data["relationship_counts"] == {}
        assert data["graph_density"] == 0.0

    async def test_graph_summary_service_exception(self, client):
        """Graph summary returns 500 on unexpected error."""
        with patch(
            "routers.metrics._get_metrics_service",
            side_effect=RuntimeError("db locked"),
        ):
            resp = await client.get("/api/health/metrics/graph-summary")

        assert resp.status_code == 500
        body = resp.json()
        assert "detail" in body
        assert "graph summary" in body["detail"].lower()


# ---------------------------------------------------------------------------
# Entity metrics
# ---------------------------------------------------------------------------


class TestEntityMetrics:
    async def test_entity_metrics_returns_all_fields(self, client):
        """Entity metrics response contains all centrality fields."""
        svc = _mock_metrics_service()
        with patch("routers.metrics._get_metrics_service", return_value=svc):
            resp = await client.get("/api/health/metrics/entity/my_func/metrics")

        assert resp.status_code == 200
        data = resp.json()
        assert data["entity_name"] == "my_func"
        assert data["degree_centrality"] == 0.8
        assert data["betweenness_centrality"] == 0.3
        assert data["closeness_centrality"] == 0.5
        assert data["pagerank"] == 0.1
        assert data["network_importance"] == 0.7
        assert data["in_degree"] == 3
        assert data["out_degree"] == 4
        assert data["total_degree"] == 7

    async def test_entity_metrics_passes_name_to_service(self, client):
        """The entity name from the URL is forwarded to the service."""
        svc = _mock_metrics_service()
        with patch("routers.metrics._get_metrics_service", return_value=svc):
            await client.get("/api/health/metrics/entity/some_func/metrics")

        svc.get_function_network_metrics.assert_awaited_once_with("some_func")

    async def test_entity_metrics_no_service_404(self, client):
        """Entity metrics returns 404 when no graph DB is available."""
        with patch("routers.metrics._get_metrics_service", return_value=None):
            resp = await client.get("/api/health/metrics/entity/missing/metrics")

        assert resp.status_code == 404
        assert "No graph databases" in resp.json()["detail"]

    async def test_entity_metrics_service_error_500(self, client):
        """Entity metrics returns 500 on unexpected service error."""
        svc = AsyncMock()
        svc.get_function_network_metrics = AsyncMock(side_effect=RuntimeError("boom"))
        with patch("routers.metrics._get_metrics_service", return_value=svc):
            resp = await client.get("/api/health/metrics/entity/func1/metrics")

        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# Centrality calculation
# ---------------------------------------------------------------------------


class TestCentrality:
    async def test_centrality_success(self, client):
        """POST centrality returns calculated metrics with entity count."""
        svc = _mock_metrics_service()
        with patch("routers.metrics._get_metrics_service", return_value=svc):
            resp = await client.post("/api/health/metrics/centrality")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["entities_processed"] == 2
        assert "func_a" in data["centrality_metrics"]
        assert "func_b" in data["centrality_metrics"]
        assert data["centrality_metrics"]["func_a"]["degree_centrality"] == 0.9

    async def test_centrality_defaults_to_function_type(self, client):
        """POST centrality without entity_type defaults to 'function'."""
        svc = _mock_metrics_service()
        with patch("routers.metrics._get_metrics_service", return_value=svc):
            await client.post("/api/health/metrics/centrality")

        svc.calculate_centrality_metrics.assert_awaited_with("function")

    async def test_centrality_with_entity_type(self, client):
        """POST centrality with entity_type query param uses that type."""
        svc = _mock_metrics_service()
        with patch("routers.metrics._get_metrics_service", return_value=svc):
            resp = await client.post("/api/health/metrics/centrality?entity_type=class")

        assert resp.status_code == 200
        svc.calculate_centrality_metrics.assert_awaited_with("class")

    async def test_centrality_no_graph_is_typed_404(self, client):
        """POST centrality with no graph is a typed 404, never a 500."""
        with patch("routers.metrics._get_metrics_service", return_value=None):
            resp = await client.post("/api/health/metrics/centrality")

        assert resp.status_code == 404
        assert "no graph" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Entity type metrics
# ---------------------------------------------------------------------------


class TestEntityTypeMetrics:
    async def test_entity_type_metrics_success(self, client):
        """GET entity type metrics returns centrality dict keyed by entity name."""
        svc = _mock_metrics_service()
        with patch("routers.metrics._get_metrics_service", return_value=svc):
            resp = await client.get("/api/health/metrics/entity-type/function/metrics")

        assert resp.status_code == 200
        data = resp.json()
        assert "func_a" in data
        assert data["func_a"]["degree_centrality"] == 0.9
        assert data["func_b"]["in_degree"] == 2

    async def test_entity_type_metrics_no_service_empty(self, client):
        """GET entity type metrics returns empty dict when no DB."""
        with patch("routers.metrics._get_metrics_service", return_value=None):
            resp = await client.get("/api/health/metrics/entity-type/class/metrics")

        assert resp.status_code == 200
        assert resp.json() == {}

    async def test_entity_type_metrics_exception_500(self, client):
        """GET entity type metrics returns 500 on unexpected error."""
        svc = AsyncMock()
        svc.calculate_centrality_metrics = AsyncMock(side_effect=RuntimeError("boom"))
        with patch("routers.metrics._get_metrics_service", return_value=svc):
            resp = await client.get("/api/health/metrics/entity-type/function/metrics")

        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# Top entities
# ---------------------------------------------------------------------------


class TestTopEntities:
    async def test_top_entities_ranked_by_degree(self, client):
        """GET top entities returns ranked list sorted by metric value."""
        svc = _mock_metrics_service()
        with patch("routers.metrics._get_metrics_service", return_value=svc):
            resp = await client.get("/api/health/metrics/top-entities?metric=degree")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        # func_a has degree_centrality 0.9, func_b has 0.4
        assert data[0]["rank"] == 1
        assert data[0]["entity_name"] == "func_a"
        assert data[0]["metric_value"] == 0.9
        assert data[0]["entity_type"] == "function"
        assert data[1]["rank"] == 2
        assert data[1]["entity_name"] == "func_b"
        assert data[1]["metric_value"] == 0.4

    async def test_top_entities_empty_when_no_service(self, client):
        """GET top entities returns empty list when no service."""
        with patch("routers.metrics._get_metrics_service", return_value=None):
            resp = await client.get("/api/health/metrics/top-entities?metric=degree")

        assert resp.status_code == 200
        assert resp.json() == []

    async def test_top_entities_limit(self, client):
        """Limit parameter restricts result size."""
        svc = _mock_metrics_service()
        with patch("routers.metrics._get_metrics_service", return_value=svc):
            resp = await client.get("/api/health/metrics/top-entities?metric=degree&limit=1")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["entity_name"] == "func_a"

    async def test_top_entities_by_in_degree(self, client):
        """Top entities can rank by in_degree metric."""
        svc = _mock_metrics_service()
        with patch("routers.metrics._get_metrics_service", return_value=svc):
            resp = await client.get("/api/health/metrics/top-entities?metric=in_degree")

        assert resp.status_code == 200
        data = resp.json()
        # func_a in_degree=5, func_b in_degree=2
        assert data[0]["entity_name"] == "func_a"
        assert data[0]["metric_value"] == 5

    async def test_top_entities_exception_500(self, client):
        """Top entities returns 500 on unexpected error."""
        svc = AsyncMock()
        svc.calculate_centrality_metrics = AsyncMock(side_effect=RuntimeError("boom"))
        with patch("routers.metrics._get_metrics_service", return_value=svc):
            resp = await client.get("/api/health/metrics/top-entities?metric=degree")

        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# Entity rankings
# ---------------------------------------------------------------------------


class TestEntityRankings:
    async def test_valid_metrics_returns_structure(self, client):
        """GET entity-rankings with valid metrics returns complete structure."""
        resp = await client.get("/api/health/metrics/entity-rankings?metrics=pagerank,degree")
        assert resp.status_code == 200
        data = resp.json()
        assert data["data_available"] is False
        assert "pagerank" in data["rankings"]
        assert "degree" in data["rankings"]
        assert data["summary"]["total_metrics_analyzed"] == 2
        assert data["requested_metrics"] == ["pagerank", "degree"]

    async def test_invalid_metrics_returns_400(self, client):
        """GET entity-rankings with invalid metric returns 400."""
        resp = await client.get("/api/health/metrics/entity-rankings?metrics=invalid_metric")
        assert resp.status_code == 400
        assert "Invalid metrics" in resp.json()["detail"]

    async def test_mixed_valid_invalid_metrics_returns_400(self, client):
        """GET entity-rankings with mix of valid and invalid returns 400."""
        resp = await client.get("/api/health/metrics/entity-rankings?metrics=pagerank,bogus")
        assert resp.status_code == 400

    async def test_single_valid_metric(self, client):
        """GET entity-rankings with a single valid metric succeeds."""
        resp = await client.get("/api/health/metrics/entity-rankings?metrics=closeness")
        assert resp.status_code == 200
        data = resp.json()
        assert "closeness" in data["rankings"]
        assert data["summary"]["entities_per_metric"]["closeness"] == 0


# ---------------------------------------------------------------------------
# Dashboard summary
# ---------------------------------------------------------------------------


class TestDashboardSummary:
    async def test_dashboard_active_with_service(self, client):
        """GET dashboard-summary with service returns active status and graph data."""
        svc = _mock_metrics_service()
        with patch("routers.metrics._get_metrics_service", return_value=svc):
            resp = await client.get("/api/health/metrics/dashboard-summary")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "active"
        overview = data["graph_overview"]
        assert overview["entity_counts"] == {"function": 10, "class": 5}
        assert overview["relationship_counts"] == {"calls": 20}
        assert overview["graph_density"] == 0.42
        assert "analysis_timestamp" in overview
        assert "pagerank" in data["metrics_available"]
        assert data["last_updated"] is not None

    async def test_dashboard_limited_without_service(self, client):
        """GET dashboard-summary returns limited status when service unavailable."""
        with patch("routers.metrics._get_metrics_service", return_value=None):
            resp = await client.get("/api/health/metrics/dashboard-summary")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "limited"
        assert "error" in data["graph_overview"]

    async def test_dashboard_exception_500(self, client):
        """Dashboard returns 500 on unexpected error."""
        svc = AsyncMock()
        svc.get_graph_metrics = AsyncMock(side_effect=RuntimeError("boom"))
        with patch("routers.metrics._get_metrics_service", return_value=svc):
            resp = await client.get("/api/health/metrics/dashboard-summary")

        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# Compare entity types
# ---------------------------------------------------------------------------


class TestCompareEntityTypes:
    async def test_compare_success_with_explicit_types(self, client):
        """GET metrics-comparison with explicit entity_types returns per-type results."""
        svc = _mock_metrics_service()
        with patch("routers.metrics._get_metrics_service", return_value=svc):
            resp = await client.get(
                "/api/health/metrics/metrics-comparison?entity_types=function,class&metric=degree"
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "function" in data
        assert "class" in data
        # Each type should have ranked entities
        assert len(data["function"]) == 2
        assert data["function"][0]["rank"] == 1

    async def test_compare_defaults_to_function_and_class(self, client):
        """GET metrics-comparison without entity_types defaults to function,class."""
        svc = _mock_metrics_service()
        with patch("routers.metrics._get_metrics_service", return_value=svc):
            resp = await client.get("/api/health/metrics/metrics-comparison")

        assert resp.status_code == 200
        data = resp.json()
        assert "function" in data
        assert "class" in data

    async def test_compare_no_service_empty(self, client):
        """GET metrics-comparison returns empty dict when no service."""
        with patch("routers.metrics._get_metrics_service", return_value=None):
            resp = await client.get("/api/health/metrics/metrics-comparison")

        assert resp.status_code == 200
        assert resp.json() == {}


# ---------------------------------------------------------------------------
# Performance endpoints (stubs)
# ---------------------------------------------------------------------------


class TestPerformanceStubs:
    async def test_multi_language_performance_empty_is_404(self, client):
        """No performance data is a typed 404, like the per-language sibling."""
        resp = await client.get("/api/health/metrics/multi-language/performance")
        assert resp.status_code == 404
        assert "No performance data" in resp.json()["detail"]

    async def test_language_stats_404(self, client):
        """Language stats stub raises 404 mentioning the language."""
        resp = await client.get("/api/health/metrics/multi-language/languages/python/stats")
        assert resp.status_code == 404
        assert "python" in resp.json()["detail"]

    async def test_parser_performance_empty(self, client):
        """Parser performance returns empty rankings and summary."""
        resp = await client.get("/api/health/metrics/multi-language/parser-performance")
        assert resp.status_code == 200
        data = resp.json()
        assert data["parser_rankings"] == []
        assert data["summary"]["total_parsers"] == 0
        assert data["summary"]["best_performer"] is None

    async def test_top_languages_empty(self, client):
        """Top languages returns empty list."""
        resp = await client.get("/api/health/metrics/multi-language/top-languages")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_export_metrics_success_structure(self, client):
        """Export metrics returns success with expected structure."""
        resp = await client.post("/api/health/metrics/multi-language/export")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert "request_id" in data
        assert "timestamp" in data
        assert "data" in data
        assert "parser_performance" in data["data"]

    async def test_reset_metrics_success(self, client):
        """Reset metrics returns success with confirmation message."""
        resp = await client.post("/api/health/metrics/multi-language/reset")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert "reset" in data["message"].lower()
        assert "request_id" in data


# ---------------------------------------------------------------------------
# Container-based metrics
# ---------------------------------------------------------------------------


class TestContainerMetrics:
    async def test_container_entity_metrics_success(self, client):
        """Container entity metrics returns combined container+entity data."""
        svc = _mock_metrics_service()
        with patch("routers.metrics._get_metrics_service", return_value=svc):
            resp = await client.get(
                "/api/health/metrics/container/my-container/entity/my_func/metrics"
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["container_name"] == "my-container"
        assert data["entity_name"] == "my_func"
        assert data["degree_centrality"] == 0.8
        assert data["in_degree"] == 3

    async def test_container_entity_metrics_not_found(self, client):
        """Container entity metrics returns 404 when container missing."""
        with patch("routers.metrics._get_metrics_service", return_value=None):
            resp = await client.get("/api/health/metrics/container/missing/entity/func/metrics")

        assert resp.status_code == 404
        assert "missing" in resp.json()["detail"]

    async def test_container_overview_success(self, client):
        """Container overview returns graph statistics from service."""
        svc = _mock_metrics_service()
        with patch("routers.metrics._get_metrics_service", return_value=svc):
            resp = await client.get("/api/health/metrics/container/my-container/overview")

        assert resp.status_code == 200
        data = resp.json()
        assert data["container_name"] == "my-container"
        stats = data["graph_statistics"]
        assert stats["total_entities"] == 15
        assert stats["total_relationships"] == 20
        assert stats["density"] == 0.42
        assert stats["node_types"] == {"function": 10, "class": 5}

    async def test_container_overview_not_found(self, client):
        """Container overview returns 404 when no service."""
        with patch("routers.metrics._get_metrics_service", return_value=None):
            resp = await client.get("/api/health/metrics/container/missing/overview")

        assert resp.status_code == 404

    async def test_container_health_metrics(self, client):
        """Container health returns healthy status with entity count."""
        svc = _mock_metrics_service()
        with patch("routers.metrics._get_metrics_service", return_value=svc):
            resp = await client.get("/api/health/metrics/container/my-container/health")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["graph_loaded"] is True
        assert data["entity_count"] == 15
        assert data["container_name"] == "my-container"
        assert data["processing_capabilities"]["centrality_calculation"] is True

    async def test_container_health_not_found(self, client):
        """Container health returns 404 when no service."""
        with patch("routers.metrics._get_metrics_service", return_value=None):
            resp = await client.get("/api/health/metrics/container/missing/health")

        assert resp.status_code == 404

    async def test_container_refresh_with_service(self, client):
        """Container refresh processes entities and returns completion status."""
        svc = _mock_metrics_service()
        with patch("routers.metrics._get_metrics_service", return_value=svc):
            resp = await client.post("/api/health/metrics/container/my-container/refresh")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "completed"
        assert data["entities_processed"] == 2
        assert data["container_name"] == "my-container"
        assert "degree_centrality" in data["metrics_updated"]
        assert "refresh_timestamp" in data

    async def test_container_refresh_no_service(self, client):
        """Container refresh with no service returns 0 entities processed."""
        with patch("routers.metrics._get_metrics_service", return_value=None):
            resp = await client.post("/api/health/metrics/container/missing/refresh")

        assert resp.status_code == 200
        data = resp.json()
        assert data["entities_processed"] == 0
        assert data["status"] == "completed"
        assert data["metrics_updated"] == []

    async def test_containers_comparison(self, client):
        """Containers comparison returns default single-container comparison."""
        resp = await client.get("/api/health/metrics/containers/comparison")
        assert resp.status_code == 200
        data = resp.json()
        assert data["containers_analyzed"] == ["default"]
        assert "default" in data["comparative_metrics"]
        assert data["summary_statistics"]["total_containers"] == 1
        assert len(data["recommendations"]) >= 1
