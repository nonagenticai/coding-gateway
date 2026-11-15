"""Regression tests for the py/path-injection highs (alerts 2-7).

The policy these lock in: a caller-supplied path is NORMALISED
(``os.path.realpath``), then accepted ONLY when it starts with the configured
root plus the OS separator (path components, not a string prefix), and every
later filesystem access uses the normalised value. Nothing here may pass a raw
caller string to ``open``/``isdir``/``exists``.
"""

from __future__ import annotations

import os

import pytest
from fastapi import HTTPException

from routers.validation import _explicit_dir_and_lines, _validate_explicit_path


@pytest.fixture
def codebase_root(monkeypatch, tmp_path):
    """Configure CODEBASE_WORKSPACE_ROOT = tmp_path/root for one test."""
    from config.settings import get_settings

    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("CODEBASE_WORKSPACE_ROOT", str(root))
    get_settings.cache_clear()
    yield root


def test_explicit_path_inside_root_accepted_and_normalised(codebase_root):
    repo = codebase_root / "repo"
    repo.mkdir()
    assert _validate_explicit_path(str(repo)) == os.path.realpath(repo)


def test_explicit_path_that_is_a_file_is_refused(codebase_root):
    f = codebase_root / "afile.py"
    f.write_text("x = 1\n")
    with pytest.raises(HTTPException) as exc:
        _validate_explicit_path(str(f))
    assert exc.value.status_code == 400
    assert "not a directory" in exc.value.detail


def test_explicit_path_traversal_out_of_root_is_refused(codebase_root):
    (codebase_root / "repo").mkdir()
    with pytest.raises(HTTPException) as exc:
        _validate_explicit_path(str(codebase_root / "repo" / ".." / ".." / "etc"))
    assert exc.value.status_code == 400
    assert "outside the configured codebase root" in exc.value.detail


def test_explicit_path_symlink_out_of_root_is_refused(codebase_root, tmp_path):
    real = tmp_path / "real-outside"
    real.mkdir()
    (codebase_root / "link").symlink_to(real, target_is_directory=True)
    with pytest.raises(HTTPException) as exc:
        _validate_explicit_path(str(codebase_root / "link"))
    assert exc.value.status_code == 400
    assert "outside the configured codebase root" in exc.value.detail


def test_dir_and_lines_counts_an_honest_file(codebase_root):
    repo = codebase_root / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "a.py").write_text("one\ntwo\nthree\n")
    assert _explicit_dir_and_lines(str(repo), "pkg", is_file=False, line=None) == (True, None)
    assert _explicit_dir_and_lines(str(repo), "pkg/a.py", is_file=True, line=2) == (False, 3)
    assert _explicit_dir_and_lines(str(repo), "pkg/a.py", is_file=True, line=None) == (False, None)


def test_dir_and_lines_refuses_a_symlink_escape(codebase_root, tmp_path):
    """A link inside the workspace pointing outside it is treated as absent, so
    the line-count helper cannot be used as a read oracle for pod files."""
    repo = codebase_root / "repo"
    repo.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("a\nb\nc\nd\n")
    (repo / "escape.py").symlink_to(secret)
    assert _explicit_dir_and_lines(str(repo), "escape.py", is_file=True, line=1) == (False, None)


def test_dir_and_lines_refuses_dot_dot(codebase_root):
    repo = codebase_root / "repo"
    repo.mkdir()
    assert _explicit_dir_and_lines(str(repo), "../secret.py", is_file=True, line=None) == (
        False,
        None,
    )


async def test_file_reference_endpoint_refuses_outside_root(client, codebase_root, tmp_path):
    """End-to-end: the router reaches the containment check, not just the helper."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "main.py").write_text("x = 1\n")
    resp = await client.post(
        "/api/validation/file-reference",
        json={"file_path": "main.py", "codebase_path": str(outside)},
    )
    assert resp.status_code == 400
    assert "outside the configured codebase root" in resp.json()["detail"]


def test_returned_path_is_the_normalised_one(codebase_root):
    """The caller's string is never propagated: the helper returns the realpath."""
    link = codebase_root / "linked-repo"
    real = codebase_root / "real-repo"
    real.mkdir()
    link.symlink_to(real, target_is_directory=True)
    resolved = _validate_explicit_path(str(link))
    assert resolved == os.path.realpath(str(real))
    assert resolved != str(link)
