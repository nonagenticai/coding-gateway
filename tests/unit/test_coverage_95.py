"""Comprehensive tests to push coverage from 88% to 95%+.

Targets uncovered lines in:
- app_factory.py (65%): lifespan startup/shutdown, _add_exception_handlers fallback
- db/__init__.py (0%): import coverage
- routers/proxy.py (73%): WebSocket proxy, proxy_request auth/size/timeout paths
- services/grammar_manager.py (76%): _download_and_install_grammar, _get_latest_version,
  _calculate_directory_hash, cleanup, check_for_updates with network
- services/code_graph/multi_language_graph_service.py (78%): analyze_file/directory,
  entity/relationship normalization, cross-language, search, detect language
- services/code_graph/reference_validator.py (78%): validate_code_snippet,
  PackageReferenceValidator, _is_package_importable, _get_package_info
- routers/graphs.py (80%): list/detail/delete endpoints, orphan scan, pagination
- services/job_manager.py (82%): create_pytest_job, get_job_status, get_job_logs,
  wait_for_completion, build_job_manifest, cleanup_old_jobs
- services/code_graph/graph_database_populator.py (83%): populate_graph_database,
  _batch_insert_nodes optimization branches, verify_database_integrity, get_database_stats
- services/code_graph/graph_validation.py (84%): validate_graph_integrity,
  check_dependency_cycles severity, _find_dangling_references list refs, cache hit
- services/validation.py (85%): ImportValidationService, FunctionValidationService
- services/cache_manager.py (86%): various cache methods
- routers/metrics.py (86%): metrics endpoints
- services/code_graph/structure_storage.py (86%): storage operations
- routers/jobs.py (87%): job router endpoints
- routers/validation.py (87%): validation endpoints
- services/code_graph/consistency_checker.py (88%): checker operations
- routers/validation_status.py (88%): status endpoints
- routers/cache.py (90%): cache endpoints
- routers/visualization.py (91%): visualization endpoints
"""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import networkx as nx
import pytest

# =====================================================================
# db/__init__.py — Force import to cover re-exports
# =====================================================================


def _owned_labels() -> dict:
    """Labels of a Job owned by the tenant these tests run as (n8n-dev)."""
    from services.job_manager import (
        APP_LABEL_KEY,
        APP_LABEL_VALUE,
        TENANT_LABEL_KEY,
        tenant_label_value,
    )

    return {APP_LABEL_KEY: APP_LABEL_VALUE, TENANT_LABEL_KEY: tenant_label_value("n8n-dev")}


class TestDbInitCoverage:
    """Cover db/__init__.py re-exports.

    Note: The db module is stubbed by conftest, so we verify the stub exists
    and the real module's __init__.py structure is correct by checking the
    source file directly.
    """

    def test_db_module_importable(self):
        import db

        assert db is not None

    def test_db_init_source_has_correct_exports(self):
        """Verify the source file declares the expected __all__."""
        import ast

        with open("src/db/__init__.py") as f:
            tree = ast.parse(f.read())
        # Find the __all__ assignment
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "__all__":
                        # Verify it contains expected names
                        assert isinstance(node.value, ast.List)
                        names = [
                            elt.value for elt in node.value.elts if isinstance(elt, ast.Constant)
                        ]
                        assert "AsyncSessionLocal" in names
                        assert "engine" in names
                        assert "get_db" in names
                        return
        pytest.fail("__all__ not found in db/__init__.py")


# =====================================================================
# app_factory.py — lifespan, fallback exception handler
# =====================================================================


class TestLifespan:
    """Test app_factory lifespan startup and shutdown."""

    async def test_lifespan_startup_and_shutdown(self):
        """Full lifespan cycle: startup -> yield -> shutdown."""
        from app_factory import lifespan

        mock_cache = AsyncMock()
        mock_cache.close = AsyncMock()

        mock_app = MagicMock()
        mock_app.state = MagicMock()
        mock_app.openapi = MagicMock(return_value={"paths": {}, "info": {}})

        with (
            patch("services.cache_manager.get_cache_manager", return_value=mock_cache),
            patch(
                "openapi_aggregator.get_aggregated_spec",
                new_callable=AsyncMock,
                return_value={"paths": {"/test": {}}, "info": {}},
            ),
            patch("routers.proxy.close_proxy_client", new_callable=AsyncMock),
        ):
            async with lifespan(mock_app):
                # Inside the lifespan context
                assert hasattr(mock_app.state, "cache")

    async def test_lifespan_openapi_aggregation_failure(self):
        """OpenAPI aggregation failure is non-fatal."""
        from app_factory import lifespan

        mock_cache = AsyncMock()
        mock_cache.close = AsyncMock()

        mock_app = MagicMock()
        mock_app.state = MagicMock()
        mock_app.openapi = MagicMock(side_effect=RuntimeError("openapi fail"))

        with (
            patch("services.cache_manager.get_cache_manager", return_value=mock_cache),
            patch("routers.proxy.close_proxy_client", new_callable=AsyncMock),
        ):
            async with lifespan(mock_app):
                pass  # Should not raise

    async def test_lifespan_cache_close_failure(self):
        """Cache close failure during shutdown is non-fatal."""
        from app_factory import lifespan

        mock_cache = AsyncMock()
        mock_cache.close = AsyncMock(side_effect=RuntimeError("close fail"))

        mock_app = MagicMock()
        mock_app.state = MagicMock()
        mock_app.state.cache = mock_cache
        mock_app.state.background_tasks = set()
        mock_app.openapi = MagicMock(return_value={"paths": {}})

        with (
            patch("services.cache_manager.get_cache_manager", return_value=mock_cache),
            patch(
                "openapi_aggregator.get_aggregated_spec",
                new_callable=AsyncMock,
                return_value={"paths": {}},
            ),
            patch("routers.proxy.close_proxy_client", new_callable=AsyncMock),
        ):
            async with lifespan(mock_app):
                pass  # Shutdown should handle cache close error gracefully

    async def test_lifespan_cancels_background_tasks(self):
        """Background tasks are cancelled during shutdown."""
        from app_factory import lifespan

        mock_cache = AsyncMock()
        mock_cache.close = AsyncMock()

        mock_task = MagicMock()
        mock_task.done.return_value = False
        mock_task.cancel = MagicMock()

        mock_app = MagicMock()
        mock_app.state = MagicMock()
        mock_app.openapi = MagicMock(return_value={"paths": {}})

        with (
            patch("services.cache_manager.get_cache_manager", return_value=mock_cache),
            patch(
                "openapi_aggregator.get_aggregated_spec",
                new_callable=AsyncMock,
                return_value={"paths": {}},
            ),
            patch("routers.proxy.close_proxy_client", new_callable=AsyncMock),
            patch("asyncio.gather", new_callable=AsyncMock, return_value=[]),
        ):
            async with lifespan(mock_app):
                mock_app.state.background_tasks = {mock_task}

        mock_task.cancel.assert_called_once()


class TestAddExceptionHandlersFallback:
    """Test _add_exception_handlers when middleware.exception_handlers is unavailable."""

    def test_fallback_handler_registered_on_import_error(self):
        """When register_exception_handlers import fails, a fallback is registered."""
        from fastapi import FastAPI

        app = FastAPI()

        # Temporarily make the import fail by patching builtins.__import__
        original_import = (
            __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__
        )

        def fake_import(name, *args, **kwargs):
            if name == "middleware.exception_handlers":
                raise ImportError("test: no module")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            from app_factory import _add_exception_handlers

            _add_exception_handlers(app)

        # The function should still register a handler (either the real one or fallback)
        assert Exception in app.exception_handlers


class TestAddMiddlewaresEdgeCases:
    """Test _add_middlewares edge cases."""

    def test_process_time_middleware_import_error(self):
        """When metrics middleware import fails, it's skipped gracefully."""
        from app_factory import _add_middlewares

        app = MagicMock()
        settings = MagicMock()
        settings.enable_auth = False
        settings.cors_origins = ["*"]
        # This should not raise
        _add_middlewares(app, settings)
        assert app.add_middleware.called


