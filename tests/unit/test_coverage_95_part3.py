"""Additional tests to push total coverage past 95%.

Part 3: Targeting remaining biggest gaps:
- routers/graphs.py (85%): initialize/delete endpoints, _get_graph_counts, pagination
- routers/proxy.py (73%): auth forwarding branches (lines 271-285)
- services/code_graph/multi_language_graph_service.py (79%): analyze_file with processor,
  analyze_directory, _detect_file_language, search_entities, cross_language
- services/cache_manager.py (86%): get/set/delete/clear operations
- services/validation.py (86%): deeper import validation branches
- routers/metrics.py (86%): container/centrality endpoints
- services/code_graph/consistency_checker.py (88%): checker facade methods
- routers/validation.py (87%): more endpoint coverage
- routers/validation_status.py (90%): status endpoint branches
- services/code_graph/structure_storage.py (86%): deeper methods
- services/job_manager.py (89%): remaining uncovered methods
"""

from __future__ import annotations

import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# =====================================================================
# routers/graphs.py — more endpoint paths
# =====================================================================


class TestGraphsEndpointsDeeper:
    """Cover more graphs.py endpoint internals with real SQLite databases."""

    async def test_graphs_nonexistent_container_returns_404(self, client):
        """GET /api/graphs/{name} for nonexistent container returns 404.

        The autouse ``_stub_analysis_get_404`` fixture covers the "no
        upstream graph" case, so this test just exercises the handler.
        """
        resp = await client.get("/api/graphs/nonexistent-container")
        assert resp.status_code == 404

    async def test_graphs_list_returns_empty_for_empty_dir(self, client):
        """GET /api/graphs returns empty list when no databases exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("routers.graphs.get_settings") as mock_settings:
                mock_s = MagicMock()
                mock_s.shared_graphs_path = tmpdir
                mock_settings.return_value = mock_s

                resp = await client.get("/api/graphs")
                assert resp.status_code == 200
                data = resp.json()
                assert data["status"] == "success"
                assert data["graphs"] == []
                assert data["total"] == 0

    async def test_graphs_nodes_for_nonexistent_db_returns_404(self, client):
        """GET /api/graphs/{name}/nodes returns 404 for missing database.

        Autouse 404 stub covers "no upstream graph"; handler should
        surface it verbatim.
        """
        resp = await client.get("/api/graphs/nonexistent/nodes")
        assert resp.status_code == 404

    async def test_initialize_existing_graph_conflict(self, client):
        """POST /api/graphs/initialize with existing graph returns 409."""
        from unittest.mock import AsyncMock

        import httpx

        req = httpx.Request("POST", "http://coding-analysis/api/analysis/graphs/existing-container")
        with patch(
            "routers.graphs._analysis_post",
            new=AsyncMock(return_value=httpx.Response(409, json={}, request=req)),
        ):
            resp = await client.post(
                "/api/graphs/initialize",
                json={"container_name": "existing-container", "force_rebuild": False},
            )
        assert resp.status_code == 409
        data = resp.json()
        assert "already exists" in data["detail"]

    async def test_delete_graph_with_existing_db(self, client):
        """DELETE /api/graphs/{name} with an upstream graph should succeed."""
        from unittest.mock import AsyncMock

        import httpx

        req = httpx.Request("DELETE", "http://coding-analysis/api/analysis/graphs/deletable")
        upstream = httpx.Response(
            200,
            json={
                "container_name": "deletable",
                "deleted": True,
                "nodes_deleted": 1,
                "edges_deleted": 0,
            },
            request=req,
        )
        with patch("routers.graphs._analysis_delete", new=AsyncMock(return_value=upstream)):
            resp = await client.delete("/api/graphs/deletable")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["deleted_nodes"] == 1

    async def test_get_graph_detail_with_existing_db(self, client):
        """GET /api/graphs/{name} with an upstream graph returns its metadata.

        Counts now come from coding-analysis over HTTP; mock the
        ``_analysis_get`` helper with a 200 metadata response.
        """
        import httpx

        req = httpx.Request("GET", "http://coding-analysis/api/analysis/graphs/detail-test")
        upstream = httpx.Response(
            200,
            json={
                "container_name": "detail-test",
                "exists": True,
                "node_count": 1,
                "edge_count": 0,
            },
            request=req,
        )

        async def _fake(*args, **kwargs):
            return upstream

        with patch("routers.graphs._analysis_get", new=_fake):
            resp = await client.get("/api/graphs/detail-test")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["graph"]["container_name"] == "detail-test"
        assert data["graph"]["node_count"] == 1
        assert data["graph"]["edge_count"] == 0


# =====================================================================
# services/cache_manager.py — deeper coverage with mock Redis
# =====================================================================


class TestCacheManagerDeeper:
    """Cover RedisCacheManager get/set/delete with properly mocked async Redis."""

    def _make_manager_with_redis(self):
        from services.cache_manager import RedisCacheManager

        mgr = RedisCacheManager()
        mock_redis = MagicMock()
        mock_redis.async_get = AsyncMock()
        mock_redis.async_set = AsyncMock()
        mock_redis.async_delete = AsyncMock()
        mgr._redis = mock_redis
        return mgr, mock_redis

    async def test_get_returns_cached_value_and_increments_hits(self):
        mgr, mock_redis = self._make_manager_with_redis()
        mock_redis.async_get = AsyncMock(return_value={"key": "value"})

        result = await mgr.get("test-key")
        assert result == {"key": "value"}
        assert mgr.stats["hits"] == 1
        mock_redis.async_get.assert_called_once_with("test-key")

    async def test_get_returns_none_for_missing_key_and_increments_misses(self):
        mgr, mock_redis = self._make_manager_with_redis()
        mock_redis.async_get = AsyncMock(return_value=None)

        result = await mgr.get("missing-key")
        assert result is None
        assert mgr.stats["misses"] == 1

    async def test_set_stores_value_and_returns_true(self):
        mgr, mock_redis = self._make_manager_with_redis()
        mock_redis.async_set = AsyncMock(return_value=True)

        result = await mgr.set("test-key", {"data": "value"})
        assert result is True
        mock_redis.async_set.assert_called_once()

    async def test_delete_returns_true_when_key_existed(self):
        mgr, mock_redis = self._make_manager_with_redis()
        mock_redis.async_delete = AsyncMock(return_value=1)

        result = await mgr.delete("test-key")
        assert result is True
        mock_redis.async_delete.assert_called_once_with("test-key")


# =====================================================================
# services/validation.py — FunctionValidationService & deeper
# =====================================================================


class TestValidationServiceFunctions:
    """Cover FunctionValidationService and remaining validation branches."""

    async def test_import_validation_with_third_party(self):
        from services.validation import ImportValidationService

        svc = ImportValidationService()
        result = await svc.validate_imports(
            "test-graph",
            [
                {"module": "requests", "type": "import"},  # Common third-party
                {"module": "my.local.module", "type": "from_import"},
            ],
        )
        # requests may be valid or invalid depending on check
        assert len(result.valid_imports) + len(result.invalid_imports) >= 1

    def test_extract_import_info_import(self):
        import ast

        from services.validation import ASTValidationService

        svc = ASTValidationService()
        node = ast.parse("import os").body[0]
        imports = svc._extract_import_info(node)
        assert len(imports) == 1
        assert imports[0]["module"] == "os"
        assert imports[0]["type"] == "import"

    def test_extract_import_info_from_import(self):
        import ast

        from services.validation import ASTValidationService

        svc = ASTValidationService()
        node = ast.parse("from os.path import join").body[0]
        imports = svc._extract_import_info(node)
        assert len(imports) == 1
        assert imports[0]["module"] == "os.path.join"
        assert imports[0]["type"] == "from_import"

    def test_extract_variable_info(self):
        import ast

        from services.validation import ASTValidationService

        svc = ASTValidationService()
        node = ast.parse("x = 42").body[0]
        variables = svc._extract_variable_info(node)
        assert len(variables) == 1
        assert variables[0]["name"] == "x"

    def test_calculate_complexity_score(self):
        import ast

        from services.validation import ASTValidationService

        svc = ASTValidationService()
        code = """
