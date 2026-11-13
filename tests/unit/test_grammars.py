"""Tests for the grammars router — status, install, install-all, async install,
task status, check-updates, cleanup, and supported-languages endpoints.

All tests send real HTTP requests via the ``client`` fixture and mock only the
grammar manager service.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_grammar_manager(
    status_data: dict | None = None,
    install_result: bool = True,
    install_all_result: dict | None = None,
    updates: dict | None = None,
    cleanup_result: dict | None = None,
):
    """Return a mock TreeSitterGrammarManager."""
    mgr = AsyncMock()
    mgr.initialize = AsyncMock()

    default_status = {
        "python": {
            "language": "python",
            "installed": True,
            "installed_version": "0.20.4",
            "latest_version": "0.20.4",
            "needs_update": False,
            "last_checked": "2026-01-01T00:00:00Z",
            "last_updated": "2026-01-01T00:00:00Z",
            "repository_url": "https://github.com/tree-sitter/tree-sitter-python",
        },
        "java": {
            "language": "java",
            "installed": True,
            "installed_version": "0.20.2",
            "latest_version": "0.20.3",
            "needs_update": True,
            "last_checked": "2026-01-01T00:00:00Z",
            "last_updated": "2025-12-01T00:00:00Z",
            "repository_url": "https://github.com/tree-sitter/tree-sitter-java",
        },
        "rust": {
            "language": "rust",
            "installed": False,
            "installed_version": None,
            "latest_version": "0.20.4",
            "needs_update": False,
            "last_checked": None,
            "last_updated": None,
            "repository_url": "https://github.com/tree-sitter/tree-sitter-rust",
        },
    }

    mgr.get_grammar_status = AsyncMock(return_value=status_data or default_status)
    mgr.install_grammar = AsyncMock(return_value=install_result)
    mgr.install_all_grammars = AsyncMock(
        return_value=install_all_result or {"python": True, "java": True, "rust": True}
    )
    mgr.check_for_updates = AsyncMock(
        return_value=updates or {"python": False, "java": True, "rust": False}
    )
    mgr.cleanup_old_versions = AsyncMock(
        return_value=cleanup_result or {"python": 0, "java": 2, "rust": 0}
    )
    mgr.OFFICIAL_GRAMMARS = {
        "python": {"repository_url": "https://github.com/tree-sitter/tree-sitter-python"},
        "java": {"repository_url": "https://github.com/tree-sitter/tree-sitter-java"},
        "rust": {"repository_url": "https://github.com/tree-sitter/tree-sitter-rust"},
    }
    return mgr


def _patch_grammar_manager(mgr):
    return patch("routers.grammars.get_grammar_manager", return_value=mgr)


# ---------------------------------------------------------------------------
# GET /api/grammars/status
# ---------------------------------------------------------------------------


class TestGrammarStatus:
    async def test_returns_all_languages(self, client):
        mgr = _mock_grammar_manager()
        with _patch_grammar_manager(mgr):
            resp = await client.get("/api/grammars/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert set(body["grammars"].keys()) == {"python", "java", "rust"}
        assert body["grammars"]["python"]["installed"] is True
        assert body["grammars"]["java"]["needs_update"] is True
        assert body["grammars"]["rust"]["installed"] is False
        assert body["grammars"]["rust"]["installed_version"] is None

    async def test_service_error_returns_500(self, client):
        mgr = _mock_grammar_manager()
        mgr.get_grammar_status = AsyncMock(side_effect=RuntimeError("disk error"))
        with _patch_grammar_manager(mgr):
            resp = await client.get("/api/grammars/status")
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# POST /api/grammars/install
# ---------------------------------------------------------------------------


class TestInstallGrammars:
    async def test_install_specific_languages(self, client):
        mgr = _mock_grammar_manager()
        with _patch_grammar_manager(mgr):
            resp = await client.post(
                "/api/grammars/install",
                json={"languages": ["python", "java"], "force": False},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert body["results"]["python"] is True
        assert body["results"]["java"] is True
        assert body["summary"]["total_requested"] == 2
        assert body["summary"]["successful_installations"] == 2
        assert body["summary"]["failed_installations"] == 0

    async def test_empty_languages_returns_400(self, client):
        mgr = _mock_grammar_manager()
        with _patch_grammar_manager(mgr):
            resp = await client.post(
                "/api/grammars/install",
                json={"languages": [], "force": False},
            )
        assert resp.status_code == 400

    async def test_unsupported_language_returns_400(self, client):
        mgr = _mock_grammar_manager()
        with _patch_grammar_manager(mgr):
            resp = await client.post(
                "/api/grammars/install",
                json={"languages": ["brainfuck"], "force": False},
            )
        assert resp.status_code == 400

    async def test_partial_failure_reported(self, client):
        mgr = _mock_grammar_manager()
        call_count = 0

        async def _alternating(language, force=False):
            nonlocal call_count
            call_count += 1
            return call_count % 2 == 1

        mgr.install_grammar = _alternating
        with _patch_grammar_manager(mgr):
            resp = await client.post(
                "/api/grammars/install",
                json={"languages": ["python", "java"], "force": True},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["summary"]["successful_installations"] == 1
        assert body["summary"]["failed_installations"] == 1

    async def test_initialize_error_returns_500(self, client):
        mgr = _mock_grammar_manager()
        mgr.initialize = AsyncMock(side_effect=RuntimeError("crash"))
        with _patch_grammar_manager(mgr):
            resp = await client.post(
                "/api/grammars/install",
                json={"languages": ["python"]},
            )
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# POST /api/grammars/install-all
# ---------------------------------------------------------------------------


class TestInstallAllGrammars:
    async def test_all_succeed(self, client):
        mgr = _mock_grammar_manager()
        with _patch_grammar_manager(mgr):
            resp = await client.post("/api/grammars/install-all")
        assert resp.status_code == 200
        body = resp.json()
        assert body["summary"]["total_languages"] == 3
        assert body["summary"]["successful_installations"] == 3
        assert body["summary"]["failed_installations"] == 0

    async def test_partial_failures(self, client):
        mgr = _mock_grammar_manager(install_all_result={"python": True, "java": False})
        with _patch_grammar_manager(mgr):
            resp = await client.post("/api/grammars/install-all")
        assert resp.status_code == 200
        body = resp.json()
        assert body["summary"]["failed_installations"] == 1
        assert "failed" in body["message"].lower()


# ---------------------------------------------------------------------------
# POST /api/grammars/install-all-async
# ---------------------------------------------------------------------------


class TestInstallAllAsync:
    async def test_returns_task_id_and_check_url(self, client):
        resp = await client.post("/api/grammars/install-all-async")
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_status"] == "submitted"
        assert body["task_id"].startswith("grammar-install-")
        assert "/api/grammars/tasks/" in body["check_url"]
        # check_url contains the task_id
        assert body["task_id"] in body["check_url"]


# ---------------------------------------------------------------------------
# GET /api/grammars/tasks/{task_id}
# ---------------------------------------------------------------------------


class TestTaskStatus:
    async def test_unknown_task_returns_404(self, client):
        resp = await client.get("/api/grammars/tasks/nonexistent-task")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/grammars/check-updates
# ---------------------------------------------------------------------------


class TestCheckUpdates:
    async def test_returns_update_availability(self, client):
        mgr = _mock_grammar_manager()
        with _patch_grammar_manager(mgr):
            resp = await client.post("/api/grammars/check-updates")
        assert resp.status_code == 200
        body = resp.json()
        assert body["updates_available"]["java"] is True
        assert body["updates_available"]["python"] is False
        assert body["summary"]["updates_available"] == 1
        assert body["summary"]["up_to_date"] == 2
        assert "java" in body["summary"]["languages_with_updates"]

    async def test_specific_language_filter(self, client):
        mgr = _mock_grammar_manager()
        with _patch_grammar_manager(mgr):
            resp = await client.post("/api/grammars/check-updates?languages=python")
        assert resp.status_code == 200

    async def test_unsupported_language_returns_400(self, client):
        mgr = _mock_grammar_manager()
        with _patch_grammar_manager(mgr):
            resp = await client.post("/api/grammars/check-updates?languages=klingon")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# DELETE /api/grammars/cleanup
# ---------------------------------------------------------------------------


class TestCleanupGrammars:
    async def test_removes_old_versions(self, client):
        mgr = _mock_grammar_manager()
        with _patch_grammar_manager(mgr):
            resp = await client.delete("/api/grammars/cleanup")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_versions_removed"] == 2
        assert body["languages_cleaned"] == 1
        assert body["total_languages"] == 3

    async def test_nothing_to_clean(self, client):
        mgr = _mock_grammar_manager(cleanup_result={"python": 0, "java": 0, "rust": 0})
        with _patch_grammar_manager(mgr):
            resp = await client.delete("/api/grammars/cleanup")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_versions_removed"] == 0
        assert body["languages_cleaned"] == 0

    async def test_service_error_returns_500(self, client):
        mgr = _mock_grammar_manager()
        mgr.cleanup_old_versions = AsyncMock(side_effect=RuntimeError("disk full"))
        with _patch_grammar_manager(mgr):
            resp = await client.delete("/api/grammars/cleanup")
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# GET /api/grammars/supported-languages
# ---------------------------------------------------------------------------


class TestSupportedLanguages:
    async def test_returns_language_list(self, client):
        mgr = _mock_grammar_manager()
        with _patch_grammar_manager(mgr):
            resp = await client.get("/api/grammars/supported-languages")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_languages"] == 3
        assert set(body["supported_languages"]) == {"python", "java", "rust"}
        # languages_info has detail per language
        assert "python" in body["languages_info"]
        assert body["languages_info"]["python"]["installed"] is True

    async def test_service_error_returns_500(self, client):
        mgr = _mock_grammar_manager()
        mgr.get_grammar_status = AsyncMock(side_effect=RuntimeError("fail"))
        with _patch_grammar_manager(mgr):
            resp = await client.get("/api/grammars/supported-languages")
        assert resp.status_code == 500