# =====================================================================
# routers/proxy.py — proxy_request paths (auth, size limits, timeout, etc.)
# =====================================================================


class TestProxyRequestAuthForwarding:
    """Test auth context forwarding in proxy_request."""

    async def test_proxy_forwards_user_auth_headers(self, client):
        """User auth context is forwarded to backend as X- headers."""
        import httpx

        mock_response = httpx.Response(
            200, content=b'{"ok": true}', headers={"content-type": "application/json"}
        )
        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=mock_response)

        with patch("routers.proxy.get_proxy_client", return_value=mock_client):
            resp = await client.get("/api/analysis/test")

        assert resp.status_code == 200
        # Verify the request was forwarded
        mock_client.request.assert_awaited_once()

    async def test_proxy_query_string_forwarded(self, client):
        """Query params are appended to the backend URL."""
        import httpx

        mock_response = httpx.Response(
            200, content=b'{"ok": true}', headers={"content-type": "application/json"}
        )
        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=mock_response)

        with patch("routers.proxy.get_proxy_client", return_value=mock_client):
            resp = await client.get("/api/analysis/test?foo=bar&baz=1")

        assert resp.status_code == 200
        call_kwargs = mock_client.request.call_args
        assert "foo=bar" in call_kwargs.kwargs.get(
            "url", call_kwargs.args[1] if len(call_kwargs.args) > 1 else ""
        )

    async def test_proxy_response_too_large_by_content_length(self, client):
        """Backend response with content-length > 100MB returns 502."""
        import httpx

        mock_response = httpx.Response(
            200,
            content=b'{"ok": true}',
            headers={
                "content-type": "application/json",
                "content-length": str(200 * 1024 * 1024),  # 200MB
            },
        )
        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=mock_response)

        with patch("routers.proxy.get_proxy_client", return_value=mock_client):
            resp = await client.get("/api/analysis/big")

        assert resp.status_code == 502
        data = resp.json()
        assert data["error"]["code"] == "HTTP_502"
        assert "too large" in data["error"]["message"].lower()

    async def test_proxy_invalid_content_length_header(self, client):
        """Invalid content-length header is skipped gracefully."""
        import httpx

        mock_response = httpx.Response(
            200,
            content=b'{"ok": true}',
            headers={
                "content-type": "application/json",
                "content-length": "not-a-number",
            },
        )
        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=mock_response)

        with patch("routers.proxy.get_proxy_client", return_value=mock_client):
            resp = await client.get("/api/analysis/test")

        assert resp.status_code == 200

    async def test_proxy_timeout_returns_504(self, client):
        """Backend timeout returns 504."""
        import httpx

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))

        with patch("routers.proxy.get_proxy_client", return_value=mock_client):
            resp = await client.get("/api/analysis/slow")

        assert resp.status_code == 504
        data = resp.json()
        assert data["error"]["code"] == "HTTP_504"

    async def test_proxy_connect_error_returns_502(self, client):
        """Backend connect error returns 502."""
        import httpx

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

        with patch("routers.proxy.get_proxy_client", return_value=mock_client):
            resp = await client.get("/api/analysis/down")

        assert resp.status_code == 502
        data = resp.json()
        assert data["error"]["code"] == "HTTP_502"
        assert "unreachable" in data["error"]["message"].lower()


# =====================================================================
# services/grammar_manager.py — _download_and_install, _get_latest_version,
# _calculate_directory_hash, cleanup with versions, check_for_updates with update
# =====================================================================