if True:
    for x in range(10):
        while True:
            break
    try:
        pass
    except:
        pass
"""
        tree = ast.parse(code)
        score = svc._calculate_complexity_score(tree)
        assert score > 0
        assert score <= 10.0


# =====================================================================
# routers/validation.py — more endpoint branches
# =====================================================================


class TestValidationRouterBranches:
    """Cover validation router endpoints with real assertions on response bodies."""

    async def test_validate_function_reference_without_codebase_refuses(self, client):
        """No codebase named -> 400, not a 200 carrying a manufactured verdict."""
        resp = await client.post(
            "/api/validation/function-reference",
            json={"function_name": "test_func"},
        )
        assert resp.status_code == 400
        assert "valid" not in resp.json()

    async def test_validate_class_reference_without_codebase_refuses(self, client):
        """No codebase named -> 400, not a 200 carrying a manufactured verdict."""
        resp = await client.post(
            "/api/validation/class-reference",
            json={"class_name": "TestClass"},
        )
        assert resp.status_code == 400
        assert "valid" not in resp.json()

    async def test_validate_function_consistency_returns_result(self, client):
        """POST /api/validation/function-consistency returns consistency check."""
        resp = await client.post(
            "/api/validation/function-consistency",
            json={
                "function_code": "def hello():\n    pass",
                "description": "A hello function",
                "context": "test",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "consistent" in data
        assert "confidence" in data
        assert isinstance(data["confidence"], (int, float))

    async def test_validate_class_consistency_returns_result(self, client):
        """POST /api/validation/class-consistency returns consistency check."""
        resp = await client.post(
            "/api/validation/class-consistency",
            json={
                "class_code": "class MyClass:\n    pass",
                "description": "A test class",
                "context": "test",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "consistent" in data
        assert "confidence" in data

    async def test_validate_code_refuses_when_no_workspace_is_reachable(self, client):
        """Must not report ``valid is True`` for a file it never opened.

        conftest's CONTAINERS_URL (http://coding-containers:8691) does not
        resolve from the test runner, so the workspace read fails at the
        transport and the honest answer is "I could not look" (503), not
        "I looked and it was fine".
        """
        resp = await client.post(
            "/api/validation/code",
            json={
                "container_name": "test",
                "file_path": "test.py",
                "validation_type": "syntax",
            },
        )
        assert resp.status_code == 503
        assert "validation_results" not in resp.json()


# =====================================================================
# routers/validation_status.py — more status branches
# =====================================================================


class TestValidationStatusBranches:
    """Cover validation_status router — nonexistent validation IDs return 404."""

    async def test_get_nonexistent_validation_returns_404(self, client):
        """GET /api/validation/{id} for nonexistent validation returns 404."""
        resp = await client.get("/api/validation/nonexistent-validation-id")
        assert resp.status_code == 404

    async def test_update_nonexistent_validation_returns_404(self, client):
        """POST /api/validation/status with nonexistent ID returns 404."""
        resp = await client.post(
            "/api/validation/status",
            json={
                "validation_id": "does-not-exist",
                "status": "in_progress",
            },
        )
        assert resp.status_code == 404


# =====================================================================
# services/code_graph/consistency_checker.py — facade methods
# =====================================================================


class TestConsistencyCheckerFacade:
    """Cover CodeConsistencyChecker facade methods."""

    def test_check_function_description_returns_consistency_data(self):
        from services.code_graph.consistency_checker import CodeConsistencyChecker

        checker = CodeConsistencyChecker()
        result = checker.check_function_description(
            "def hello():\n    pass",
            "A function that says hello",
            "test context",
        )
        assert "is_consistent" in result
        assert "consistency_score" in result
        assert isinstance(result["consistency_score"], (int, float))
        assert 0.0 <= result["consistency_score"] <= 1.0

    def test_check_class_description_returns_consistency_data(self):
        from services.code_graph.consistency_checker import CodeConsistencyChecker

        checker = CodeConsistencyChecker()
        result = checker.check_class_description(
            "class MyClass:\n    pass",
            "A test class",
            "test context",
        )
        assert "is_consistent" in result
        assert "consistency_score" in result
        assert isinstance(result["consistency_score"], (int, float))


# =====================================================================
# routers/metrics.py — more endpoint coverage
# =====================================================================


class TestMetricsEndpointsDeeper:
    """Cover metrics router endpoints with real database and proper assertions."""

    async def test_entity_metrics_with_no_db_returns_404(self, client):
        """GET /api/health/metrics/entity/{name}/metrics with no DB returns 404."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("routers.metrics.get_settings") as mock_settings:
                mock_s = MagicMock()
                mock_s.shared_graphs_path = tmpdir
                mock_settings.return_value = mock_s

                resp = await client.get("/api/health/metrics/entity/myfunc/metrics")
                assert resp.status_code == 404

    async def test_graph_summary_returns_expected_fields(self, client):
        """GET /api/health/metrics/graph-summary returns proper structure."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("routers.metrics.get_settings") as mock_settings:
                mock_s = MagicMock()
                mock_s.shared_graphs_path = tmpdir
                mock_settings.return_value = mock_s

                resp = await client.get("/api/health/metrics/graph-summary")
                assert resp.status_code == 200
                data = resp.json()
                assert "entity_counts" in data
                assert "graph_density" in data
                assert "analysis_timestamp" in data

    async def test_dashboard_summary_returns_expected_structure(self, client):
        """GET /api/health/metrics/dashboard-summary returns dashboard data."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("routers.metrics.get_settings") as mock_settings:
                mock_s = MagicMock()
                mock_s.shared_graphs_path = tmpdir
                mock_settings.return_value = mock_s

                resp = await client.get("/api/health/metrics/dashboard-summary")
                assert resp.status_code == 200
                data = resp.json()
                assert "graph_overview" in data
                assert "metrics_available" in data
                assert "status" in data

    async def test_containers_comparison_returns_expected_structure(self, client):
        """GET /api/health/metrics/containers/comparison returns comparison data."""
        resp = await client.get("/api/health/metrics/containers/comparison")
        assert resp.status_code == 200
        data = resp.json()
        assert "containers_analyzed" in data
        assert "summary_statistics" in data
        assert data["summary_statistics"]["total_containers"] >= 1


