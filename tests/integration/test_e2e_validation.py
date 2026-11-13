"""E2E tests for validation endpoints.

These are mock/ASGI-only tests: they drive the app through httpx's
ASGITransport with all backend dependencies patched, so they need no running
service, database, or network. They are NOT integration tests and therefore
carry no ``pytest.mark.integration`` marker.
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock, patch


async def test_validate_file_reference_success(client, tmp_path):
    """POST /api/validation/file-reference returns valid=True for existing file."""
    mock_validator = MagicMock()
    mock_validator.validate_file_reference.return_value = (True, None)

    with patch("routers.validation.ReferenceValidator", return_value=mock_validator):
        resp = await client.post(
            "/api/validation/file-reference",
            json={"file_path": "src/main.py", "codebase_path": str(tmp_path)},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["valid"] is True
        assert body["file_path"] == "src/main.py"
        assert body["suggestion"] is None


async def test_validate_file_reference_not_found(client, tmp_path):
    """POST /api/validation/file-reference returns valid=False with suggestion."""
    mock_validator = MagicMock()
    mock_validator.validate_file_reference.return_value = (False, "src/main.py")

    with patch("routers.validation.ReferenceValidator", return_value=mock_validator):
        resp = await client.post(
            "/api/validation/file-reference",
            json={"file_path": "src/mian.py", "codebase_path": str(tmp_path)},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["valid"] is False
        assert body["suggestion"] == "src/main.py"


async def test_validate_function_reference_success(client, tmp_path):
    """POST /api/validation/function-reference returns valid=True."""
    mock_validator = MagicMock()
    mock_validator.validate_function_reference.return_value = (
        True,
        {"file": "src/main.py", "line": 10},
    )

    with patch("routers.validation.ReferenceValidator", return_value=mock_validator):
        resp = await client.post(
            "/api/validation/function-reference",
            json={"function_name": "create_app", "codebase_path": str(tmp_path)},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["valid"] is True
        assert body["function_name"] == "create_app"
        assert body["details"]["file"] == "src/main.py"
        assert body["details"]["line"] == 10


async def test_validate_function_reference_not_found(client, tmp_path):
    """POST /api/validation/function-reference returns valid=False."""
    mock_validator = MagicMock()
    mock_validator.validate_function_reference.return_value = (False, None)

    with patch("routers.validation.ReferenceValidator", return_value=mock_validator):
        resp = await client.post(
            "/api/validation/function-reference",
            json={"function_name": "nonexistent_func", "codebase_path": str(tmp_path)},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["valid"] is False


async def test_validate_code_snippet_missing_fields(client):
    """POST /api/validation/code-snippet without required fields returns 400."""
    resp = await client.post(
        "/api/validation/code-snippet",
        json={"code_snippet": "print('hello')"},
    )
    assert resp.status_code == 400
    body = resp.json()
    # The exception handler wraps HTTPException.detail into "message"
    msg = body.get("message", "") or body.get("detail", "")
    assert "codebase_path" in msg or "container_name" in msg


async def test_validate_code_snippet_success(client):
    """POST /api/validation/code-snippet with valid path returns validation result."""
    mock_validator = MagicMock()
    mock_validator.validate_code_snippet.return_value = {
        "valid": True,
        "issues": [],
        "suggestion": None,
        "overall_score": 0.95,
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create enough depth to pass the shallow-path check
        deep_path = os.path.join(tmpdir, "workspace", "repo")
        os.makedirs(deep_path, exist_ok=True)

        with patch("routers.validation.ReferenceValidator", return_value=mock_validator):
            resp = await client.post(
                "/api/validation/code-snippet",
                json={
                    "code_snippet": "import os\nprint(os.getcwd())",
                    "codebase_path": deep_path,
                },
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["valid"] is True
            assert body["overall_score"] == 0.95


async def test_validate_code_snippet_empty_string(client):
    """POST /api/validation/code-snippet with empty code_snippet returns 400."""
    resp = await client.post(
        "/api/validation/code-snippet",
        json={"code_snippet": "", "codebase_path": "/some/path"},
    )
    assert resp.status_code == 400
