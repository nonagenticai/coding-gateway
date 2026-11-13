"""Additional tests to push coverage from 90% to 95%+.

Part 2: Targeting remaining gaps in:
- routers/proxy.py: auth forwarding lines 271-285, content body > 100MB
- routers/graphs.py: endpoint paths for list, detail, nodes, edges, initialize, delete
- routers/jobs.py: get_job_manager init error, list active jobs
- services/job_manager.py: KubernetesJobManager __init__, get_job_manager singleton
- services/code_graph/multi_language_graph_service.py: analyze_file deeper, factory
- services/cache_manager.py: RedisCacheManager methods
- services/validation.py: deeper import validation, function validation
- routers/validation_status.py: status endpoints
- routers/cache.py: more cache endpoints
- services/code_graph/graph_database_populator.py: remaining uncovered lines
- services/code_graph/graph_validation.py: remaining branches
- services/code_graph/structure_storage.py: more storage methods
"""

from __future__ import annotations

import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# =====================================================================
# routers/proxy.py — auth forwarding, content body too large
# =====================================================================


class TestProxyAuthForwarding:
    """Cover the auth forwarding branch in proxy_request (lines 271-285)."""

    async def test_proxy_body_too_large(self, client):
        """Response body > 100MB returns 502 with BAD_GATEWAY error_code."""
        import httpx

        large_content = b"x" * (101 * 1024 * 1024)  # 101MB
        mock_response = httpx.Response(
            200,
            content=large_content,
            headers={"content-type": "application/octet-stream"},
        )
        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=mock_response)

        with patch("routers.proxy.get_proxy_client", return_value=mock_client):
            resp = await client.get("/api/analysis/huge")

        assert resp.status_code == 502
        data = resp.json()
        assert "error" in data
        assert data["error"]["code"] == "HTTP_502"
        assert "too large" in data["error"]["message"].lower()
        assert "timestamp" in data["error"]
        assert "request_id" in data["error"]


# =====================================================================
# routers/graphs.py — endpoint coverage via HTTP
# =====================================================================