# =====================================================================
# services/job_manager.py — remaining uncovered lines
# =====================================================================


class TestJobManagerRemaining:
    """Cover remaining uncovered lines in job_manager.py."""

    def _make_manager(self):
        from services.job_manager import KubernetesJobManager

        mgr = KubernetesJobManager.__new__(KubernetesJobManager)
        mgr.namespace = "test"
        mgr.MAX_CONCURRENT_JOBS = 5
        mgr.DEFAULT_TTL_SECONDS = 300
        mgr.DEFAULT_BACKOFF_LIMIT = 3
        mgr.DEFAULT_TIMEOUT_SECONDS = 600
        mgr.batch_v1 = MagicMock()
        mgr.core_v1 = MagicMock()
        return mgr

    async def test_get_job_logs_no_pods(self):
        mgr = self._make_manager()
        mock_pods = MagicMock()
        mock_pods.items = []
        mgr.core_v1.list_namespaced_pod = MagicMock(return_value=mock_pods)

        logs = await mgr.get_job_logs("test-job", tenant_id="n8n-dev")
        assert logs == "[No logs available]"

    async def test_delete_job_api_error(self):
        from kubernetes.client.rest import ApiException

        mgr = self._make_manager()
        from services.job_manager import (
            APP_LABEL_KEY,
            APP_LABEL_VALUE,
            TENANT_LABEL_KEY,
            tenant_label_value,
        )

        mgr.batch_v1.read_namespaced_job.return_value.metadata.labels = {
            APP_LABEL_KEY: APP_LABEL_VALUE,
            TENANT_LABEL_KEY: tenant_label_value("n8n-dev"),
        }
        mgr.batch_v1.delete_namespaced_job = MagicMock(
            side_effect=ApiException(status=500, reason="Internal")
        )

        with pytest.raises(ApiException):
            await mgr.delete_job("test-job", tenant_id="n8n-dev")

    async def test_get_active_jobs_api_error(self):
        from kubernetes.client.rest import ApiException

        mgr = self._make_manager()
        mgr.batch_v1.list_namespaced_job = MagicMock(
            side_effect=ApiException(status=500, reason="Internal")
        )

        with pytest.raises(ApiException):
            await mgr.get_active_jobs(tenant_id="n8n-dev")

    async def test_job_exists_api_error(self):
        from kubernetes.client.rest import ApiException

        mgr = self._make_manager()
        mgr.batch_v1.read_namespaced_job = MagicMock(
            side_effect=ApiException(status=500, reason="Internal")
        )

        with pytest.raises(ApiException):
            await mgr.job_exists("test-job")

    async def test_get_completion_time_api_error(self):
        from kubernetes.client.rest import ApiException

        mgr = self._make_manager()
        mgr.batch_v1.read_namespaced_job = MagicMock(
            side_effect=ApiException(status=500, reason="Internal")
        )

        with pytest.raises(ApiException):
            await mgr.get_completion_time("test-job")

    async def test_list_all_jobs_api_error(self):
        from kubernetes.client.rest import ApiException

        mgr = self._make_manager()
        mgr.batch_v1.list_namespaced_job = MagicMock(
            side_effect=ApiException(status=500, reason="Internal")
        )

        with pytest.raises(ApiException):
            await mgr.list_all_jobs()

    async def test_cleanup_old_jobs_api_error(self):
        from kubernetes.client.rest import ApiException

        mgr = self._make_manager()
        mgr.batch_v1.list_namespaced_job = MagicMock(
            side_effect=ApiException(status=500, reason="Internal")
        )

        with pytest.raises(ApiException):
            await mgr.cleanup_old_jobs()

    async def test_get_job_status_api_error_non_404(self):
        from kubernetes.client.rest import ApiException

        mgr = self._make_manager()
        mgr.batch_v1.read_namespaced_job = MagicMock(
            side_effect=ApiException(status=500, reason="Internal")
        )

        with pytest.raises(ApiException):
            await mgr.get_job_status("test-job", tenant_id="n8n-dev")

    async def test_create_pytest_job_api_error(self):
        from kubernetes.client.rest import ApiException

        mgr = self._make_manager()
        mgr.count_all_active_jobs = AsyncMock(return_value=0)
        mgr.batch_v1.create_namespaced_job = MagicMock(
            side_effect=ApiException(status=500, reason="Internal")
        )

        with pytest.raises(ApiException):
            await mgr.create_pytest_job({"pytest_args": ["-v"]}, tenant_id="n8n-dev")
