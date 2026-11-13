"""Tests for db.session — engine, session factory, and get_db dependency.

The conftest replaces `db` with stubs.  We import the real session module
by loading the source file directly, after ensuring settings are available.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from unittest.mock import AsyncMock

import pytest

# Import the real session module directly from the source file.
_src_dir = os.path.join(os.path.dirname(__file__), "..", "..", "src")
_session_path = os.path.join(_src_dir, "db", "session.py")


def _load_session_module():
    """Load db.session from source, with ENVIRONMENT=test so settings don't fail."""
    os.environ.setdefault("ENVIRONMENT", "test")
    os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost:5432/test")
    # Clear settings cache so it picks up test env
    sys.path.insert(0, _src_dir)
    try:
        from config.settings import get_settings

        get_settings.cache_clear()
    finally:
        if _src_dir in sys.path:
            sys.path.remove(_src_dir)

    spec = importlib.util.spec_from_file_location("db_session_real", os.path.abspath(_session_path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_session_mod = _load_session_module()


class TestSessionModule:
    def test_engine_created(self):
        """Engine is created from settings.database_url."""
        assert _session_mod.engine is not None

    def test_session_factory_created(self):
        """AsyncSessionLocal is a session factory bound to the engine."""
        assert _session_mod.AsyncSessionLocal is not None

    async def test_get_db_yields_session_and_commits(self):
        """get_db yields a session, then commits on success."""
        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session.rollback = AsyncMock()

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

        original_factory = _session_mod.AsyncSessionLocal
        _session_mod.AsyncSessionLocal = lambda: mock_session_ctx
        try:
            gen = _session_mod.get_db()
            session = await gen.__anext__()
            assert session is mock_session
            try:
                await gen.__anext__()
            except StopAsyncIteration:
                pass
            mock_session.commit.assert_awaited_once()
        finally:
            _session_mod.AsyncSessionLocal = original_factory

    async def test_get_db_rolls_back_on_error(self):
        """get_db rolls back on exception."""
        mock_session = AsyncMock()
        mock_session.commit = AsyncMock(side_effect=RuntimeError("db error"))
        mock_session.rollback = AsyncMock()

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

        original_factory = _session_mod.AsyncSessionLocal
        _session_mod.AsyncSessionLocal = lambda: mock_session_ctx
        try:
            gen = _session_mod.get_db()
            session = await gen.__anext__()
            assert session is mock_session
            with pytest.raises(RuntimeError, match="db error"):
                await gen.__anext__()
            mock_session.rollback.assert_awaited_once()
        finally:
            _session_mod.AsyncSessionLocal = original_factory
