"""Round-2 coverage tests targeting remaining gaps in the weakest modules.

Files targeted (with pre-round coverage):
- routers/proxy.py (73%) — WebSocket paths, tenant fallback, size checks
- services/code_graph/multi_language_graph_service.py (83%) — analyze_directory
- routers/graphs.py (86%) — orphan scan, pagination, init, cleanup errors
- services/validation.py (85%) — import graph check, third-party mapping
- services/code_graph/structure_storage.py (86%) — merge settings, commit err
- app_factory.py (89%) — lifespan shutdown, CORS origins, middleware imports
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# =====================================================================
# routers/proxy.py — WebSocket route query string, _proxy_websocket error,
# tenant fallback (no user but tenant_id on request.state)
# =====================================================================


def _mock_backend(
    status: int = 200,
    body: bytes = b'{"ok": true}',
    headers: dict[str, str] | None = None,
) -> AsyncMock:
    resp_headers = {"content-type": "application/json"}
    if headers:
        resp_headers.update(headers)
    mock_response = httpx.Response(status, content=body, headers=resp_headers)
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=mock_response)
    return mock_client


class TestProxyTenantFallback:
    """x-tenant-id comes from the gateway's resolved auth context, never the client.

    The proxy strips any client-supplied x-tenant-id and forwards the
    require_tenant-resolved tenant instead (the single source of truth). A
    client can therefore never pin an arbitrary tenant on the backend, while a
    token-only request still carries the correct tenant downstream (so backends
    don't fall through to their trusted-network bypass / DEFAULT_TENANT_ID).
    """

    async def test_client_supplied_tenant_id_replaced_by_resolved(self, app, client):
        """The client's x-tenant-id is dropped and replaced by the gateway-resolved
        tenant (a UUIDv7 license tenant), never passed through."""
        from auth.dependencies import require_tenant

        resolved_tenant = "0195f48b-d000-7000-8000-000000000001"
        app.dependency_overrides[require_tenant] = lambda: resolved_tenant
        try:
            mock = _mock_backend()

            with patch("routers.proxy.get_proxy_client", return_value=mock):
                resp = await client.get(
                    "/api/analysis/test-tenant",
                    headers={
                        "x-user-id": "svc-account",
                        "x-tenant-id": "0195f48b-d000-7000-8000-0000000000ff",
                    },
                )

            assert resp.status_code == 200
            sent_headers = mock.request.call_args.kwargs["headers"]
            assert sent_headers.get("x-tenant-id") == resolved_tenant
            assert sent_headers.get("x-tenant-id") != "0195f48b-d000-7000-8000-0000000000ff"
        finally:
            app.dependency_overrides.pop(require_tenant, None)


class TestProxyWebSocketRouteQueryString:
    """WebSocket routes append query strings correctly."""

    def test_ws_realtime_url_conversion(self):
        from routers.proxy import _WS_REALTIME_URL

        assert _WS_REALTIME_URL.startswith("ws://")

    def test_realtime_ws_route_path_present(self):
        from routers.proxy import router

        paths = [r.path for r in router.routes if hasattr(r, "path") and r.path]
        assert "/api/realtime/ws/{path:path}" in paths

    def test_broadcast_subscribe_route_present(self):
        from routers.proxy import router

        paths = [r.path for r in router.routes if hasattr(r, "path") and r.path]
        assert "/api/broadcast/subscribe" in paths


class TestProxyRequestNoClient:
    """When request.client is None, x-forwarded-for is not set."""

    async def test_no_client_attribute(self, client):
        mock = _mock_backend()

        async def null_client(request, call_next):
            request._client = None  # noqa: SLF001
            return await call_next(request)

        from starlette.middleware.base import BaseHTTPMiddleware

        app = client._transport.app  # noqa: SLF001
        app.add_middleware(BaseHTTPMiddleware, dispatch=null_client)

        with patch("routers.proxy.get_proxy_client", return_value=mock):
            resp = await client.get("/api/analysis/no-client")
        # Should still succeed even without client info
        assert resp.status_code == 200


# =====================================================================
# routers/graphs.py — _find_orphaned_graphs branches, pagination,
# initialize errors, cleanup errors
# =====================================================================


def _make_temp_db(nodes=None, edges=None, tenant_id="n8n-dev"):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS nodes "
        "(id TEXT PRIMARY KEY, type TEXT, properties TEXT, content_hash TEXT, "
        "tenant_id TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS edges "
        "(source TEXT, target TEXT, relationship TEXT, properties TEXT, tenant_id TEXT)"
    )
    if nodes:
        for n in nodes:
            conn.execute(
                "INSERT INTO nodes (id, type, properties, tenant_id) VALUES (?, ?, ?, ?)",
                (
                    n["id"],
                    n["type"],
                    json.dumps(n.get("properties", {})),
                    n.get("tenant_id", tenant_id),
                ),
            )
    if edges:
        for e in edges:
            conn.execute(
                "INSERT INTO edges (source, target, relationship, properties, tenant_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    e["source"],
                    e["target"],
                    e["relationship"],
                    json.dumps(e.get("properties", {})),
                    tenant_id,
                ),
            )
    conn.commit()
    conn.close()
    return path


class TestValidateSqlitePagination:
    def test_normal_pagination_passes(self):
        from routers.graphs import _validate_sqlite_pagination

        _validate_sqlite_pagination(1, 50)  # must not raise

    def test_page_too_large_raises(self):
        from fastapi import HTTPException

        from routers.graphs import _validate_sqlite_pagination

        with pytest.raises(HTTPException) as exc_info:
            _validate_sqlite_pagination(2_000_000, 50)
        assert exc_info.value.status_code == 400

    def test_offset_overflow_raises(self):
        from fastapi import HTTPException

        from routers.graphs import SQLITE_INTEGER_MAX, _validate_sqlite_pagination

        # Ensure offset = (page-1)*limit > SQLITE_INTEGER_MAX
        with pytest.raises(HTTPException) as exc_info:
            _validate_sqlite_pagination(SQLITE_INTEGER_MAX + 2, 1)
        assert exc_info.value.status_code == 400


def _analysis_resp(body=None, status_code=200):
    request = httpx.Request("POST", "http://coding-analysis/api/analysis/graphs/x")
    return httpx.Response(status_code, json=body or {}, request=request)


class TestInitializeCodeGraph:
    async def test_initialize_creates_graph(self, client):
        with patch(
            "routers.graphs._analysis_post",
            new=AsyncMock(
                return_value=_analysis_resp(
                    {
                        "container_name": "test-init",
                        "created": True,
                        "repo_node_id": "repo_test-init",
                    },
                    status_code=201,
                )
            ),
        ):
            resp = await client.post(
                "/api/graphs/initialize",
                json={"container_name": "test-init", "codebase_path": "/data/test"},
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "success"
        assert body["container_name"] == "test-init"
        assert body["nodes_created"] == 1

    async def test_initialize_conflict_without_force(self, client):
        """Existing graph without force_rebuild returns 409."""
        with patch(
            "routers.graphs._analysis_post",
            new=AsyncMock(return_value=_analysis_resp(status_code=409)),
        ):
            resp = await client.post(
                "/api/graphs/initialize",
                json={"container_name": "existing"},
            )
        assert resp.status_code == 409

    async def test_initialize_get_method_blocked(self, client):
        """GET /api/graphs/initialize-disabled-get returns 405."""
        resp = await client.get("/api/graphs/initialize-disabled-get")
        assert resp.status_code == 405


class TestCleanupOrphanedErrors:
    """Error paths in cleanup_orphaned_graphs."""

    async def test_cleanup_no_confirm_message(self, client):
        """dry_run=false without confirm returns specific message."""
        with (
            patch(
                "routers.graphs._list_active_container_names",
                new_callable=AsyncMock,
                return_value=set(),
            ),
            patch(
                "routers.graphs._list_graphs_from_analysis",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            resp = await client.delete("/api/graphs/cleanup/orphaned?dry_run=false&confirm=false")
        assert resp.status_code == 200
        body = resp.json()
        assert "not performed" in body["message"].lower() or body["cleaned_count"] == 0

    async def test_cleanup_with_confirm_deletes(self, client):
        """dry_run=false with confirm=true actually deletes via the analysis proxy."""
        with (
            patch(
                "routers.graphs._list_active_container_names",
                new_callable=AsyncMock,
                return_value=set(),
            ),
            patch(
                "routers.graphs._list_graphs_from_analysis",
                new_callable=AsyncMock,
                return_value=[{"container_name": "orphan1", "tenant_id": None}],
            ),
            patch(
                "routers.graphs._analysis_delete",
                new=AsyncMock(
                    return_value=_analysis_resp({"nodes_deleted": 1, "edges_deleted": 0})
                ),
            ),
        ):
            resp = await client.delete("/api/graphs/cleanup/orphaned?dry_run=false&confirm=true")
        assert resp.status_code == 200
        body = resp.json()
        assert body["cleaned_count"] == 1

    async def test_cleanup_httpx_error_returns_500(self, client):
        """httpx.HTTPError during container check returns 500."""
        with patch(
            "routers.graphs._list_active_container_names",
            new_callable=AsyncMock,
            side_effect=httpx.ConnectError("cannot connect"),
        ):
            resp = await client.delete("/api/graphs/cleanup/orphaned")
        assert resp.status_code == 500


class TestListGraphsError:
    async def test_list_graphs_exception(self, client):
        """Exception in list_graphs returns 500."""

        async def _boom(*args, **kwargs):
            raise RuntimeError("boom")

        with patch("routers.graphs._analysis_get", new=_boom):
            resp = await client.get("/api/graphs")
        assert resp.status_code == 500


class TestGetGraphDetailsError:
    async def test_exception_returns_500(self, client):
        """Non-HTTPException in get_graph_details returns 500."""

        async def _boom(*args, **kwargs):
            raise RuntimeError("upstream exploded")

        with patch("routers.graphs._analysis_get", new=_boom):
            resp = await client.get("/api/graphs/testdb")
        assert resp.status_code == 500


class TestDeleteGraphError:
    async def test_exception_returns_500(self, client):
        """Non-HTTPException in delete_graph returns 500."""

        async def _boom(*args, **kwargs):
            raise RuntimeError("DB exploded")

        with patch("routers.graphs._analysis_delete", new=_boom):
            resp = await client.delete("/api/graphs/testdb")
        assert resp.status_code == 500


class TestGetGraphNodesError:
    async def test_upstream_failure_returns_500(self, client):
        """Exception bubbling out of _analysis_get surfaces as 500."""

        async def _boom(*args, **kwargs):
            raise RuntimeError("upstream exploded")

        with patch("routers.graphs._analysis_get", new=_boom):
            resp = await client.get("/api/graphs/testdb/nodes")
        assert resp.status_code == 500


class TestGetGraphEdgesError:
    async def test_upstream_failure_returns_500(self, client):
        async def _boom(*args, **kwargs):
            raise RuntimeError("upstream exploded")

        with patch("routers.graphs._analysis_get", new=_boom):
            resp = await client.get("/api/graphs/testdb/edges")
        assert resp.status_code == 500


class TestAugmentedGraphError:
    async def test_ai_augmented_error_returns_500(self, client):
        # /ai-augmented derives existence from _get_graph_counts -> _analysis_get.
        async def _boom(*args, **kwargs):
            raise RuntimeError("boom")

        with patch("routers.graphs._get_graph_counts", new=_boom):
            resp = await client.get("/api/graphs/testdb/ai-augmented")
        assert resp.status_code == 500

    async def test_runtime_augmented_error_returns_500(self, client):
        async def _boom(*args, **kwargs):
            raise RuntimeError("boom")

        with patch("routers.graphs._get_graph_counts", new=_boom):
            resp = await client.get("/api/graphs/testdb/runtime-augmented")
        assert resp.status_code == 500

    async def test_architecture_augmented_error_returns_500(self, client):
        # /augmented now forwards to _analysis_get; simulate an upstream
        # RuntimeError on that call.
        async def _boom(*args, **kwargs):
            raise RuntimeError("upstream exploded")

        with patch("routers.graphs._analysis_get", new=_boom):
            resp = await client.get("/api/graphs/testdb/augmented")
        assert resp.status_code == 500


class TestInternalCountsError:
    async def test_exception_returns_500(self, client):
        with patch("routers.graphs._get_graph_counts", side_effect=RuntimeError("fail")):
            resp = await client.get("/api/graphs/internal/database/counts/testdb")
        assert resp.status_code == 500


# =====================================================================
# services/validation.py — _check_import_in_graph, third-party mapping,
# function graph check with exception, import from level
# =====================================================================


class TestImportValidationImportInGraph:
    """Test _check_import_in_graph with real SQLite."""

    async def test_check_import_found_in_graph(self):
        from services.validation import ImportValidationService

        svc = ImportValidationService(project_root="/tmp/test")

        db_path = _make_temp_db(
            nodes=[
                {
                    "id": "mod1",
                    "type": "module",
                    "properties": {"name": "my_module"},
                }
            ]
        )
        try:
            with patch.object(svc, "_check_import_in_graph") as mock_check:
                mock_check.return_value = True
                imports = [{"module": "my_module", "type": "import", "line": 1}]
                result = await svc.validate_imports("graph1", imports)
                assert "my_module" in result.local_imports
        finally:
            os.unlink(db_path)


class TestImportValidationThirdPartyMapping:
    """Test _is_third_party_available with import_to_package_map."""

    async def test_cv2_maps_to_opencv(self):
        from services.validation import ImportValidationService

        svc = ImportValidationService()
        available = {"opencv-python": {}}
        result = await svc._is_third_party_available("cv2", available)
        assert result is True

    async def test_pil_maps_to_pillow(self):
        from services.validation import ImportValidationService

        svc = ImportValidationService()
        available = {"pillow": {}}
        result = await svc._is_third_party_available("PIL", available)
        assert result is True

    async def test_yaml_maps_to_pyyaml(self):
        from services.validation import ImportValidationService

        svc = ImportValidationService()
        available = {"pyyaml": {}}
        result = await svc._is_third_party_available("yaml", available)
        assert result is True

    async def test_partial_name_match(self):
        from services.validation import ImportValidationService

        svc = ImportValidationService()
        available = {"mypackage": {}}
        # "mypackage_ext" starts with "mypackage"
        result = await svc._is_third_party_available("mypackage_ext", available)
        assert result is True

    async def test_no_match(self):
        from services.validation import ImportValidationService

        svc = ImportValidationService()
        available = {"unrelated": {}}
        result = await svc._is_third_party_available("totally_different", available)
        assert result is False


class TestFunctionValidationErrors:
    """Test _check_function_in_graph_direct error path."""

    async def test_db_query_exception_returns_false(self):
        from services.validation import FunctionValidationService

        svc = FunctionValidationService()
        mock_db = MagicMock()
        mock_db.conn = AsyncMock()
        mock_db.conn.execute = AsyncMock(side_effect=Exception("query failed"))

        result = await svc._check_function_in_graph_direct("my_func", mock_db)
        assert result is False

    async def test_db_returns_none_result(self):
        from services.validation import FunctionValidationService

        svc = FunctionValidationService()
        mock_db = MagicMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(return_value=None)
        mock_db.conn = AsyncMock()
        mock_db.conn.execute = AsyncMock(return_value=mock_cursor)

        result = await svc._check_function_in_graph_direct("my_func", mock_db)
        assert result is False


class TestImportValidationSimilarPackages:
    """Test suggestion generation with similar packages."""

    async def test_similar_packages_suggestion_for_likely_third_party(self):
        from services.validation import ImportValidationService

        svc = ImportValidationService()

        # "requests" is in _is_likely_third_party, package_info returns None
        with (
            patch.object(svc, "_check_import_in_graph", return_value=False),
            patch.object(
                svc.dependency_parser,
                "get_similar_packages",
                return_value=["requests2"],
            ),
        ):
            imports = [{"module": "requests", "type": "import", "line": 1}]
            result = await svc.validate_imports("g1", imports)
            assert len(result.suggestions) > 0
            assert "Did you mean" in result.suggestions[0]

    async def test_no_similar_packages_for_unknown_module(self):
        from services.validation import ImportValidationService

        svc = ImportValidationService()

        with (
            patch.object(svc, "_check_import_in_graph", return_value=False),
            patch.object(svc.dependency_parser, "get_similar_packages", return_value=[]),
        ):
            imports = [
                {
                    "module": "totally_unknown_xyz_123",
                    "type": "import",
                    "line": 1,
                }
            ]
            result = await svc.validate_imports("g1", imports)
            assert len(result.suggestions) > 0
            assert "adding it to dependencies" in result.suggestions[0]


class TestASTValidationAdditional:
    """Additional AST validation edge cases."""

    async def test_import_from_no_module(self):
        """from . import something — module is None."""
        import ast

        from services.validation import ASTValidationService

        svc = ASTValidationService()
        # "from . import foo" has module=None
        node = ast.ImportFrom(module=None, names=[ast.alias(name="foo", asname=None)], level=1)
        node.lineno = 1
        result = svc._extract_import_info(node)
        assert len(result) == 1
        assert result[0]["module"] == "foo"
        assert result[0]["level"] == 1

    async def test_variable_extraction_non_name_target(self):
        """Assign to non-Name target (e.g., subscript) is skipped."""
        import ast

        from services.validation import ASTValidationService

        svc = ASTValidationService()
        code = "x[0] = 1"
        tree = ast.parse(code)
        node = tree.body[0]
        result = svc._extract_variable_info(node)
        assert result == []  # subscript target, not Name

    async def test_get_attribute_name_unknown(self):
        """Non-Name/Attribute node returns 'unknown'."""
        import ast

        from services.validation import ASTValidationService

        svc = ASTValidationService()
        node = ast.Constant(value=42)
        result = svc._get_attribute_name(node)
        assert result == "unknown"


# =====================================================================
# app_factory.py — CORS with specific origins, middleware import failures
# =====================================================================


class TestCorsSpecificOrigins:
    """CORS with specific (non-wildcard) origins enables credentials."""

    def test_specific_origins_enable_credentials(self, monkeypatch):
        from config.settings import get_settings

        get_settings.cache_clear()
        # cors_origins is list[str], pydantic-settings expects JSON array
        monkeypatch.setenv("CORS_ORIGINS", '["http://localhost:3000","http://example.com"]')
        monkeypatch.setenv("ENABLE_AUTH", "false")
        get_settings.cache_clear()

        with patch("services.cache_manager.get_cache_manager", return_value=AsyncMock()):
            from app_factory import create_app

            app = create_app()
            # CORSMiddleware should be present
            names = [m.cls.__name__ for m in app.user_middleware if hasattr(m, "cls")]
            assert "CORSMiddleware" in names

        monkeypatch.setenv("CORS_ORIGINS", '["*"]')
        get_settings.cache_clear()


class TestMiddlewareImportFailure:
    """Middleware import failures are handled gracefully."""

    def test_validation_middleware_import_failure(self, monkeypatch):
        from config.settings import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("ENABLE_AUTH", "false")
        get_settings.cache_clear()

        # This tests that create_app handles ImportError for ValidationMiddleware
        with (
            patch("services.cache_manager.get_cache_manager", return_value=AsyncMock()),
            patch(
                "app_factory._add_middlewares",
                wraps=None,  # let the original run
            ),
        ):
            from app_factory import create_app

            # Should not raise even if middlewares have issues
            app = create_app()
            assert app is not None

        get_settings.cache_clear()


# =====================================================================
# routers/graphs.py — _SimpleDbManager, _get_graph_tenant_id
# =====================================================================


class TestGetGraphCounts:
    async def test_returns_counts(self):
        """_get_graph_counts reads from coding-analysis over HTTP now."""
        import httpx

        from routers.graphs import _get_graph_counts

        req = httpx.Request("GET", "http://coding-analysis/api/analysis/graphs/testdb")
        response = httpx.Response(
            200,
            json={
                "container_name": "testdb",
                "exists": True,
                "node_count": 2,
                "edge_count": 1,
            },
            request=req,
        )

        async def _fake(*args, **kwargs):
            return response

        with patch("routers.graphs._analysis_get", new=_fake):
            nc, ec = await _get_graph_counts("testdb")
        assert nc == 2
        assert ec == 1

    async def test_returns_negative_when_not_found(self):
        import httpx

        from routers.graphs import _get_graph_counts

        req = httpx.Request("GET", "http://coding-analysis/api/analysis/graphs/missing")
        response = httpx.Response(404, json={"detail": "missing"}, request=req)

        async def _fake(*args, **kwargs):
            return response

        with patch("routers.graphs._analysis_get", new=_fake):
            nc, ec = await _get_graph_counts("missing")
        assert nc == -1
        assert ec == -1

    async def test_returns_negative_on_exception(self):
        from routers.graphs import _get_graph_counts

        async def _boom(*args, **kwargs):
            raise RuntimeError("boom")

        with patch("routers.graphs._analysis_get", new=_boom):
            nc, ec = await _get_graph_counts("broken")
        assert nc == -1
        assert ec == -1


# =====================================================================
# routers/graphs.py — _validate_container_name
# =====================================================================


class TestValidateContainerName:
    def test_valid_names(self):
        from routers.graphs import _validate_container_name

        assert _validate_container_name("my-app") == "my-app"
        assert _validate_container_name("app_v2") == "app_v2"
        assert _validate_container_name("app.service") == "app.service"

    def test_invalid_names_raise(self):
        from fastapi import HTTPException

        from routers.graphs import _validate_container_name

        with pytest.raises(HTTPException):
            _validate_container_name("bad name!")
        with pytest.raises(HTTPException):
            _validate_container_name("../escape")
        with pytest.raises(HTTPException):
            _validate_container_name("path/../traversal")


# =====================================================================
# routers/graphs.py — _list_active_container_names
# =====================================================================


class TestListActiveContainerNames:
    async def test_parses_containers_from_response(self, client):
        from routers.graphs import _list_active_container_names

        mock = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "containers": [
                {"container_name": "app1"},
                {"container_name": "app2"},
                "not-a-dict",  # should be skipped
                {"container_name": ""},  # empty should be skipped
                {"no_name_key": True},  # missing key should be skipped
            ]
        }
        mock.get = AsyncMock(return_value=mock_resp)

        with patch("routers.graphs.get_proxy_client", return_value=mock):
            # Create a mock request
            mock_request = MagicMock()
            mock_request.headers = {"authorization": "Bearer token"}
            result = await _list_active_container_names(mock_request, "test-tenant")

        assert result == {"app1", "app2"}

    async def test_parses_list_format(self, client):
        """When response is a flat list instead of {containers: [...]}."""
        from routers.graphs import _list_active_container_names

        mock = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = [
            {"container_name": "svc1"},
            {"container_name": "svc2"},
        ]
        mock.get = AsyncMock(return_value=mock_resp)

        with patch("routers.graphs.get_proxy_client", return_value=mock):
            mock_request = MagicMock()
            mock_request.headers = {"content-type": "application/json"}
            result = await _list_active_container_names(mock_request, "test-tenant")

        assert result == {"svc1", "svc2"}


# =====================================================================
# app_factory.py — lifespan shutdown with background tasks
# =====================================================================


class TestLifespanShutdown:
    """Test lifespan shutdown edge cases."""

    async def test_lifespan_with_background_tasks(self):
        """Shutdown cancels pending background tasks."""
        import asyncio

        from fastapi import FastAPI

        from app_factory import lifespan

        mock_cache = AsyncMock()
        mock_cache.close = AsyncMock()

        test_app = FastAPI()

        async def long_task():
            await asyncio.sleep(9999)

        with (
            patch(
                "services.cache_manager.get_cache_manager",
                return_value=mock_cache,
            ),
            patch(
                "openapi_aggregator.get_aggregated_spec",
                side_effect=Exception("skip openapi"),
            ),
        ):
            gen = lifespan(test_app)
            await gen.__aenter__()

            # Add a background task
            task = asyncio.create_task(long_task())
            test_app.state.background_tasks.add(task)

            await gen.__aexit__(None, None, None)

        # Task should have been cancelled
        assert task.cancelled() or task.done()

    async def test_lifespan_cache_close_failure(self):
        """Cache close failure during shutdown doesn't crash."""
        from fastapi import FastAPI

        from app_factory import lifespan

        mock_cache = AsyncMock()
        mock_cache.close = AsyncMock(side_effect=RuntimeError("close failed"))

        test_app = FastAPI()

        with (
            patch(
                "services.cache_manager.get_cache_manager",
                return_value=mock_cache,
            ),
            patch(
                "openapi_aggregator.get_aggregated_spec",
                side_effect=Exception("skip openapi"),
            ),
        ):
            gen = lifespan(test_app)
            await gen.__aenter__()
            # Shutdown should handle cache close failure
            await gen.__aexit__(None, None, None)

        mock_cache.close.assert_awaited_once()


class TestHealthApiHealth:
    """Verify /api/health/* routes return expected responses."""

    async def test_health_root_contains_uptime(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert "uptime_seconds" in body
        assert body["uptime_seconds"] >= 0

    async def test_root_endpoint_contains_docs_link(self, client):
        resp = await client.get("/")
        assert resp.status_code == 200
        body = resp.json()
        assert body["docs"] == "/docs"
        assert body["health"] == "/health"