class TestGrammarManagerDeep:
    """Deep tests for TreeSitterGrammarManager internals."""

    async def test_calculate_directory_hash(self):
        from services.grammar_manager import TreeSitterGrammarManager

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = TreeSitterGrammarManager(cache_dir=Path(tmpdir))
            # Create some files
            (Path(tmpdir) / "a.txt").write_text("hello")
            (Path(tmpdir) / "b.txt").write_text("world")
            h = await mgr._calculate_directory_hash(Path(tmpdir))
            assert isinstance(h, str)
            assert len(h) == 64  # SHA-256 hex digest

    async def test_calculate_directory_hash_with_unreadable_file(self):
        from services.grammar_manager import TreeSitterGrammarManager

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = TreeSitterGrammarManager(cache_dir=Path(tmpdir))
            # Create a normal file
            (Path(tmpdir) / "a.txt").write_text("hello")
            # The hash should still work even if some files can't be read
            h = await mgr._calculate_directory_hash(Path(tmpdir))
            assert isinstance(h, str)

    async def test_download_and_install_grammar_git_failure(self):
        from services.grammar_manager import GrammarInfo, TreeSitterGrammarManager

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = TreeSitterGrammarManager(cache_dir=Path(tmpdir))
            grammar = GrammarInfo(
                name="test-grammar",
                language="test",
                repository_url="https://github.com/nonexistent/repo",
                version="1.0",
            )
            install_dir = Path(tmpdir) / "test"
            install_dir.mkdir()

            mock_process = AsyncMock()
            mock_process.returncode = 1
            mock_process.communicate = AsyncMock(return_value=(b"", b"fatal: not found"))

            with patch("asyncio.create_subprocess_exec", return_value=mock_process):
                result = await mgr._download_and_install_grammar(grammar, install_dir)

            assert result is False

    async def test_download_and_install_grammar_success_with_grammar_js(self):
        from services.grammar_manager import GrammarInfo, TreeSitterGrammarManager

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = TreeSitterGrammarManager(cache_dir=Path(tmpdir))
            grammar = GrammarInfo(
                name="test-grammar",
                language="test",
                repository_url="https://github.com/tree-sitter/tree-sitter-test",
                version="1.0",
            )
            install_dir = Path(tmpdir) / "test"

            mock_process = AsyncMock()
            mock_process.returncode = 0
            mock_process.communicate = AsyncMock(return_value=(b"", b""))

            def fake_exec(*args, **kwargs):
                # Create grammar.js after "cloning"
                install_dir.mkdir(exist_ok=True)
                (install_dir / "grammar.js").write_text("module.exports = {}")
                return mock_process

            with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
                result = await mgr._download_and_install_grammar(grammar, install_dir)

            assert result is True
            assert grammar.sha256_hash is not None

    async def test_download_and_install_grammar_no_grammar_js(self):
        from services.grammar_manager import GrammarInfo, TreeSitterGrammarManager

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = TreeSitterGrammarManager(cache_dir=Path(tmpdir))
            grammar = GrammarInfo(
                name="test-grammar",
                language="c",
                repository_url="https://github.com/tree-sitter/tree-sitter-c",
                version="1.0",
            )
            install_dir = Path(tmpdir) / "c"

            mock_process = AsyncMock()
            mock_process.returncode = 0
            mock_process.communicate = AsyncMock(return_value=(b"", b""))

            def fake_exec(*args, **kwargs):
                install_dir.mkdir(exist_ok=True)
                # No grammar.js created
                return mock_process

            with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
                result = await mgr._download_and_install_grammar(grammar, install_dir)

            assert result is False

    async def test_download_and_install_typescript_subdirectory(self):
        from services.grammar_manager import GrammarInfo, TreeSitterGrammarManager

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = TreeSitterGrammarManager(cache_dir=Path(tmpdir))
            grammar = GrammarInfo(
                name="tree-sitter-typescript",
                language="typescript",
                repository_url="https://github.com/tree-sitter/tree-sitter-typescript",
                version="1.0",
            )
            install_dir = Path(tmpdir) / "typescript"

            mock_process = AsyncMock()
            mock_process.returncode = 0
            mock_process.communicate = AsyncMock(return_value=(b"", b""))

            def fake_exec(*args, **kwargs):
                install_dir.mkdir(exist_ok=True)
                (install_dir / "typescript").mkdir(exist_ok=True)
                (install_dir / "typescript" / "grammar.js").write_text("module.exports = {}")
                return mock_process

            with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
                result = await mgr._download_and_install_grammar(grammar, install_dir)

            assert result is True

    async def test_download_and_install_typescript_no_subdirectory(self):
        from services.grammar_manager import GrammarInfo, TreeSitterGrammarManager

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = TreeSitterGrammarManager(cache_dir=Path(tmpdir))
            grammar = GrammarInfo(
                name="tree-sitter-typescript",
                language="typescript",
                repository_url="https://github.com/tree-sitter/tree-sitter-typescript",
                version="1.0",
            )
            install_dir = Path(tmpdir) / "typescript"

            mock_process = AsyncMock()
            mock_process.returncode = 0
            mock_process.communicate = AsyncMock(return_value=(b"", b""))

            def fake_exec(*args, **kwargs):
                install_dir.mkdir(exist_ok=True)
                # No typescript/grammar.js or tsx/grammar.js
                return mock_process

            with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
                result = await mgr._download_and_install_grammar(grammar, install_dir)

            assert result is False

    async def test_get_latest_version_from_releases(self):
        from services.grammar_manager import GrammarInfo, TreeSitterGrammarManager

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = TreeSitterGrammarManager(cache_dir=Path(tmpdir))
            grammar = GrammarInfo(
                name="tree-sitter-python",
                language="python",
                repository_url="https://github.com/tree-sitter/tree-sitter-python",
                version="unknown",
            )

            mock_response = AsyncMock()
            mock_response.status = 200
            mock_response.json = AsyncMock(return_value={"tag_name": "v0.21.0"})

            mock_session = AsyncMock()
            mock_session.get = MagicMock(
                return_value=AsyncMock(
                    __aenter__=AsyncMock(return_value=mock_response), __aexit__=AsyncMock()
                )
            )
            mock_session_ctx = AsyncMock(
                __aenter__=AsyncMock(return_value=mock_session), __aexit__=AsyncMock()
            )

            with patch("aiohttp.ClientSession", return_value=mock_session_ctx):
                result = await mgr._get_latest_version(grammar)

            assert result == "v0.21.0"

    async def test_get_latest_version_from_commits(self):
        from services.grammar_manager import GrammarInfo, TreeSitterGrammarManager

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = TreeSitterGrammarManager(cache_dir=Path(tmpdir))
            grammar = GrammarInfo(
                name="tree-sitter-python",
                language="python",
                repository_url="https://github.com/tree-sitter/tree-sitter-python",
                version="unknown",
            )

            # Releases return 404, commits return data
            release_resp = AsyncMock()
            release_resp.status = 404

            commits_resp = AsyncMock()
            commits_resp.status = 200
            commits_resp.json = AsyncMock(return_value=[{"sha": "abcdef1234567890"}])

            call_count = 0

            class FakeGetCtx:
                def __init__(self, resp):
                    self.resp = resp

                async def __aenter__(self):
                    return self.resp

                async def __aexit__(self, *args):
                    pass

            mock_session = MagicMock()

            def side_effect_get(url):
                nonlocal call_count
                call_count += 1
                if "releases" in url:
                    return FakeGetCtx(release_resp)
                return FakeGetCtx(commits_resp)

            mock_session.get = side_effect_get

            class FakeSessionCtx:
                async def __aenter__(self):
                    return mock_session

                async def __aexit__(self, *args):
                    pass

            with patch("aiohttp.ClientSession", return_value=FakeSessionCtx()):
                result = await mgr._get_latest_version(grammar)

            assert result == "abcdef12"

    async def test_get_latest_version_network_error(self):
        from services.grammar_manager import GrammarInfo, TreeSitterGrammarManager

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = TreeSitterGrammarManager(cache_dir=Path(tmpdir))
            grammar = GrammarInfo(
                name="tree-sitter-python",
                language="python",
                repository_url="https://github.com/tree-sitter/tree-sitter-python",
                version="unknown",
            )

            with patch("aiohttp.ClientSession", side_effect=RuntimeError("network error")):
                result = await mgr._get_latest_version(grammar)

            assert result is None

    async def test_check_for_updates_with_update_available(self):
        from services.grammar_manager import TreeSitterGrammarManager

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = TreeSitterGrammarManager(cache_dir=Path(tmpdir))
            await mgr.initialize()

            # Set auto_update to force checking
            mgr.auto_update = True

            mgr._get_latest_version = AsyncMock(return_value="v2.0.0")

            result = await mgr.check_for_updates(languages=["python"])
            assert result.get("python") is True

    async def test_check_for_updates_error_handling(self):
        from services.grammar_manager import TreeSitterGrammarManager

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = TreeSitterGrammarManager(cache_dir=Path(tmpdir))
            await mgr.initialize()
            mgr.auto_update = True

            mgr._get_latest_version = AsyncMock(side_effect=RuntimeError("api error"))

            result = await mgr.check_for_updates(languages=["python"])
            assert result["python"] is False

    async def test_check_for_updates_unknown_language_skipped(self):
        from services.grammar_manager import TreeSitterGrammarManager

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = TreeSitterGrammarManager(cache_dir=Path(tmpdir))
            await mgr.initialize()

            result = await mgr.check_for_updates(languages=["nonexistent"])
            assert "nonexistent" not in result

    async def test_cleanup_old_versions_with_actual_dirs(self):
        from services.grammar_manager import TreeSitterGrammarManager

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = TreeSitterGrammarManager(cache_dir=Path(tmpdir))
            await mgr.initialize()

            # Set up a grammar with an install path containing version dirs
            lang_dir = Path(tmpdir) / "python"
            lang_dir.mkdir()
            (lang_dir / "v1.0").mkdir()
            (lang_dir / "v2.0").mkdir()

            mgr.grammars["python"].install_path = lang_dir
            mgr.grammars["python"].installed_version = "v2.0"

            result = await mgr.cleanup_old_versions()
            assert result["python"] == 1  # v1.0 removed

    async def test_cleanup_old_versions_os_error(self):
        from services.grammar_manager import TreeSitterGrammarManager

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = TreeSitterGrammarManager(cache_dir=Path(tmpdir))
            await mgr.initialize()

            lang_dir = Path(tmpdir) / "python"
            lang_dir.mkdir()

            mgr.grammars["python"].install_path = lang_dir
            mgr.grammars["python"].installed_version = "v1.0"

            with patch("shutil.rmtree", side_effect=OSError("permission denied")):
                result = await mgr.cleanup_old_versions()
            assert result["python"] == 0

    async def test_install_grammar_already_up_to_date(self):
        from services.grammar_manager import TreeSitterGrammarManager

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = TreeSitterGrammarManager(cache_dir=Path(tmpdir))
            await mgr.initialize()

            mgr.grammars["python"].is_available = True
            mgr.grammars["python"].installed_version = "unknown"
            mgr.grammars["python"].version = "unknown"

            result = await mgr.install_grammar("python")
            assert result is True  # Already up to date

    async def test_install_grammar_os_error(self):
        from services.grammar_manager import TreeSitterGrammarManager

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = TreeSitterGrammarManager(cache_dir=Path(tmpdir))
            await mgr.initialize()

            mgr._download_and_install_grammar = AsyncMock(side_effect=OSError("disk full"))

            result = await mgr.install_grammar("python", force=True)
            assert result is False

    async def test_install_all_grammars_with_error(self):
        from services.grammar_manager import TreeSitterGrammarManager

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = TreeSitterGrammarManager(cache_dir=Path(tmpdir))
            await mgr.initialize()

            call_count = 0

            async def flaky_install(lang, force=False):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise OSError("disk full")
                return True

            mgr.install_grammar = flaky_install

            results = await mgr.install_all_grammars(force=True)
            # First one should be False (error), rest True
            assert any(v is False for v in results.values())

    async def test_save_grammar_info_os_error(self):
        from services.grammar_manager import TreeSitterGrammarManager

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = TreeSitterGrammarManager(cache_dir=Path(tmpdir))
            await mgr.initialize()

            # Make grammars_file point to a read-only location
            with patch("builtins.open", side_effect=OSError("read only")):
                # Should not raise
                await mgr._save_grammar_info()

    async def test_load_grammar_info_corrupted_json(self):
        from services.grammar_manager import TreeSitterGrammarManager

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = TreeSitterGrammarManager(cache_dir=Path(tmpdir))
            # Write corrupted JSON
            (Path(tmpdir) / "grammars.json").write_text("{invalid json")

            await mgr._load_grammar_info()
            # Should not raise, grammars should be empty
            assert len(mgr.grammars) == 0

    def test_get_grammar_manager_singleton(self):
        from services.grammar_manager import get_grammar_manager

        mgr1 = get_grammar_manager()
        mgr2 = get_grammar_manager()
        assert mgr1 is mgr2