class TestGraphsEndpoints:
    """Test graph router endpoints via HTTP client."""

    async def test_list_graphs(self, client):
        """GET /api/graphs returns graph list with expected structure."""
        resp = await client.get("/api/graphs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert "graphs" in data
        assert isinstance(data["graphs"], list)
        assert "total" in data
        assert "request_id" in data
        assert "timestamp" in data

    async def test_list_graphs_trailing_slash(self, client):
        """GET /api/graphs/ also returns graph list."""
        resp = await client.get("/api/graphs/")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert isinstance(data["graphs"], list)

    async def test_get_graph_detail_not_found(self, client):
        """GET /api/graphs/{name} for nonexistent container returns 404."""
        resp = await client.get("/api/graphs/nonexistent-container")
        assert resp.status_code == 404
        data = resp.json()
        assert "detail" in data
        assert "nonexistent-container" in str(data["detail"])

    async def test_get_graph_nodes_not_found(self, client):
        """GET /api/graphs/{name}/nodes returns 404 for nonexistent container."""
        resp = await client.get("/api/graphs/nonexistent/nodes")
        assert resp.status_code == 404

    async def test_get_graph_edges_not_found(self, client):
        """GET /api/graphs/{name}/edges returns 404 for nonexistent container."""
        resp = await client.get("/api/graphs/nonexistent/edges")
        assert resp.status_code == 404

    async def test_delete_graph_nonexistent(self, client):
        """DELETE /api/graphs/{name} for nonexistent returns 404."""
        resp = await client.delete("/api/graphs/nonexistent-container")
        assert resp.status_code == 404

    async def test_delete_graph_invalid_name(self, client):
        """DELETE /api/graphs/{name} with path traversal returns 404 (path normalized away)."""
        resp = await client.delete("/api/graphs/../../etc/passwd")
        # Path traversal normalized by ASGI/httpx → /etc/passwd → no route match → 404
        assert resp.status_code == 404

    async def test_initialize_graph(self, client):
        """POST /api/graphs/initialize creates a graph (201) or fails (500)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("routers.graphs.get_settings") as mock_settings:
                mock_s = MagicMock()
                mock_s.shared_graphs_path = tmpdir
                mock_settings.return_value = mock_s

                resp = await client.post(
                    "/api/graphs/initialize",
                    json={
                        "container_name": "test-init-container",
                        "force_rebuild": True,
                    },
                )
                # GraphDatabaseConnection stub raises "not implemented", so expect 500
                assert resp.status_code == 500
                data = resp.json()
                assert "detail" in data

    async def test_get_graph_counts_no_db(self):
        """_get_graph_counts returns (-1, -1) when no db found."""
        from routers.graphs import _get_graph_counts

        with tempfile.TemporaryDirectory() as tmpdir:
            mock_settings = MagicMock()
            mock_settings.shared_graphs_path = tmpdir

            counts = await _get_graph_counts("nonexistent", settings=mock_settings)
            assert counts == (-1, -1)

    async def test_get_graph_counts_with_db(self):
        """_get_graph_counts returns counts from coding-analysis over HTTP."""
        import httpx

        from routers.graphs import _get_graph_counts

        req = httpx.Request("GET", "http://coding-analysis/api/analysis/graphs/mycontainer")
        response = httpx.Response(
            200,
            json={
                "container_name": "mycontainer",
                "exists": True,
                "node_count": 2,
                "edge_count": 1,
            },
            request=req,
        )

        async def _fake(*args, **kwargs):
            return response

        with patch("routers.graphs._analysis_get", new=_fake):
            node_count, edge_count = await _get_graph_counts("mycontainer")
            assert node_count == 2
            assert edge_count == 1

    async def test_get_graph_counts_error(self):
        """_get_graph_counts maps unexpected errors to (-1, -1)."""
        from routers.graphs import _get_graph_counts

        async def _boom(*args, **kwargs):
            raise RuntimeError("upstream exploded")

        with patch("routers.graphs._analysis_get", new=_boom):
            counts = await _get_graph_counts("broken")
            assert counts == (-1, -1)

    def test_validate_sqlite_pagination_large_page(self):
        """Large page number raises HTTPException with status 400."""
        from fastapi import HTTPException

        from routers.graphs import _validate_sqlite_pagination

        with pytest.raises(HTTPException) as exc_info:
            _validate_sqlite_pagination(2_000_000, 10)
        assert exc_info.value.status_code == 400
        assert "too large" in str(exc_info.value.detail).lower()


# =====================================================================
# routers/jobs.py — get_job_manager init path
# =====================================================================


class TestJobsRouter:
    """Cover routers/jobs.py get_job_manager init and endpoints."""

    def test_get_job_manager_init_error(self):
        """get_job_manager raises when K8s config loading fails."""
        import routers.jobs as jobs_mod

        original = jobs_mod._job_manager
        jobs_mod._job_manager = None
        try:
            with patch(
                "services.job_manager.config.load_kube_config",
                side_effect=RuntimeError("no kubeconfig"),
            ):
                with pytest.raises(RuntimeError, match="no kubeconfig"):
                    jobs_mod.get_job_manager()
        finally:
            jobs_mod._job_manager = original

    def test_get_job_manager_returns_cached(self):
        """get_job_manager returns cached instance without re-initialization."""
        import routers.jobs as jobs_mod

        original = jobs_mod._job_manager
        mock_mgr = MagicMock()
        jobs_mod._job_manager = mock_mgr
        try:
            result = jobs_mod.get_job_manager()
            assert result is mock_mgr
        finally:
            jobs_mod._job_manager = original

    async def test_list_active_jobs_endpoint(self, client):
        """GET /api/jobs returns active jobs with count and namespace."""
        import routers.jobs as jobs_mod

        original = jobs_mod._job_manager
        mock_mgr = MagicMock()
        mock_mgr.get_active_jobs = AsyncMock(return_value=["job1", "job2"])
        mock_mgr.namespace = "test-ns"
        jobs_mod._job_manager = mock_mgr
        try:
            resp = await client.get("/api/jobs")
            assert resp.status_code == 200
            data = resp.json()
            assert data["count"] == 2
            assert data["active_jobs"] == ["job1", "job2"]
            assert data["namespace"] == "test-ns"
            assert "max_concurrent" in data
        finally:
            jobs_mod._job_manager = original

    async def test_create_job_endpoint(self, client):
        """POST /api/jobs creates a job and returns 201 with job details."""
        import routers.jobs as jobs_mod

        original = jobs_mod._job_manager
        mock_mgr = AsyncMock()
        mock_mgr.count_all_active_jobs = AsyncMock(return_value=0)
        mock_mgr.create_pytest_job = AsyncMock(return_value="pytest-job-abc123")
        mock_mgr.namespace = "test-ns"
        jobs_mod._job_manager = mock_mgr
        try:
            resp = await client.post(
                "/api/jobs",
                json={"pytest_args": ["-v", "--tb=short"]},
            )
            assert resp.status_code == 201
            data = resp.json()
            assert data["job_name"] == "pytest-job-abc123"
            assert data["namespace"] == "test-ns"
            assert data["status"] == "created"
        finally:
            jobs_mod._job_manager = original

    async def test_get_job_status_endpoint(self, client):
        """GET /api/jobs/{name} returns job status with expected fields."""
        import routers.jobs as jobs_mod

        original = jobs_mod._job_manager
        mock_mgr = MagicMock()
        mock_mgr.get_job_status = AsyncMock(
            return_value={"status": "succeeded", "active": 0, "succeeded": 1, "failed": 0}
        )
        mock_mgr.get_job_logs = AsyncMock(return_value="test output")
        mock_mgr.namespace = "test-ns"
        jobs_mod._job_manager = mock_mgr
        try:
            resp = await client.get("/api/jobs/test-job")
            assert resp.status_code == 200
            data = resp.json()
            assert data["job_name"] == "test-job"
            assert data["namespace"] == "test-ns"
            assert data["status"] == "succeeded"
            assert data["active"] == 0
            assert data["succeeded"] == 1
            assert data["failed"] == 0
            assert data["logs"] == "test output"
        finally:
            jobs_mod._job_manager = original

    async def test_get_job_status_not_found(self, client):
        """GET /api/jobs/{name} returns 404 when job does not exist."""
        import routers.jobs as jobs_mod

        original = jobs_mod._job_manager
        mock_mgr = MagicMock()
        mock_mgr.get_job_status = AsyncMock(return_value=None)
        mock_mgr.namespace = "test-ns"
        jobs_mod._job_manager = mock_mgr
        try:
            resp = await client.get("/api/jobs/nonexistent-job")
            assert resp.status_code == 404
        finally:
            jobs_mod._job_manager = original

    async def test_delete_job_endpoint(self, client):
        """DELETE /api/jobs/{name} deletes a job and returns confirmation."""
        import routers.jobs as jobs_mod

        original = jobs_mod._job_manager
        mock_mgr = AsyncMock()
        mock_mgr.get_job_status = AsyncMock(return_value={"status": "succeeded"})
        mock_mgr.delete_job = AsyncMock(return_value=True)
        mock_mgr.namespace = "test-ns"
        jobs_mod._job_manager = mock_mgr
        try:
            resp = await client.delete("/api/jobs/test-job")
            assert resp.status_code == 200
            data = resp.json()
            assert data["job_name"] == "test-job"
            assert data["status"] == "deleted"
            assert "test-job" in data["message"]
            mock_mgr.delete_job.assert_awaited_once_with("test-job", tenant_id="n8n-dev")
        finally:
            jobs_mod._job_manager = original

    async def test_delete_job_not_found(self, client):
        """DELETE /api/jobs/{name} returns 404 when job doesn't exist."""
        import routers.jobs as jobs_mod

        original = jobs_mod._job_manager
        mock_mgr = AsyncMock()
        mock_mgr.get_job_status = AsyncMock(return_value=None)
        mock_mgr.delete_job = AsyncMock(return_value=False)
        mock_mgr.namespace = "test-ns"
        jobs_mod._job_manager = mock_mgr
        try:
            resp = await client.delete("/api/jobs/nonexistent-job")
            assert resp.status_code == 404
        finally:
            jobs_mod._job_manager = original


# =====================================================================
# services/job_manager.py — KubernetesJobManager __init__, get_job_manager
# =====================================================================


class TestJobManagerInit:
    """Cover KubernetesJobManager __init__ and get_job_manager singleton."""

    def test_job_manager_in_cluster_env(self):
        """in_cluster=False loads kubeconfig (out-of-cluster)."""
        from services.job_manager import KubernetesJobManager

        with patch("kubernetes.config.load_kube_config"):
            mgr = KubernetesJobManager(namespace="test", in_cluster=False)
            assert mgr.namespace == "test"

    def test_job_manager_config_error(self):
        """Config loading failure raises RuntimeError."""
        from services.job_manager import KubernetesJobManager

        with patch("kubernetes.config.load_kube_config", side_effect=RuntimeError("no config")):
            with pytest.raises(RuntimeError, match="no config"):
                KubernetesJobManager(namespace="test", in_cluster=False)

    def test_get_job_manager_singleton(self):
        """get_job_manager returns the same instance on repeated calls."""
        import services.job_manager as jm_mod

        original = jm_mod._job_manager_instance
        jm_mod._job_manager_instance = None

        try:
            with patch("kubernetes.config.load_kube_config"):
                mgr1 = jm_mod.get_job_manager("ns1")
                mgr2 = jm_mod.get_job_manager("ns2")
                assert mgr1 is mgr2  # Singleton
                assert mgr1.namespace == "ns1"  # First call's namespace is used
        finally:
            jm_mod._job_manager_instance = original


# =====================================================================
# services/cache_manager.py — deeper RedisCacheManager coverage
# =====================================================================


class TestRedisCacheManagerDeep:
    """Cover uncovered methods of RedisCacheManager."""

    def test_redis_cache_manager_init(self):
        """RedisCacheManager initializes with empty stats and no redis connection."""
        from services.cache_manager import RedisCacheManager

        mgr = RedisCacheManager()
        assert mgr._redis is None
        assert mgr.stats["hits"] == 0
        assert mgr.stats["misses"] == 0

    def test_redis_cache_manager_get_stats(self):
        """get_stats returns dict with hit/miss counters."""
        from services.cache_manager import RedisCacheManager

        mgr = RedisCacheManager()
        stats = mgr.get_stats()
        assert isinstance(stats, dict)
        assert stats["hits"] == 0
        assert stats["misses"] == 0

    async def test_redis_cache_manager_get_returns_none_without_redis(self):
        """get() without redis connection returns None and increments misses."""
        from services.cache_manager import RedisCacheManager

        mgr = RedisCacheManager()
        result = await mgr.get("nonexistent-key")
        assert result is None

    async def test_redis_cache_manager_set_without_redis_returns_false(self):
        """set() without redis connection should fail gracefully and return False."""
        from services.cache_manager import RedisCacheManager

        mgr = RedisCacheManager()
        # No redis connection initialized — set should handle error and return False
        result = await mgr.set("key", "value")
        assert result is False

    async def test_redis_cache_manager_close(self):
        """close() is safe to call without active redis connection."""
        from services.cache_manager import RedisCacheManager

        mgr = RedisCacheManager()
        await mgr.close()  # Should not raise


# =====================================================================
# services/validation.py — deeper branches
# =====================================================================


class TestValidationServiceBranches:
    """Cover deeper branches in services/validation.py."""

    async def test_import_validation_validate_imports(self):
        """validate_imports correctly classifies stdlib, third-party, and missing imports."""
        from services.validation import ImportValidationService

        svc = ImportValidationService()
        result = await svc.validate_imports(
            code_graph_id="test",
            imports=[
                {"module": "os", "type": "import"},
                {"module": "json", "type": "import"},
                {"module": "nonexistent_package_xyz", "type": "import"},
            ],
        )
        assert "os" in result.stdlib_imports
        assert "json" in result.stdlib_imports
        assert len(result.stdlib_imports) >= 2
        assert len(result.missing_dependencies) >= 1

    async def test_import_validation_empty_module(self):
        """Empty module name is skipped (no valid imports)."""
        from services.validation import ImportValidationService

        svc = ImportValidationService()
        result = await svc.validate_imports(
            code_graph_id="test",
            imports=[{"module": "", "type": "import"}],
        )
        assert len(result.valid_imports) == 0

    async def test_import_validation_relative_import(self):
        """Relative imports (starting with .) are not stdlib."""
        from services.validation import ImportValidationService

        svc = ImportValidationService()
        assert svc.is_stdlib_module(".local") is False

    async def test_check_import_in_graph_no_db(self):
        """_check_import_in_graph returns False for nonexistent graph."""
        from services.validation import ImportValidationService

        svc = ImportValidationService()
        result = await svc._check_import_in_graph("nonexistent-graph", "os")
        assert result is False

    def test_stdlib_modules_populated(self):
        """ImportValidationService has a populated set of stdlib module names."""
        from services.validation import ImportValidationService

        svc = ImportValidationService()
        assert len(svc.stdlib_modules) > 0
        assert "os" in svc.stdlib_modules
        assert "sys" in svc.stdlib_modules
        assert "json" in svc.stdlib_modules

    async def test_is_third_party_available_prefix_no_match(self):
        """_is_third_party_available returns False for partial non-matching name."""
        from services.validation import ImportValidationService

        svc = ImportValidationService()
        available = {"numpy": {"version": "1.0"}}
        # "nump" is not "numpy" — should not match
        result = await svc._is_third_party_available("nump", available)
        assert result is False

    async def test_is_third_party_available_exact_match(self):
        """_is_third_party_available returns True for exact package name."""
        from services.validation import ImportValidationService

        svc = ImportValidationService()
        available = {"numpy": {"version": "1.0"}}
        result = await svc._is_third_party_available("numpy", available)
        assert result is True

    def test_is_likely_third_party_common(self):
        """Common third-party packages are recognized."""
        from services.validation import ImportValidationService

        svc = ImportValidationService()
        assert svc._is_likely_third_party("flask") is True
        assert svc._is_likely_third_party("numpy") is True

    def test_is_likely_third_party_local(self):
        """Unknown local module names are not recognized as third-party."""
        from services.validation import ImportValidationService

        svc = ImportValidationService()
        assert svc._is_likely_third_party("my_local_util_xyz") is False

    async def test_ast_analysis_exception_handling(self):
        """ASTValidationService reports errors when analysis raises."""
        from services.validation import ASTValidationService

        svc = ASTValidationService()
        with patch.object(svc, "_analyze_python_ast", side_effect=RuntimeError("boom")):
            result = await svc.analyze_proposed_code_ast("x = 1", "test.py")
        assert result.is_valid is False
        assert "boom" in result.syntax_errors[0]

    def test_get_attribute_name(self):
        """_get_attribute_name resolves Name nodes and falls back to 'unknown'."""
        import ast

        from services.validation import ASTValidationService

        svc = ASTValidationService()
        name_node = ast.Name(id="foo")
        assert svc._get_attribute_name(name_node) == "foo"

        num_node = ast.Constant(value=42)
        assert svc._get_attribute_name(num_node) == "unknown"


# =====================================================================
# routers/validation_status.py — status endpoints
# =====================================================================


class TestValidationStatusRouter:
    """Cover validation_status router endpoints."""

    async def test_update_validation_status_not_found(self, client):
        """POST /api/validation/status returns 404 for unknown validation_id."""
        resp = await client.post(
            "/api/validation/status",
            json={
                "validation_id": "nonexistent-id",
                "status": "in_progress",
            },
        )
        assert resp.status_code == 404

    async def test_get_validation_status_not_found(self, client):
        """GET /api/validation/{id} returns 404 for unknown validation."""
        resp = await client.get("/api/validation/nonexistent-id")
        assert resp.status_code == 404

    async def test_complete_validation_not_found(self, client):
        """POST /api/validation/complete returns 404 for unknown validation."""
        resp = await client.post(
            "/api/validation/complete",
            json={
                "validation_id": "nonexistent-id",
                "status": "completed",
                "results": {"test": True},
            },
        )
        assert resp.status_code == 404

    async def test_validation_lifecycle(self, client):
        """Register, update, complete, and get validation status."""
        from routers.validation_status import get_pr_validation_orchestrator

        orchestrator = get_pr_validation_orchestrator()
        vid = "test-lifecycle-id"
        orchestrator.register_validation(
            vid,
            {
                "fast_validation": {"passed": True},
                "comprehensive_validation": {"status": "triggered"},
            },
        )

        # Get status while triggered (should report in_progress)
        resp = await client.get(f"/api/validation/{vid}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["validation_id"] == vid
        assert data["status"] == "in_progress"  # "triggered" maps to "in_progress"

        # Update status
        resp = await client.post(
            "/api/validation/status",
            json={
                "validation_id": vid,
                "status": "in_progress",
                "stage": "testing",
                "message": "Running tests",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["validation_id"] == vid
        assert data["message"] == "Status updated successfully"

        # After updating comp status to "in_progress", GET falls through to fast_validation
        resp = await client.get(f"/api/validation/{vid}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["validation_id"] == vid
        # comp status is "in_progress" which doesn't match triggered/completed/failed,
        # so it checks fast_validation.passed=True -> "fast_validation_passed"
        assert data["status"] == "fast_validation_passed"

        # Complete
        resp = await client.post(
            "/api/validation/complete",
            json={
                "validation_id": vid,
                "status": "completed",
                "results": {"passed": True, "score": 100},
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert "completed" in data["message"].lower() or "stored" in data["message"].lower()

        # After completion, GET should return "completed"
        resp = await client.get(f"/api/validation/{vid}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "completed"


# =====================================================================
# routers/cache.py — more cache endpoint coverage
# =====================================================================


class TestCacheRouterEndpoints:
    """Cover more cache router endpoints."""

    async def test_cache_clear_endpoint(self, client):
        """DELETE /api/cache/clear returns 200 or 500 (depending on Redis)."""
        resp = await client.delete("/api/cache/clear")
        # Redis is not available in test env, so expect 500
        assert resp.status_code == 500

    async def test_cache_health_endpoint(self, client):
        """GET /api/cache/health returns cache health with status field."""
        resp = await client.get("/api/cache/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert "performance" in data


# =====================================================================
# routers/visualization.py — visualization endpoint coverage
# =====================================================================


class TestVisualizationRouter:
    """Cover visualization router endpoints."""

    async def test_get_dependency_graph_not_found(self, client):
        """GET /api/visualization/dependency-graph/{name} returns 404 for nonexistent."""
        resp = await client.get("/api/visualization/dependency-graph/nonexistent")
        assert resp.status_code == 404

    async def test_get_cross_language_graph_not_found(self, client):
        """GET /api/visualization/cross-language/{name} returns 404 for nonexistent."""
        resp = await client.get("/api/visualization/cross-language/nonexistent")
        assert resp.status_code == 404


# =====================================================================
# routers/metrics.py — more metrics coverage
# =====================================================================


class TestMetricsRouterEndpoints:
    """Cover metrics router endpoint lines."""

    async def test_graph_summary(self, client):
        """GET /api/health/metrics/graph-summary returns summary with expected fields."""
        resp = await client.get("/api/health/metrics/graph-summary")
        assert resp.status_code == 200
        data = resp.json()
        # When no DB is available, returns empty default
        assert "entity_counts" in data
        assert "relationship_counts" in data
        assert "graph_density" in data
        assert "analysis_timestamp" in data

    async def test_container_entity_metrics_not_found(self, client):
        """GET /api/health/metrics/container/{name}/entity/{entity}/metrics returns 404."""
        resp = await client.get("/api/health/metrics/container/nonexistent/entity/node1/metrics")
        assert resp.status_code == 404

    async def test_centrality_metrics_no_db(self, client):
        """POST /api/health/metrics/centrality with no graph is a typed 404, not 500."""
        resp = await client.post("/api/health/metrics/centrality")
        assert resp.status_code == 404


# =====================================================================
# routers/validation.py — more validation endpoints
# =====================================================================


class TestValidationRouterEndpoints:
    """Cover validation router endpoints with proper response body checks."""

    # ⚠️ These three used to assert ``valid is False`` with the comment
    # "# File doesn't exist". They passed for the wrong reason: the router
    # answered from the gateway's own filesystem, where NOTHING exists, so
    # valid=false was a constant and the assertion could not fail. A request
    # that names no codebase now gets a refusal instead of a verdict.

    async def test_validate_file_reference_without_codebase_refuses(self, client):
        """POST /api/validation/file-reference with no codebase refuses."""
        resp = await client.post(
            "/api/validation/file-reference",
            json={"file_path": "nonexistent.py"},
        )
        assert resp.status_code == 400
        assert "valid" not in resp.json()

    async def test_validate_function_reference_without_codebase_refuses(self, client):
        """POST /api/validation/function-reference with no codebase refuses."""
        resp = await client.post(
            "/api/validation/function-reference",
            json={"function_name": "nonexistent_function"},
        )
        assert resp.status_code == 400
        assert "valid" not in resp.json()

    async def test_validate_class_reference_without_codebase_refuses(self, client):
        """POST /api/validation/class-reference with no codebase refuses."""
        resp = await client.post(
            "/api/validation/class-reference",
            json={"class_name": "NonexistentClass"},
        )
        assert resp.status_code == 400
        assert "valid" not in resp.json()

    async def test_validate_code_snippet_missing_path_returns_400(self, client):
        """POST /api/validation/code-snippet without codebase_path or container_name returns 400."""
        resp = await client.post(
            "/api/validation/code-snippet",
            json={"code_snippet": "import os\nx = 1"},
        )
        assert resp.status_code == 400
        data = resp.json()
        assert "codebase_path" in data["detail"] or "container_name" in data["detail"]


# =====================================================================
# services/code_graph/graph_validation.py — remaining branches
# =====================================================================


class TestGraphValidationRemainingBranches:
    """Cover remaining uncovered branches in graph_validation.py."""

    def test_check_dependency_cycles_error_handling(self):
        """Cycle detection returns has_cycles=False with error on failure."""
        import networkx as nx

        from services.code_graph.graph_validation import GraphValidator

        validator = GraphValidator()
        g = MagicMock(spec=nx.DiGraph)

        with patch("networkx.simple_cycles", side_effect=RuntimeError("cycle error")):
            result = validator.check_dependency_cycles(g)

        assert result["has_cycles"] is False
        assert "error" in result

    def test_validate_graph_integrity_error_handling(self):
        """Integrity check returns is_intact=False on error."""
        import networkx as nx

        from services.code_graph.graph_validation import GraphValidator

        validator = GraphValidator()
        g = MagicMock(spec=nx.DiGraph)
        g.nodes.return_value = MagicMock(side_effect=RuntimeError("node error"))

        result = validator.validate_graph_integrity(g, expected_nodes=["A"])
        assert result["is_intact"] is False
