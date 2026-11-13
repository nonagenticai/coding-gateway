"""Tests for services/grammar_manager.py – TreeSitterGrammarManager."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from services.grammar_manager import (
    GrammarInfo,
    TreeSitterGrammarManager,
    get_grammar_manager,
)


@pytest.fixture
def tmp_cache_dir(tmp_path):
    """Provide a temporary directory for grammar caching."""
    return tmp_path / "grammars"


@pytest.fixture
def manager(tmp_cache_dir):
    mgr = TreeSitterGrammarManager(cache_dir=tmp_cache_dir)
    return mgr


# ---------------------------------------------------------------------------
# list / exists / missing
# ---------------------------------------------------------------------------


class TestListGrammars:
    async def test_list_after_init(self, manager):
        await manager.initialize()
        assert len(manager.grammars) >= len(TreeSitterGrammarManager.OFFICIAL_GRAMMARS)

    async def test_all_official_languages_present(self, manager):
        await manager.initialize()
        for lang in TreeSitterGrammarManager.OFFICIAL_GRAMMARS:
            assert lang in manager.grammars

    async def test_grammar_status(self, manager):
        await manager.initialize()
        status = await manager.get_grammar_status()
        assert "python" in status
        assert status["python"]["installed"] is False
        assert status["python"]["installed_version"] is None


class TestGrammarExists:
    async def test_exists_after_init(self, manager):
        await manager.initialize()
        assert "python" in manager.grammars
        assert "java" in manager.grammars

    async def test_missing_language(self, manager):
        await manager.initialize()
        assert "brainfuck" not in manager.grammars


# ---------------------------------------------------------------------------
# download success / failure
# ---------------------------------------------------------------------------


class TestDownload:
    async def test_install_unknown_language(self, manager):
        await manager.initialize()
        result = await manager.install_grammar("brainfuck")
        assert result is False

    async def test_install_already_up_to_date(self, manager):
        await manager.initialize()
        grammar = manager.grammars["python"]
        grammar.is_available = True
        grammar.installed_version = grammar.version
        result = await manager.install_grammar("python")
        assert result is True

    async def test_install_success_mocked(self, manager):
        await manager.initialize()
        with patch.object(
            manager, "_download_and_install_grammar", new_callable=AsyncMock, return_value=True
        ):
            result = await manager.install_grammar("python", force=True)
            assert result is True
            assert manager.grammars["python"].is_available is True
            assert manager.grammars["python"].installed_version is not None

    async def test_install_failure_mocked(self, manager):
        await manager.initialize()
        with patch.object(
            manager, "_download_and_install_grammar", new_callable=AsyncMock, return_value=False
        ):
            result = await manager.install_grammar("python", force=True)
            assert result is False
            assert manager.grammars["python"].is_available is False

    async def test_install_os_error(self, manager):
        await manager.initialize()
        with patch.object(
            manager, "_download_and_install_grammar", side_effect=OSError("disk full")
        ):
            result = await manager.install_grammar("python", force=True)
            assert result is False


class TestInstallAll:
    async def test_install_all(self, manager):
        await manager.initialize()
        with patch.object(manager, "install_grammar", new_callable=AsyncMock, return_value=True):
            results = await manager.install_all_grammars()
            assert all(v is True for v in results.values())

    async def test_install_all_partial_failure(self, manager):
        await manager.initialize()
        call_count = 0

        async def flaky_install(lang, force=False):
            nonlocal call_count
            call_count += 1
            return call_count % 2 == 0  # Every other one fails

        with patch.object(manager, "install_grammar", side_effect=flaky_install):
            results = await manager.install_all_grammars()
            assert not all(v is True for v in results.values())


# ---------------------------------------------------------------------------
# remove / cleanup
# ---------------------------------------------------------------------------


class TestRemove:
    async def test_cleanup_old_versions_no_path(self, manager):
        await manager.initialize()
        results = await manager.cleanup_old_versions()
        # No install_path set, so cleanup should be 0 for each
        for lang, count in results.items():
            assert count == 0

    async def test_cleanup_old_versions_with_versions(self, manager, tmp_cache_dir):
        await manager.initialize()
        grammar = manager.grammars["python"]
        lang_dir = tmp_cache_dir / "python"
        lang_dir.mkdir(parents=True, exist_ok=True)
        grammar.install_path = lang_dir
        grammar.installed_version = "v2.0"

        # Create old version directories
        (lang_dir / "v1.0").mkdir()
        (lang_dir / "v1.5").mkdir()

        results = await manager.cleanup_old_versions()
        assert results["python"] == 2


# ---------------------------------------------------------------------------
# refresh (check for updates)
# ---------------------------------------------------------------------------


class TestRefresh:
    async def test_check_for_updates_skips_unknown(self, manager):
        await manager.initialize()
        results = await manager.check_for_updates(languages=["brainfuck"])
        assert len(results) == 0

    async def test_check_for_updates_no_update(self, manager):
        await manager.initialize()
        with patch.object(
            manager, "_get_latest_version", new_callable=AsyncMock, return_value=None
        ):
            results = await manager.check_for_updates(languages=["python"])
            assert results.get("python") is False

    async def test_check_for_updates_with_update(self, manager):
        await manager.initialize()
        manager.auto_update = True  # Skip interval check
        with patch.object(
            manager, "_get_latest_version", new_callable=AsyncMock, return_value="v999"
        ):
            results = await manager.check_for_updates(languages=["python"])
            assert results["python"] is True
            assert manager.grammars["python"].version == "v999"

    async def test_check_for_updates_exception(self, manager):
        await manager.initialize()
        manager.auto_update = True
        with patch.object(manager, "_get_latest_version", side_effect=Exception("api error")):
            results = await manager.check_for_updates(languages=["python"])
            assert results["python"] is False


# ---------------------------------------------------------------------------
# filesystem errors
# ---------------------------------------------------------------------------


class TestFilesystemErrors:
    async def test_load_grammar_info_missing_file(self, manager):
        """Loading from non-existent file should not raise."""
        await manager._load_grammar_info()
        assert manager.grammars == {} or isinstance(manager.grammars, dict)

    async def test_load_grammar_info_corrupted_file(self, manager, tmp_cache_dir):
        tmp_cache_dir.mkdir(parents=True, exist_ok=True)
        grammars_file = tmp_cache_dir / "grammars.json"
        grammars_file.write_text("{invalid json")
        await manager._load_grammar_info()
        # Should not crash, just log error

    async def test_save_grammar_info(self, manager, tmp_cache_dir):
        await manager.initialize()
        # Verify file was saved
        assert (tmp_cache_dir / "grammars.json").exists()

    async def test_save_grammar_info_read_only(self, manager, tmp_cache_dir):
        await manager.initialize()
        # Make directory read-only
        grammars_file = tmp_cache_dir / "grammars.json"
        grammars_file.chmod(0o000)
        try:
            await manager._save_grammar_info()  # Should not raise
        finally:
            grammars_file.chmod(0o644)

    async def test_calculate_directory_hash(self, manager, tmp_cache_dir):
        test_dir = tmp_cache_dir / "hash_test"
        test_dir.mkdir(parents=True)
        (test_dir / "file1.txt").write_text("hello")
        (test_dir / "file2.txt").write_text("world")
        h = await manager._calculate_directory_hash(test_dir)
        assert isinstance(h, str)
        assert len(h) == 64  # SHA-256


# ---------------------------------------------------------------------------
# GrammarInfo serialization
# ---------------------------------------------------------------------------


class TestGrammarInfo:
    def test_to_dict(self):
        from datetime import UTC, datetime

        gi = GrammarInfo(
            name="tree-sitter-python",
            language="python",
            repository_url="https://github.com/tree-sitter/tree-sitter-python",
            version="v0.20",
            last_checked=datetime(2025, 1, 1, tzinfo=UTC),
            install_path=Path("/tmp/grammars/python"),
        )
        d = gi.to_dict()
        assert d["name"] == "tree-sitter-python"
        assert d["last_checked"] == "2025-01-01T00:00:00+00:00"
        assert d["install_path"] == "/tmp/grammars/python"

    def test_from_dict(self):
        data = {
            "name": "tree-sitter-java",
            "language": "java",
            "repository_url": "https://github.com/tree-sitter/tree-sitter-java",
            "version": "v0.21",
            "last_checked": "2025-06-01T00:00:00+00:00",
            "install_path": "/tmp/grammars/java",
        }
        gi = GrammarInfo.from_dict(data)
        assert gi.language == "java"
        assert gi.install_path == Path("/tmp/grammars/java")
        assert gi.last_checked is not None

    def test_from_dict_no_dates(self):
        data = {
            "name": "test",
            "language": "go",
            "repository_url": "https://example.com",
            "version": "v1",
        }
        gi = GrammarInfo.from_dict(data)
        assert gi.last_checked is None
        assert gi.install_path is None


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


class TestSingleton:
    def test_get_grammar_manager(self):
        import services.grammar_manager as mod

        old = mod._grammar_manager_instance
        mod._grammar_manager_instance = None
        try:
            mgr = get_grammar_manager()
            assert isinstance(mgr, TreeSitterGrammarManager)
        finally:
            mod._grammar_manager_instance = old