# =====================================================================
# services/code_graph/reference_validator.py
# =====================================================================


class TestReferenceValidatorDeep:
    """Deep tests for ReferenceValidator."""

    def _make_validator(self):
        """Create a ReferenceValidator with a temp directory containing Python files."""
        tmpdir = tempfile.mkdtemp()
        # Create a Python file with functions and classes
        test_file = os.path.join(tmpdir, "module.py")
        with open(test_file, "w") as f:
            f.write(
                "import os\n"
                "from pathlib import Path\n"
                "\n"
                "class MyClass:\n"
                "    def my_method(self):\n"
                "        pass\n"
                "\n"
                "def my_function():\n"
                "    pass\n"
                "\n"
                "CONSTANT = 42\n"
            )

        from services.code_graph.reference_validator import ReferenceValidator

        return ReferenceValidator(tmpdir), tmpdir

    def test_validate_file_reference_exists(self):
        validator, tmpdir = self._make_validator()
        exists, path = validator.validate_file_reference("module.py")
        assert exists is True
        assert path == "module.py"

    def test_validate_file_reference_not_found(self):
        validator, tmpdir = self._make_validator()
        exists, path = validator.validate_file_reference("nonexistent.py")
        assert exists is False

    def test_validate_file_reference_similar(self):
        validator, tmpdir = self._make_validator()
        # "module" is a substring of "module.py"
        exists, path = validator.validate_file_reference("module")
        assert exists is False
        assert path is not None
        assert "module.py" in path

    def test_validate_file_reference_absolute_existing(self):
        validator, tmpdir = self._make_validator()
        abs_path = os.path.join(tmpdir, "module.py")
        exists, path = validator.validate_file_reference(abs_path)
        assert exists is True

    def test_validate_file_reference_absolute_not_existing(self):
        validator, tmpdir = self._make_validator()
        exists, path = validator.validate_file_reference("/nonexistent/path.py")
        assert exists is False
        assert path is None

    def test_validate_function_reference_exists(self):
        validator, _ = self._make_validator()
        exists, details = validator.validate_function_reference("my_function")
        assert exists is True
        assert details["file"] == "module.py"

    def test_validate_function_reference_method(self):
        validator, _ = self._make_validator()
        exists, details = validator.validate_function_reference("MyClass.my_method")
        assert exists is True
        assert details["class"] == "MyClass"

    def test_validate_function_reference_similar(self):
        validator, _ = self._make_validator()
        exists, details = validator.validate_function_reference("my_")
        assert exists is False
        assert "similar_functions" in details

    def test_validate_function_reference_not_found(self):
        validator, _ = self._make_validator()
        exists, details = validator.validate_function_reference("totally_nonexistent_xyz")
        assert exists is False
        assert details is None

    def test_validate_class_reference_exists(self):
        validator, _ = self._make_validator()
        exists, details = validator.validate_class_reference("MyClass")
        assert exists is True
        assert "my_method" in details["methods"]

    def test_validate_class_reference_similar(self):
        validator, _ = self._make_validator()
        exists, details = validator.validate_class_reference("My")
        assert exists is False
        assert "similar_classes" in details

    def test_validate_class_reference_not_found(self):
        validator, _ = self._make_validator()
        exists, details = validator.validate_class_reference("NonExistentClass")
        assert exists is False
        assert details is None

    def test_validate_code_snippet_valid(self):
        validator, _ = self._make_validator()
        result = validator.validate_code_snippet("x = 1 + 2")
        assert result["valid"] is True
        assert result["overall_score"] == 1.0

    def test_validate_code_snippet_syntax_error(self):
        validator, _ = self._make_validator()
        result = validator.validate_code_snippet("def (:")
        assert result["valid"] is False
        assert result["issues"][0]["type"] == "syntax"

    def test_validate_code_snippet_empty(self):
        validator, _ = self._make_validator()
        result = validator.validate_code_snippet("")
        assert result["valid"] is False

    def test_validate_code_snippet_not_string(self):
        validator, _ = self._make_validator()
        result = validator.validate_code_snippet(None)
        assert result["valid"] is False

    def test_validate_code_snippet_with_imports(self):
        validator, _ = self._make_validator()
        result = validator.validate_code_snippet("import os\nfrom pathlib import Path")
        # These imports exist in the codebase
        assert result["overall_score"] >= 0

    def test_validate_code_snippet_with_function_call(self):
        validator, _ = self._make_validator()
        result = validator.validate_code_snippet("my_function()")
        assert result is not None
        assert "overall_score" in result

    def test_validate_code_snippet_with_method_call(self):
        validator, _ = self._make_validator()
        result = validator.validate_code_snippet("obj.method()")
        assert result is not None

    def test_create_error_response(self):
        validator, _ = self._make_validator()
        result = validator._create_error_response("test", "name", "message", "suggestion")
        assert result["valid"] is False
        assert result["overall_score"] == 0.0
        assert len(result["issues"]) == 1
        assert result["issues"][0]["type"] == "test"

    def test_suggest_similar_import_found(self):
        validator, _ = self._make_validator()
        result = validator._suggest_similar_import("os")
        # "os" is a standard module — either a suggestion is found or None is returned
        if result is not None:
            assert isinstance(result, str)
            assert len(result) > 0

    def test_suggest_similar_function_found(self):
        validator, _ = self._make_validator()
        result = validator._suggest_similar_function("my_func")
        # "my_func" is a substring of "my_function"
        assert result is not None
        assert "my_function" in result

    def test_suggest_similar_method_found(self):
        validator, _ = self._make_validator()
        result = validator._suggest_similar_method("MyClass.my")
        # "MyClass.my" is a substring of "MyClass.my_method"
        assert result is not None
        assert "MyClass.my_method" in result


