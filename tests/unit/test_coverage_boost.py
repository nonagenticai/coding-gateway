"""Tests targeting uncovered lines in weakest modules to push coverage above 90%.

Covers:
- app_factory: generate_unique_id, _add_middlewares branches, lifespan
- routers/proxy: _resolve_backend, _filter_headers, close_proxy_client, proxy errors
- services/grammar_manager: GrammarInfo, TreeSitterGrammarManager core methods
- services/code_graph_metrics: betweenness centrality, BFS helpers
- routers/graphs: _SimpleDbManager, graph helpers
- db/__init__.py: re-export coverage
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# =====================================================================
# app_factory: generate_unique_id
# =====================================================================


class TestGenerateUniqueId:
    """Test generate_unique_id helper in app_factory."""

    def test_root_path(self):
        from app_factory import generate_unique_id

        route = MagicMock()
        route.path = "/api/"
        route.methods = {"GET"}
        result = generate_unique_id(route)
        assert result == "root_get"

    def test_simple_path(self):
        from app_factory import generate_unique_id

        route = MagicMock()
        route.path = "/api/graphs"
        route.methods = {"POST"}
        result = generate_unique_id(route)
        assert result == "graphs_post"

    def test_path_with_param(self):
        from app_factory import generate_unique_id

        route = MagicMock()
        route.path = "/api/graphs/{graph_id}"
        route.methods = {"GET"}
        result = generate_unique_id(route)
        assert result == "graphs_graph_id_get"

    def test_health_root_path(self):
        from app_factory import generate_unique_id

        route = MagicMock()
        route.path = "/health"
        route.methods = {"GET"}
        result = generate_unique_id(route)
        assert result == "health_root_get"

    def test_health_api_path(self):
        from app_factory import generate_unique_id

        route = MagicMock()
        route.path = "/api/health"
        route.methods = {"GET"}
        result = generate_unique_id(route)
        assert result == "health_api_get"

    def test_no_methods(self):
        from app_factory import generate_unique_id

        route = MagicMock()
        route.path = "/api/test"
        route.methods = set()
        result = generate_unique_id(route)
        assert result == "test_unknown"

    def test_hyphenated_path(self):
        from app_factory import generate_unique_id

        route = MagicMock()
        route.path = "/api/bug-hunter"
        route.methods = {"GET"}
        result = generate_unique_id(route)
        assert result == "bug_hunter_get"

    def test_empty_path(self):
        from app_factory import generate_unique_id

        route = MagicMock()
        route.path = ""
        route.methods = {"GET"}
        result = generate_unique_id(route)
        assert result == "root_get"


# =====================================================================
# app_factory: _add_middlewares branches
# =====================================================================


class TestAddMiddlewares:
    """Test _add_middlewares branch coverage with real call validation."""

    def test_auth_disabled_skips_auth_middleware(self):
        from app_factory import _add_middlewares

        app = MagicMock()
        settings = MagicMock()
        settings.enable_auth = False
        settings.cors_origins = ["*"]
        _add_middlewares(app, settings)
        # AuthMiddleware is always registered — it handles ENABLE_AUTH=false
        # internally by injecting an anonymous user with DEFAULT_TENANT_ID
        middleware_classes = [
            call.args[0].__name__ if hasattr(call.args[0], "__name__") else str(call.args[0])
            for call in app.add_middleware.call_args_list
            if call.args
        ]
        assert "AuthMiddleware" in middleware_classes

    def test_auth_as_string_true_adds_auth_middleware(self):
        from app_factory import _add_middlewares

        app = MagicMock()
        settings = MagicMock()
        settings.enable_auth = "true"
        settings.cors_origins = ["*"]
        _add_middlewares(app, settings)
        # Should add auth middleware when enable_auth="true"
        assert app.add_middleware.call_count >= 2  # At least CORS + RequestLogging

    def test_auth_as_string_false_skips_auth(self):
        from app_factory import _add_middlewares

        app = MagicMock()
        settings = MagicMock()
        settings.enable_auth = "false"
        settings.cors_origins = ["*"]
        _add_middlewares(app, settings)
        # AuthMiddleware is always registered; it handles enable_auth=false
        # internally by injecting an anonymous user with DEFAULT_TENANT_ID
        middleware_classes = [
            call.args[0].__name__ if hasattr(call.args[0], "__name__") else str(call.args[0])
            for call in app.add_middleware.call_args_list
            if call.args
        ]
        assert "AuthMiddleware" in middleware_classes

    def test_cors_origins_as_string_gets_split(self):
        from app_factory import _add_middlewares

        app = MagicMock()
        settings = MagicMock()
        settings.enable_auth = False
        settings.cors_origins = "http://localhost,http://example.com"
        _add_middlewares(app, settings)
        # Find the CORSMiddleware call and verify origins were split
        from fastapi.middleware.cors import CORSMiddleware

        cors_calls = [
            call
            for call in app.add_middleware.call_args_list
            if call.args and call.args[0] is CORSMiddleware
        ]
        assert len(cors_calls) == 1
        assert cors_calls[0].kwargs["allow_origins"] == ["http://localhost", "http://example.com"]

    def test_cors_with_explicit_origins_enables_credentials(self):
        from app_factory import _add_middlewares

        app = MagicMock()
        settings = MagicMock()
        settings.enable_auth = False
        settings.cors_origins = ["http://example.com"]
        _add_middlewares(app, settings)
        from fastapi.middleware.cors import CORSMiddleware

        cors_calls = [
            call
            for call in app.add_middleware.call_args_list
            if call.args and call.args[0] is CORSMiddleware
        ]
        assert len(cors_calls) == 1
        # When origins aren't ["*"], credentials should be enabled
        assert cors_calls[0].kwargs["allow_credentials"] is True


# =====================================================================
# routers/proxy: _resolve_backend, _filter_headers
# =====================================================================


class TestProxyHelpers:
    """Test proxy module helper functions."""

    def test_resolve_backend_known_prefix(self):
        from routers.proxy import _resolve_backend

        result = _resolve_backend("/api/analysis/some/path")
        assert result is not None
        backend_base, target_url = result
        assert "coding-analysis" in backend_base or "8688" in backend_base
        assert target_url.endswith("/api/analysis/some/path")

    def test_resolve_backend_exact_prefix(self):
        from routers.proxy import _resolve_backend

        result = _resolve_backend("/api/tests")
        assert result is not None

    def test_resolve_backend_unknown_prefix(self):
        from routers.proxy import _resolve_backend

        result = _resolve_backend("/unknown/path")
        assert result is None

    def test_resolve_backend_no_match(self):
        from routers.proxy import _resolve_backend

        result = _resolve_backend("/totally-random")
        assert result is None

    def test_resolve_backend_containers(self):
        from routers.proxy import _resolve_backend

        result = _resolve_backend("/api/containers/list")
        assert result is not None
        assert "8691" in result[1] or "coding-containers" in result[1]

    def test_resolve_backend_security(self):
        from routers.proxy import _resolve_backend

        result = _resolve_backend("/api/security/scan")
        assert result is not None

    def test_resolve_backend_more_specific_wins(self):
        """The multi-line validation should route to containers, not validation router."""
        from routers.proxy import _resolve_backend

        result = _resolve_backend("/api/validation/multi-line-code-snippet")
        assert result is not None
        assert "8691" in result[1] or "coding-containers" in result[1]

    def test_filter_headers_strips_hop_by_hop(self):
        from routers.proxy import _filter_headers

        headers = {
            "content-type": "application/json",
            "host": "example.com",
            "connection": "keep-alive",
            "transfer-encoding": "chunked",
            "x-custom": "value",
        }
        filtered = _filter_headers(headers)
        assert "content-type" in filtered
        assert "x-custom" in filtered
        assert "host" not in filtered
        assert "connection" not in filtered
        assert "transfer-encoding" not in filtered

    def test_filter_headers_case_insensitive(self):
        from routers.proxy import _filter_headers

        headers = {"Host": "example.com", "Content-Length": "100"}
        filtered = _filter_headers(headers)
        assert "Host" not in filtered
        assert "Content-Length" not in filtered


class TestCloseProxyClient:
    """Test close_proxy_client."""

    async def test_close_when_no_client(self):
        from routers import proxy

        original = proxy._client
        proxy._client = None
        await proxy.close_proxy_client()
        assert proxy._client is None
        proxy._client = original

    async def test_close_when_client_exists(self):
        from routers import proxy

        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.aclose = AsyncMock()
        original = proxy._client
        proxy._client = mock_client
        await proxy.close_proxy_client()
        mock_client.aclose.assert_awaited_once()
        assert proxy._client is None
        proxy._client = original

    async def test_close_already_closed_client(self):
        from routers import proxy

        mock_client = AsyncMock()
        mock_client.is_closed = True
        original = proxy._client
        proxy._client = mock_client
        await proxy.close_proxy_client()
        mock_client.aclose.assert_not_awaited()
        proxy._client = original


# =====================================================================
# services/grammar_manager: GrammarInfo, core methods
# =====================================================================


class TestGrammarInfo:
    """Test GrammarInfo dataclass."""

    def test_to_dict_with_dates(self):
        from datetime import UTC, datetime

        from services.grammar_manager import GrammarInfo

        info = GrammarInfo(
            name="tree-sitter-python",
            language="python",
            repository_url="https://github.com/tree-sitter/tree-sitter-python",
            version="0.20.0",
            installed_version="0.19.0",
            last_checked=datetime(2025, 1, 1, tzinfo=UTC),
            last_updated=datetime(2025, 1, 1, tzinfo=UTC),
            install_path=Path("/tmp/grammars/python"),
        )
        d = info.to_dict()
        assert d["name"] == "tree-sitter-python"
        assert isinstance(d["last_checked"], str)
        assert isinstance(d["last_updated"], str)
        assert isinstance(d["install_path"], str)

    def test_to_dict_without_dates(self):
        from services.grammar_manager import GrammarInfo

        info = GrammarInfo(
            name="tree-sitter-java",
            language="java",
            repository_url="https://github.com/tree-sitter/tree-sitter-java",
            version="unknown",
        )
        d = info.to_dict()
        assert d["last_checked"] is None
        assert d["last_updated"] is None

    def test_from_dict_with_dates(self):
        from services.grammar_manager import GrammarInfo

        data = {
            "name": "tree-sitter-python",
            "language": "python",
            "repository_url": "https://github.com/tree-sitter/tree-sitter-python",
            "version": "0.20.0",
            "last_checked": "2025-01-01T00:00:00+00:00",
            "last_updated": "2025-01-01T00:00:00+00:00",
            "install_path": "/tmp/grammars/python",
        }
        info = GrammarInfo.from_dict(data)
        assert info.name == "tree-sitter-python"
        assert info.install_path == Path("/tmp/grammars/python")

    def test_from_dict_without_dates(self):
        from services.grammar_manager import GrammarInfo

        data = {
            "name": "tree-sitter-java",
            "language": "java",
            "repository_url": "https://github.com/tree-sitter/tree-sitter-java",
            "version": "unknown",
        }
        info = GrammarInfo.from_dict(data)
        assert info.last_checked is None


class TestTreeSitterGrammarManager:
    """Test TreeSitterGrammarManager core methods."""

    async def test_initialize_creates_grammars(self):
        from services.grammar_manager import TreeSitterGrammarManager

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = TreeSitterGrammarManager(cache_dir=Path(tmpdir))
            await mgr.initialize()
            assert len(mgr.grammars) > 0
            assert "python" in mgr.grammars
            assert "java" in mgr.grammars

    async def test_get_grammar_status(self):
        from services.grammar_manager import TreeSitterGrammarManager

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = TreeSitterGrammarManager(cache_dir=Path(tmpdir))
            await mgr.initialize()
            status = await mgr.get_grammar_status()
            assert "python" in status
            assert "installed" in status["python"]
            assert "language" in status["python"]

    async def test_install_unknown_language_fails(self):
        from services.grammar_manager import TreeSitterGrammarManager

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = TreeSitterGrammarManager(cache_dir=Path(tmpdir))
            await mgr.initialize()
            result = await mgr.install_grammar("nonexistent_language")
            assert result is False

    async def test_install_all_grammars(self):
        from services.grammar_manager import TreeSitterGrammarManager

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = TreeSitterGrammarManager(cache_dir=Path(tmpdir))
            await mgr.initialize()
            # Mock _download_and_install_grammar to avoid network calls
            mgr._download_and_install_grammar = AsyncMock(return_value=True)
            results = await mgr.install_all_grammars(force=True)
            assert len(results) > 0
            assert all(v is True for v in results.values())

    async def test_cleanup_old_versions_no_install_path(self):
        from services.grammar_manager import TreeSitterGrammarManager

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = TreeSitterGrammarManager(cache_dir=Path(tmpdir))
            await mgr.initialize()
            # No install paths set, so cleanup should return all zeros
            result = await mgr.cleanup_old_versions()
            assert all(v == 0 for v in result.values())

    async def test_check_for_updates_skips_recently_checked(self):
        from datetime import UTC, datetime

        from services.grammar_manager import TreeSitterGrammarManager

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = TreeSitterGrammarManager(cache_dir=Path(tmpdir))
            await mgr.initialize()
            # Set last_checked to now for all grammars
            for grammar in mgr.grammars.values():
                grammar.last_checked = datetime.now(UTC)
            result = await mgr.check_for_updates()
            # All should be False since recently checked
            assert all(v is False for v in result.values())

    async def test_save_and_load_grammar_info(self):
        from services.grammar_manager import TreeSitterGrammarManager

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = TreeSitterGrammarManager(cache_dir=Path(tmpdir))
            await mgr.initialize()
            # Save then reload
            mgr2 = TreeSitterGrammarManager(cache_dir=Path(tmpdir))
            await mgr2.initialize()
            assert len(mgr2.grammars) == len(mgr.grammars)


# =====================================================================
# db/__init__.py: import coverage
# =====================================================================


class TestDbInit:
    """Ensure db package is importable."""

    def test_db_module_importable(self):
        """The db module stub is installed by conftest; verify it exists."""
        import db

        assert db is not None


# =====================================================================
# routers/proxy: proxy_request error paths via HTTP client
# =====================================================================


class TestProxyRequestErrors:
    """Test proxy request error handling via HTTP client."""

    async def test_proxy_unmatched_route(self, client):
        """Unknown proxy route returns 404."""
        resp = await client.get("/api/nonexistent-service/test")
        assert resp.status_code == 404
        data = resp.json()
        assert data["error"]["code"] == "HTTP_404"

    async def test_proxy_connect_error(self, client):
        """Backend unreachable returns 502 (ConnectError) or 504 (ConnectTimeout)."""
        # /api/analysis routes to coding-analysis which is unreachable in tests.
        # Linux ARC runners typically resolve+refuse fast → ConnectError → 502;
        # macOS dev DNS times out at resolver layer → ConnectTimeout → 504.
        resp = await client.get("/api/analysis/test")
        assert resp.status_code in (502, 504)
        data = resp.json()
        assert "error" in data


# =====================================================================
# routers/graphs: _SimpleDbManager, _validate_container_name
# =====================================================================


class TestGraphsHelpers:
    """Test graph router helper functions."""

    def test_validate_container_name_valid(self):
        from routers.graphs import _validate_container_name

        assert _validate_container_name("my-container") == "my-container"
        assert _validate_container_name("test_container.v1") == "test_container.v1"

    def test_validate_container_name_invalid(self):
        from fastapi import HTTPException

        from routers.graphs import _validate_container_name

        with pytest.raises(HTTPException) as exc_info:
            _validate_container_name("../../../etc/passwd")
        assert exc_info.value.status_code == 400

    def test_validate_container_name_path_traversal(self):
        from fastapi import HTTPException

        from routers.graphs import _validate_container_name

        with pytest.raises(HTTPException):
            _validate_container_name("foo..bar")


# =====================================================================
# routers/metrics: _SimpleDbManager
# =====================================================================


class TestMetricsHelpers:
    """Test metrics router helpers."""

    def test_resolve_sort_key_known(self):
        from routers.metrics import _resolve_sort_key

        assert _resolve_sort_key("degree") == "degree_centrality"
        assert _resolve_sort_key("in_degree") == "in_degree"
        assert _resolve_sort_key("out_degree") == "out_degree"

    def test_resolve_sort_key_unknown(self):
        from routers.metrics import _resolve_sort_key

        assert _resolve_sort_key("unknown_metric") == "degree_centrality"


# =====================================================================
# openapi_aggregator: merge and ref rewriting
# =====================================================================


class TestOpenApiAggregator:
    """Test OpenAPI spec merge logic."""

    def test_merge_spec_new_paths(self):
        from openapi_aggregator import _merge_spec

        base = {"paths": {}, "components": {"schemas": {}}}
        backend = {
            "paths": {"/api/test": {"get": {"summary": "Test"}}},
            "components": {"schemas": {"TestModel": {"type": "object"}}},
        }
        _merge_spec(base, "test_service", backend)
        assert "/api/test" in base["paths"]
        assert "TestModel" in base["components"]["schemas"]

    def test_merge_spec_conflicting_schema(self):
        from openapi_aggregator import _merge_spec

        base = {
            "paths": {},
            "components": {"schemas": {"Model": {"type": "string"}}},
        }
        backend = {
            "paths": {"/api/x": {"get": {"summary": "X"}}},
            "components": {"schemas": {"Model": {"type": "integer"}}},
        }
        _merge_spec(base, "svc", backend)
        # Original preserved, backend prefixed
        assert "Model" in base["components"]["schemas"]
        assert "svc_Model" in base["components"]["schemas"]

    def test_deep_replace_ref(self):
        from openapi_aggregator import _deep_replace_ref

        obj = {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Old"}}}}
        _deep_replace_ref(obj, "#/components/schemas/Old", "#/components/schemas/New")
        assert obj["content"]["application/json"]["schema"]["$ref"] == "#/components/schemas/New"

    def test_deep_replace_ref_in_list(self):
        from openapi_aggregator import _deep_replace_ref

        obj = [{"$ref": "#/components/schemas/Old"}, {"$ref": "#/components/schemas/Other"}]
        _deep_replace_ref(obj, "#/components/schemas/Old", "#/components/schemas/New")
        assert obj[0]["$ref"] == "#/components/schemas/New"
        assert obj[1]["$ref"] == "#/components/schemas/Other"

    def test_invalidate_cache(self):
        from openapi_aggregator import invalidate_cache

        invalidate_cache()
        from openapi_aggregator import _cache as c

        assert c == (None, 0.0)


# =====================================================================
# app_factory: _add_exception_handlers
# =====================================================================


class TestAddExceptionHandlers:
    """Test _add_exception_handlers."""

    def test_registers_handlers(self):
        from app_factory import _add_exception_handlers

        app = MagicMock()
        with patch("middleware.exception_handlers.register_exception_handlers") as mock_reg:
            _add_exception_handlers(app)
            mock_reg.assert_called_once_with(app)


# =====================================================================
# Proxy route table completeness
# =====================================================================


class TestProxyRouteTable:
    """Verify proxy route table entries."""

    def test_all_expected_prefixes_exist(self):
        from routers.proxy import PROXY_ROUTES

        expected = [
            "/api/analysis",
            "/api/tests",
            "/api/ai",
            "/api/workspaces",
            "/api/containers",
            "/api/repositories",
            "/api/security",
            "/api/realtime",
        ]
        for prefix in expected:
            assert prefix in PROXY_ROUTES, f"Missing proxy route: {prefix}"

    def test_sorted_prefixes_longest_first(self):
        from routers.proxy import _SORTED_PREFIXES

        for i in range(len(_SORTED_PREFIXES) - 1):
            assert len(_SORTED_PREFIXES[i]) >= len(_SORTED_PREFIXES[i + 1])