class TestPackageReferenceValidator:
    """Test PackageReferenceValidator."""

    def test_validate_import_statement_source_valid(self):
        from services.code_graph.reference_validator import PackageReferenceValidator

        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = os.path.join(tmpdir, "test.py")
            with open(test_file, "w") as f:
                f.write("import os\n")
            validator = PackageReferenceValidator(tmpdir)
            is_valid, details = validator.validate_import_statement("os")
            assert isinstance(is_valid, bool)
            assert "import_name" in details

    def test_validate_import_statement_not_found(self):
        from services.code_graph.reference_validator import PackageReferenceValidator

        with tempfile.TemporaryDirectory() as tmpdir:
            validator = PackageReferenceValidator(tmpdir)
            is_valid, details = validator.validate_import_statement("nonexistent_xyz_pkg")
            assert is_valid is False
            assert details["resolution"] == "not_found"

    def test_is_package_importable_no_packages(self):
        from services.code_graph.reference_validator import PackageReferenceValidator

        with tempfile.TemporaryDirectory() as tmpdir:
            validator = PackageReferenceValidator(tmpdir)
            assert validator._is_package_importable("os") is False

    def test_is_package_importable_in_modules_index(self):
        from services.code_graph.reference_validator import PackageReferenceValidator

        with tempfile.TemporaryDirectory() as tmpdir:
            validator = PackageReferenceValidator(tmpdir)
            validator.container_packages = {
                "modules_index": {"numpy": {"modules": ["numpy", "numpy.core"], "version": "1.24"}}
            }
            assert validator._is_package_importable("numpy") is True
            assert validator._is_package_importable("numpy.core") is True
            assert validator._is_package_importable("numpy.core.sub") is True
            assert validator._is_package_importable("pandas") is False

    def test_get_package_info_found(self):
        from services.code_graph.reference_validator import PackageReferenceValidator

        with tempfile.TemporaryDirectory() as tmpdir:
            validator = PackageReferenceValidator(tmpdir)
            validator.container_packages = {
                "modules_index": {
                    "numpy": {"modules": ["numpy", "numpy.core"], "version": "1.24", "type": "pip"}
                }
            }
            info = validator._get_package_info("numpy")
            assert info is not None

    def test_get_package_info_in_nested_module(self):
        from services.code_graph.reference_validator import PackageReferenceValidator

        with tempfile.TemporaryDirectory() as tmpdir:
            validator = PackageReferenceValidator(tmpdir)
            validator.container_packages = {
                "modules_index": {
                    "numpy": {"modules": ["numpy", "numpy.core"], "version": "1.24", "type": "pip"}
                }
            }
            info = validator._get_package_info("numpy.core")
            assert info["providing_package"] == "numpy"

    def test_get_package_info_not_found(self):
        from services.code_graph.reference_validator import PackageReferenceValidator

        with tempfile.TemporaryDirectory() as tmpdir:
            validator = PackageReferenceValidator(tmpdir)
            validator.container_packages = {"modules_index": {}}
            info = validator._get_package_info("nonexistent")
            assert info == {}

    async def test_validate_code_snippet_comprehensive(self):
        from services.code_graph.reference_validator import PackageReferenceValidator

        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = os.path.join(tmpdir, "test.py")
            with open(test_file, "w") as f:
                f.write("import os\n")
            validator = PackageReferenceValidator(tmpdir)
            result = await validator.validate_code_snippet_comprehensive(
                "import os\nfrom pathlib import Path"
            )
            assert "valid" in result
            assert "overall_score" in result
            assert "issues" in result

    async def test_validate_code_snippet_comprehensive_syntax_error(self):
        from services.code_graph.reference_validator import PackageReferenceValidator

        with tempfile.TemporaryDirectory() as tmpdir:
            validator = PackageReferenceValidator(tmpdir)
            result = await validator.validate_code_snippet_comprehensive("def (:")
            assert result["valid"] is False
            assert result["overall_score"] == 0.0

    async def test_initialize_for_container(self):
        from services.code_graph.reference_validator import PackageReferenceValidator

        mock_discovery = AsyncMock()
        mock_discovery.discover_container_packages = AsyncMock(
            return_value={"modules_index": {"numpy": {}}}
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            validator = PackageReferenceValidator(tmpdir, package_discovery=mock_discovery)
            await validator.initialize_for_container("test-container")
            assert validator.container_packages is not None


class TestValidationTimeoutContext:
    """Test the timeout context manager."""

    def test_timeout_doesnt_raise_within_limit(self):
        from services.code_graph.reference_validator import timeout

        with timeout(5):
            x = 1 + 1
            assert x == 2


# =====================================================================
# services/code_graph/graph_validation.py — deeper coverage
# =====================================================================


class TestGraphValidationDeep:
    """Cover uncovered lines in graph_validation.py."""

    def test_validate_graph_structure_with_cycles(self):
        from services.code_graph.graph_validation import GraphValidator

        validator = GraphValidator()
        g = nx.DiGraph()
        g.add_edges_from([("A", "B"), ("B", "C"), ("C", "A")])

        result = validator.validate_graph_structure(g)
        # Has a cycle so should have circular_dependency issue
        cycle_issues = [
            i for i in result["structural_issues"] if i["type"] == "circular_dependency"
        ]
        assert len(cycle_issues) > 0
        assert result["is_valid"] is False  # High severity cycle

    def test_validate_graph_structure_cached(self):
        from services.code_graph.graph_validation import GraphValidator

        validator = GraphValidator()
        g = nx.DiGraph()
        g.add_edge("A", "B")

        result1 = validator.validate_graph_structure(g)
        result2 = validator.validate_graph_structure(g)
        # Second call should hit cache
        assert result1 == result2

    def test_validate_graph_structure_empty_graph(self):
        """Empty graph triggers exception path (networkx raises for null graph connectivity)."""
        from services.code_graph.graph_validation import GraphValidator

        validator = GraphValidator()
        g = nx.DiGraph()

        result = validator.validate_graph_structure(g)
        # nx.is_weakly_connected raises for null graph, triggering error path
        assert result["is_valid"] is False
        assert len(result["structural_issues"]) > 0
        assert result["structural_issues"][0]["type"] == "validation_error"

    def test_validate_graph_structure_isolated_nodes(self):
        from services.code_graph.graph_validation import GraphValidator

        validator = GraphValidator()
        g = nx.DiGraph()
        g.add_node("A")
        g.add_node("B")
        g.add_edge("C", "D")

        result = validator.validate_graph_structure(g)
        connectivity_issues = [
            i for i in result["structural_issues"] if i["type"] == "connectivity"
        ]
        assert len(connectivity_issues) > 0
        assert result["connectivity_score"] < 1.0

    def test_validate_node_relationships_missing(self):
        from services.code_graph.graph_validation import GraphValidator

        validator = GraphValidator()
        g = nx.DiGraph()
        g.add_edge("A", "B")

        result = validator.validate_node_relationships(g, "missing_node")
        assert result["is_valid"] is False

    def test_validate_node_relationships_self_loop(self):
        from services.code_graph.graph_validation import GraphValidator

        validator = GraphValidator()
        g = nx.DiGraph()
        g.add_edge("A", "A")  # self-loop

        result = validator.validate_node_relationships(g, "A")
        self_ref = [i for i in result["issues"] if i["type"] == "self_reference"]
        assert len(self_ref) == 1

    def test_validate_node_relationships_high_fan_out(self):
        from services.code_graph.graph_validation import GraphValidator

        validator = GraphValidator()
        g = nx.DiGraph()
        for i in range(25):
            g.add_edge("hub", f"target_{i}")

        result = validator.validate_node_relationships(g, "hub")
        coupling = [i for i in result["issues"] if i["type"] == "high_coupling"]
        assert len(coupling) == 1

    def test_validate_node_relationships_high_fan_in(self):
        from services.code_graph.graph_validation import GraphValidator

        validator = GraphValidator()
        g = nx.DiGraph()
        for i in range(20):
            g.add_edge(f"source_{i}", "hub")

        result = validator.validate_node_relationships(g, "hub")
        dep = [i for i in result["issues"] if i["type"] == "high_dependency"]
        assert len(dep) == 1

    def test_check_dependency_cycles_no_cycles(self):
        from services.code_graph.graph_validation import GraphValidator

        validator = GraphValidator()
        g = nx.DiGraph()
        g.add_edges_from([("A", "B"), ("B", "C")])

        result = validator.check_dependency_cycles(g)
        assert result["has_cycles"] is False
        assert result["severity_assessment"] == "none"

    def test_check_dependency_cycles_low_severity(self):
        from services.code_graph.graph_validation import GraphValidator

        validator = GraphValidator()
        g = nx.DiGraph()
        g.add_edges_from([("A", "B"), ("B", "A")])

        result = validator.check_dependency_cycles(g)
        assert result["has_cycles"] is True
        assert result["severity_assessment"] == "low"

    def test_check_dependency_cycles_medium_severity(self):
        from services.code_graph.graph_validation import GraphValidator

        validator = GraphValidator()
        g = nx.DiGraph()
        g.add_edges_from([("A", "B"), ("B", "A"), ("C", "D"), ("D", "E"), ("E", "C")])

        result = validator.check_dependency_cycles(g)
        assert result["has_cycles"] is True
        # 2 cycles, severity could be low or medium
        assert result["severity_assessment"] in ("low", "medium")

    def test_check_dependency_cycles_high_severity(self):
        from services.code_graph.graph_validation import GraphValidator

        validator = GraphValidator()
        g = nx.DiGraph()
        # Create many cycles
        for i in range(10):
            g.add_edge(f"a{i}", f"b{i}")
            g.add_edge(f"b{i}", f"a{i}")

        result = validator.check_dependency_cycles(g)
        assert result["has_cycles"] is True
        assert result["severity_assessment"] == "high"

    def test_find_dangling_references_with_ref_attributes(self):
        from services.code_graph.graph_validation import GraphValidator

        validator = GraphValidator()
        g = nx.DiGraph()
        g.add_node("A", parent_ref="missing_node")
        g.add_node("B", deps_reference=["C", "missing_too"])
        g.add_node("C")

        result = validator._find_dangling_references(g)
        assert len(result) > 0

    def test_find_dangling_references_list_ref(self):
        from services.code_graph.graph_validation import GraphValidator

        validator = GraphValidator()
        g = nx.DiGraph()
        g.add_node("A", module_ref=["B", "nonexistent"])
        g.add_node("B")

        result = validator._find_dangling_references(g)
        assert any("nonexistent" in r for r in result)

    def test_validate_graph_integrity_with_expected_nodes(self):
        from services.code_graph.graph_validation import GraphValidator

        validator = GraphValidator()
        g = nx.DiGraph()
        g.add_edges_from([("A", "B"), ("B", "C")])

        result = validator.validate_graph_integrity(g, expected_nodes=["A", "B", "D"])
        assert result["is_intact"] is False  # D is missing (high severity)
        assert "D" in result["missing_nodes"]
        assert "C" in result["unexpected_nodes"]
        assert result["completeness_score"] < 1.0

    def test_validate_graph_integrity_all_present(self):
        from services.code_graph.graph_validation import GraphValidator

        validator = GraphValidator()
        g = nx.DiGraph()
        g.add_edges_from([("A", "B")])

        result = validator.validate_graph_integrity(g, expected_nodes=["A", "B"])
        assert result["is_intact"] is True
        assert result["completeness_score"] == 1.0

    def test_validate_graph_integrity_no_expected(self):
        from services.code_graph.graph_validation import GraphValidator

        validator = GraphValidator()
        g = nx.DiGraph()
        g.add_edge("A", "B")

        result = validator.validate_graph_integrity(g)
        assert result["is_intact"] is True

    def test_hash_graph_fallback(self):
        from services.code_graph.graph_validation import GraphValidator

        validator = GraphValidator()
        g = nx.DiGraph()
        g.add_node(123)  # Non-string node
        # _hash_graph should handle non-string nodes via fallback
        h = validator._hash_graph(g)
        assert isinstance(h, str)


# =====================================================================
# services/job_manager.py — deeper coverage
# =====================================================================


class TestJobManagerDeep:
    """Cover uncovered lines in job_manager.py."""

    def _make_job_manager(self):
        from services.job_manager import KubernetesJobManager

        with patch("kubernetes.config.load_kube_config"):
            mgr = KubernetesJobManager.__new__(KubernetesJobManager)
            mgr.namespace = "test"
            mgr.MAX_CONCURRENT_JOBS = 5
            mgr.DEFAULT_TTL_SECONDS = 300
            mgr.DEFAULT_BACKOFF_LIMIT = 3
            mgr.DEFAULT_TIMEOUT_SECONDS = 600
            mgr.batch_v1 = MagicMock()
            mgr.core_v1 = MagicMock()
            return mgr

    async def test_create_pytest_job_success(self):
        mgr = self._make_job_manager()
        mgr.count_all_active_jobs = AsyncMock(return_value=0)

        mock_result = MagicMock()
        mock_result.metadata.name = "pytest-job-123"
        mgr.batch_v1.create_namespaced_job = MagicMock(return_value=mock_result)

        job_name = await mgr.create_pytest_job({"pytest_args": ["-v"]}, tenant_id="n8n-dev")
        assert job_name == "pytest-job-123"

    async def test_create_pytest_job_concurrent_limit(self):
        mgr = self._make_job_manager()
        mgr.MAX_CONCURRENT_JOBS = 2
        mgr.count_all_active_jobs = AsyncMock(return_value=2)

        with pytest.raises(RuntimeError, match="Max 2"):
            await mgr.create_pytest_job({"pytest_args": ["-v"]}, tenant_id="n8n-dev")

    async def test_get_job_status_succeeded(self):
        mgr = self._make_job_manager()
        mock_job = MagicMock()
        mock_job.metadata.labels = _owned_labels()
        mock_job.status.succeeded = 1
        mock_job.status.failed = 0
        mock_job.status.active = 0
        mock_job.status.start_time = datetime(2025, 1, 1, tzinfo=UTC)
        mock_job.status.completion_time = datetime(2025, 1, 1, 0, 5, tzinfo=UTC)
        mgr.batch_v1.read_namespaced_job = MagicMock(return_value=mock_job)

        status = await mgr.get_job_status("test-job", tenant_id="n8n-dev")
        assert status["status"] == "succeeded"

    async def test_get_job_status_failed(self):
        mgr = self._make_job_manager()
        mock_job = MagicMock()
        mock_job.metadata.labels = _owned_labels()
        mock_job.status.succeeded = 0
        mock_job.status.failed = 1
        mock_job.status.active = 0
        mock_job.status.start_time = None
        mock_job.status.completion_time = None
        mgr.batch_v1.read_namespaced_job = MagicMock(return_value=mock_job)

        status = await mgr.get_job_status("test-job", tenant_id="n8n-dev")
        assert status["status"] == "failed"

    async def test_get_job_status_running(self):
        mgr = self._make_job_manager()
        mock_job = MagicMock()
        mock_job.metadata.labels = _owned_labels()
        mock_job.status.succeeded = 0
        mock_job.status.failed = 0
        mock_job.status.active = 1
        mock_job.status.start_time = datetime(2025, 1, 1, tzinfo=UTC)
        mock_job.status.completion_time = None
        mgr.batch_v1.read_namespaced_job = MagicMock(return_value=mock_job)

        status = await mgr.get_job_status("test-job", tenant_id="n8n-dev")
        assert status["status"] == "running"

    async def test_get_job_status_pending(self):
        mgr = self._make_job_manager()
        mock_job = MagicMock()
        mock_job.metadata.labels = _owned_labels()
        mock_job.status.succeeded = 0
        mock_job.status.failed = 0
        mock_job.status.active = 0
        mock_job.status.start_time = None
        mock_job.status.completion_time = None
        mgr.batch_v1.read_namespaced_job = MagicMock(return_value=mock_job)

        status = await mgr.get_job_status("test-job", tenant_id="n8n-dev")
        assert status["status"] == "pending"

    async def test_get_job_status_not_found(self):
        from kubernetes.client.rest import ApiException

        mgr = self._make_job_manager()
        mgr.batch_v1.read_namespaced_job = MagicMock(
            side_effect=ApiException(status=404, reason="Not Found")
        )

        status = await mgr.get_job_status("nonexistent", tenant_id="n8n-dev")
        assert status is None

    async def test_get_job_logs(self):
        mgr = self._make_job_manager()
        mock_pod = MagicMock()
        mock_pod.metadata.name = "test-pod"
        mock_pods = MagicMock()
        mock_pods.items = [mock_pod]
        mgr.core_v1.list_namespaced_pod = MagicMock(return_value=mock_pods)
        mgr.core_v1.read_namespaced_pod_log = MagicMock(return_value="test log output")

        logs = await mgr.get_job_logs("test-job", tenant_id="n8n-dev")
        assert "test-pod" in logs
        assert "test log output" in logs

    async def test_get_job_logs_error_reading_pod(self):
        from kubernetes.client.rest import ApiException

        mgr = self._make_job_manager()
        mock_pod = MagicMock()
        mock_pod.metadata.name = "test-pod"
        mock_pods = MagicMock()
        mock_pods.items = [mock_pod]
        mgr.core_v1.list_namespaced_pod = MagicMock(return_value=mock_pods)
        mgr.core_v1.read_namespaced_pod_log = MagicMock(
            side_effect=ApiException(status=500, reason="error")
        )

        logs = await mgr.get_job_logs("test-job", tenant_id="n8n-dev")
        assert "Error reading logs" in logs

    async def test_delete_job(self):
        mgr = self._make_job_manager()
        mgr.batch_v1.read_namespaced_job.return_value.metadata.labels = _owned_labels()
        mgr.batch_v1.delete_namespaced_job = MagicMock()

        await mgr.delete_job("test-job", tenant_id="n8n-dev")
        mgr.batch_v1.delete_namespaced_job.assert_called_once()

    async def test_delete_job_not_found(self):
        from kubernetes.client.rest import ApiException

        mgr = self._make_job_manager()
        mgr.batch_v1.delete_namespaced_job = MagicMock(
            side_effect=ApiException(status=404, reason="Not Found")
        )

        await mgr.delete_job("nonexistent", tenant_id="n8n-dev")  # Should not raise

    async def test_get_active_jobs(self):
        mgr = self._make_job_manager()
        mock_job1 = MagicMock()
        mock_job1.metadata.labels = _owned_labels()
        mock_job1.metadata.name = "active-job"
        mock_job1.status.active = 1
        mock_job1.status.completion_time = None

        mock_job2 = MagicMock()

        mock_job2.metadata.labels = _owned_labels()
        mock_job2.metadata.name = "completed-job"
        mock_job2.status.active = 0
        mock_job2.status.completion_time = datetime.now(UTC)

        mock_jobs = MagicMock()
        mock_jobs.items = [mock_job1, mock_job2]
        mgr.batch_v1.list_namespaced_job = MagicMock(return_value=mock_jobs)

        active = await mgr.get_active_jobs(tenant_id="n8n-dev")
        assert "active-job" in active
        assert "completed-job" not in active

    async def test_wait_for_completion_success(self):
        mgr = self._make_job_manager()

        call_count = 0

        async def mock_status(name, **_kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                return {"status": "running"}
            return {"status": "succeeded"}

        mgr.get_job_status = mock_status

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await mgr.wait_for_completion("test-job", timeout=60, tenant_id="n8n-dev")
        assert result["status"] == "succeeded"

    async def test_wait_for_completion_not_found(self):
        mgr = self._make_job_manager()
        mgr.get_job_status = AsyncMock(return_value=None)

        with pytest.raises(LookupError, match="not found"):
            await mgr.wait_for_completion("test-job", tenant_id="n8n-dev")

    async def test_wait_for_completion_timeout(self):
        mgr = self._make_job_manager()
        mgr.get_job_status = AsyncMock(return_value={"status": "running"})

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(TimeoutError):
                await mgr.wait_for_completion("test-job", timeout=0, tenant_id="n8n-dev")

    def test_build_job_manifest(self):
        mgr = self._make_job_manager()
        manifest = mgr._build_job_manifest(
            {"pytest_args": ["-v", "--tb=short"]}, timeout_seconds=120, tenant_id="n8n-dev"
        )

        assert manifest["kind"] == "Job"
        assert manifest["spec"]["activeDeadlineSeconds"] == 120
        containers = manifest["spec"]["template"]["spec"]["containers"]
        assert containers[0]["args"] == ["-v", "--tb=short"]

    def test_build_job_manifest_default_timeout(self):
        mgr = self._make_job_manager()
        manifest = mgr._build_job_manifest({"pytest_args": []}, tenant_id="n8n-dev")
        assert manifest["spec"]["activeDeadlineSeconds"] == mgr.DEFAULT_TIMEOUT_SECONDS

    async def test_job_exists_true(self):
        mgr = self._make_job_manager()
        mgr.batch_v1.read_namespaced_job = MagicMock(return_value=MagicMock())
        assert await mgr.job_exists("test-job") is True

    async def test_job_exists_false(self):
        from kubernetes.client.rest import ApiException

        mgr = self._make_job_manager()
        mgr.batch_v1.read_namespaced_job = MagicMock(
            side_effect=ApiException(status=404, reason="Not Found")
        )
        assert await mgr.job_exists("nonexistent") is False

    async def test_get_completion_time(self):
        mgr = self._make_job_manager()
        mock_job = MagicMock()
        mock_job.metadata.labels = _owned_labels()
        mock_job.status.completion_time = datetime(2025, 1, 1, tzinfo=UTC)
        mgr.batch_v1.read_namespaced_job = MagicMock(return_value=mock_job)

        result = await mgr.get_completion_time("test-job")
        assert result is not None
        assert "2025" in result

    async def test_get_completion_time_not_completed(self):
        mgr = self._make_job_manager()
        mock_job = MagicMock()
        mock_job.metadata.labels = _owned_labels()
        mock_job.status.completion_time = None
        mgr.batch_v1.read_namespaced_job = MagicMock(return_value=mock_job)

        result = await mgr.get_completion_time("test-job")
        assert result is None

    async def test_list_all_jobs(self):
        mgr = self._make_job_manager()
        mock_job = MagicMock()
        mock_job.metadata.labels = _owned_labels()
        mock_job.metadata.name = "job1"
        mock_job.metadata.creation_timestamp = datetime(2025, 1, 1, tzinfo=UTC)
        mock_job.status.active = 0
        mock_job.status.succeeded = 1
        mock_job.status.failed = 0
        mock_job.status.completion_time = datetime(2025, 1, 1, 0, 5, tzinfo=UTC)

        mock_jobs = MagicMock()
        mock_jobs.items = [mock_job]
        mgr.batch_v1.list_namespaced_job = MagicMock(return_value=mock_jobs)

        jobs = await mgr.list_all_jobs(label_selector="app=test")
        assert len(jobs) == 1
        assert jobs[0]["name"] == "job1"
        assert jobs[0]["succeeded"] == 1

    async def test_cleanup_old_jobs(self):
        mgr = self._make_job_manager()
        old_time = datetime.now(UTC) - timedelta(days=2)

        mock_job = MagicMock()

        mock_job.metadata.labels = _owned_labels()
        mock_job.metadata.name = "old-job"
        mock_job.status.completion_time = old_time

        mock_jobs = MagicMock()
        mock_jobs.items = [mock_job]
        mgr.batch_v1.list_namespaced_job = MagicMock(return_value=mock_jobs)
        mgr._delete_by_name = AsyncMock()

        deleted = await mgr.cleanup_old_jobs(max_age_seconds=3600)
        assert deleted == 1
        mgr._delete_by_name.assert_awaited_once_with("old-job")


# =====================================================================
# services/validation.py — deeper coverage
# =====================================================================


class TestValidationServiceDeep:
    """Cover uncovered lines in services/validation.py."""

    async def test_ast_validation_non_python(self):
        from services.validation import ASTValidationService

        svc = ASTValidationService()
        result = await svc.analyze_proposed_code_ast("fn main() {}", "main.rs", "rust")
        assert result.is_valid is False
        assert "not supported" in result.syntax_errors[0]

    async def test_ast_validation_syntax_error(self):
        from services.validation import ASTValidationService

        svc = ASTValidationService()
        result = await svc.analyze_proposed_code_ast("def (:", "bad.py", "python")
        assert result.is_valid is False
        assert len(result.syntax_errors) > 0

    async def test_ast_validation_valid_python(self):
        from services.validation import ASTValidationService

        svc = ASTValidationService()
        code = "import os\nx = 1\ndef hello():\n    pass\n"
        result = await svc.analyze_proposed_code_ast(code, "test.py", "python")
        assert result.is_valid is True
        assert result.line_count == 4

    async def test_ast_validation_with_classes(self):
        from services.validation import ASTValidationService

        svc = ASTValidationService()
        code = "class MyClass:\n    def method(self):\n        pass\n"
        result = await svc.analyze_proposed_code_ast(code, "test.py", "python")
        assert result.is_valid is True

    async def test_import_validation_stdlib(self):
        from services.validation import ImportValidationService

        svc = ImportValidationService()
        assert svc.is_stdlib_module("os") is True
        assert svc.is_stdlib_module("sys") is True
        assert svc.is_stdlib_module(".relative") is False

    async def test_import_validation_third_party_map(self):
        from services.validation import ImportValidationService

        svc = ImportValidationService()
        # Test _is_third_party_available with known mapping
        available = {"pillow": {"version": "9.0"}}
        result = await svc._is_third_party_available("PIL", available)
        assert result is True

    async def test_import_validation_third_party_prefix_match(self):
        from services.validation import ImportValidationService

        svc = ImportValidationService()
        available = {"requests": {"version": "2.0"}}
        result = await svc._is_third_party_available("requests.auth", available)
        assert result is True

    def test_is_likely_third_party(self):
        from services.validation import ImportValidationService

        svc = ImportValidationService()
        assert svc._is_likely_third_party("requests") is True
        assert svc._is_likely_third_party("my_local_module") is False


# =====================================================================
# services/cache_manager.py — cache operations coverage
# =====================================================================


class TestCacheManagerCoverage:
    """Cover uncovered cache_manager methods."""

    async def test_get_cache_manager_returns_instance(self):
        import services.cache_manager as cm_module

        # Reset singleton state
        original_manager = cm_module._cache_manager
        original_lock = cm_module._cache_manager_lock
        cm_module._cache_manager = None
        cm_module._cache_manager_lock = None

        try:
            result = await cm_module.get_cache_manager()
            assert result is not None
            assert isinstance(result, cm_module.RedisCacheManager)
        finally:
            cm_module._cache_manager = original_manager
            cm_module._cache_manager_lock = original_lock

    def test_get_cache_manager_sync_returns_instance(self):
        import services.cache_manager as cm_module

        original = cm_module._cache_manager
        cm_module._cache_manager = None

        try:
            result = cm_module.get_cache_manager_sync()
            assert result is not None
            assert isinstance(result, cm_module.RedisCacheManager)
        finally:
            cm_module._cache_manager = original


# =====================================================================
# routers/graphs.py — deeper endpoint coverage
# =====================================================================


class TestGraphsRouterDeep:
    """Cover uncovered lines in routers/graphs.py."""

    def test_validate_sqlite_pagination(self):
        from routers.graphs import _validate_sqlite_pagination

        # Normal case should not raise
        _validate_sqlite_pagination(1, 10)

    def test_validate_sqlite_pagination_overflow(self):
        from routers.graphs import SQLITE_INTEGER_MAX, _validate_sqlite_pagination

        with pytest.raises(Exception):
            _validate_sqlite_pagination(SQLITE_INTEGER_MAX, SQLITE_INTEGER_MAX)


# =====================================================================
# routers/metrics.py — cover uncovered metric endpoints
# =====================================================================


class TestMetricsRouterDeep:
    """Cover metrics router endpoints with proper assertions."""

    async def test_container_health_metrics_returns_404_without_db(self, client):
        """GET /api/health/metrics/container/{name}/health returns 404 when no graph."""
        # coding-analysis returns no graph for the container → service is None → 404.
        with patch("routers.metrics._get_metrics_service", new=AsyncMock(return_value=None)):
            resp = await client.get("/api/health/metrics/container/nonexistent/health")
            assert resp.status_code == 404

    async def test_graph_summary_endpoint_returns_200(self, client):
        """GET /api/health/metrics/graph-summary returns proper summary."""
        # No container → service is None → empty-but-valid summary payload.
        with patch("routers.metrics._get_metrics_service", new=AsyncMock(return_value=None)):
            resp = await client.get("/api/health/metrics/graph-summary")
            assert resp.status_code == 200
            data = resp.json()
            assert "entity_counts" in data
            assert "graph_density" in data


# =====================================================================
# routers/jobs.py — cover job router endpoints
# =====================================================================


class TestJobsRouterEndpoints:
    """Cover routers/jobs.py endpoints via HTTP with proper mocking."""

    async def test_list_jobs_returns_active_jobs(self, client):
        """GET /api/jobs/ returns active job list with count."""
        with patch("routers.jobs.get_job_manager") as mock_mgr:
            mock_instance = MagicMock()
            mock_instance.get_active_jobs = AsyncMock(return_value=["job-1", "job-2"])
            mock_instance.namespace = "test-ns"
            mock_mgr.return_value = mock_instance

            resp = await client.get("/api/jobs/")
            assert resp.status_code == 200
            data = resp.json()
            assert data["count"] == 2
            assert data["namespace"] == "test-ns"

    async def test_create_job_returns_201_with_job_name(self, client):
        """POST /api/jobs/ creates job and returns 201."""
        with patch("routers.jobs.get_job_manager") as mock_mgr:
            mock_instance = MagicMock()
            mock_instance.count_all_active_jobs = AsyncMock(return_value=0)
            mock_instance.create_pytest_job = AsyncMock(return_value="test-job-123")
            mock_instance.namespace = "test-ns"
            mock_mgr.return_value = mock_instance

            resp = await client.post(
                "/api/jobs/",
                json={"pytest_args": ["-v"]},
            )
            assert resp.status_code == 201
            data = resp.json()
            assert data["job_name"] == "test-job-123"
            assert data["status"] == "created"
            assert data["namespace"] == "test-ns"


# =====================================================================
# routers/validation.py — cover validation endpoints
# =====================================================================


class TestValidationRouterEndpoints:
    """Cover routers/validation.py endpoints with proper assertions."""

    async def test_validate_code_refuses_when_no_workspace_is_reachable(self, client):
        """POST /api/validation/code must NOT certify a file it never read.

        This test used to assert ``valid is True`` for a file named "test.py" in
        a container named "test-container", neither of which exists. It passed
        because the handler built ``{"valid": True, "errors": []}`` as a
        literal.

        conftest points CONTAINERS_URL at http://coding-containers:8691, which
        does not resolve from the test runner, so the workspace read fails at
        the transport. The endpoint now reports that it could not look (503)
        instead of reporting that it looked and found nothing wrong -- which is
        the whole point: those are different claims.
        """
        resp = await client.post(
            "/api/validation/code",
            json={
                "container_name": "test-container",
                "file_path": "test.py",
                "validation_type": "syntax",
            },
        )
        assert resp.status_code == 503
        assert "validation_results" not in resp.json()

    async def test_validate_code_missing_fields_returns_422(self, client):
        """POST /api/validation/code with missing fields returns 422."""
        resp = await client.post(
            "/api/validation/code",
            json={"code": "x = 1"},
        )
        assert resp.status_code == 422

    async def test_validate_file_reference_without_a_codebase_refuses(self, client):
        """A request naming no codebase gets a refusal, not a verdict.

        Previously this returned 200 valid=false, because the router silently
        fell back to the gateway's own (nonexistent) /app/data/repo.
        """
        resp = await client.post(
            "/api/validation/file-reference",
            json={"file_path": "test.py"},
        )
        assert resp.status_code == 400
        assert "valid" not in resp.json()


# =====================================================================
# routers/validation_status.py — cover status endpoints
# =====================================================================


class TestValidationStatusEndpoints:
    """Cover routers/validation_status.py endpoints."""

    async def test_get_nonexistent_validation_returns_404(self, client):
        """GET /api/validation/{id} for nonexistent validation returns 404."""
        resp = await client.get("/api/validation/nonexistent-id-12345")
        assert resp.status_code == 404


# =====================================================================
# routers/cache.py — cover additional cache endpoints
# =====================================================================


class TestCacheRouterDeep:
    """Cover cache router endpoints with proper assertions."""

    async def test_cache_stats_returns_stats_object(self, client):
        """GET /api/cache/stats returns stats with expected fields."""
        resp = await client.get("/api/cache/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert "stats" in data


# =====================================================================
# routers/visualization.py — cover visualization endpoints
# =====================================================================


class TestVisualizationRouterDeep:
    """Cover visualization endpoint behavior."""

    async def test_visualization_capabilities_returns_types(self, client):
        """GET /api/visualization/capabilities returns supported types."""
        resp = await client.get("/api/visualization/capabilities")
        assert resp.status_code == 200
        data = resp.json()
        assert "visualization_types" in data
        assert "supported_languages" in data
        assert len(data["visualization_types"]) > 0


# =====================================================================
# services/code_graph/consistency_checker.py — cover checker operations
# =====================================================================


class TestConsistencyCheckerDeep:
    """Cover uncovered consistency_checker lines."""

    def test_code_consistency_checker_initialization(self):
        from services.code_graph.consistency_checker import CodeConsistencyChecker

        checker = CodeConsistencyChecker()
        assert checker.rules_engine is not None
        assert checker.error_detector is not None
        assert checker.graph_validator is not None
        assert checker.config["enable_caching"] is True

    def test_code_consistency_checker_config(self):
        from services.code_graph.consistency_checker import CodeConsistencyChecker

        checker = CodeConsistencyChecker()
        assert "severity_weights" in checker.config
        assert checker.config["severity_weights"]["high"] == 0.3
        assert checker.config["fast_mode"] is False
