"""Every operation in the aggregated /openapi.json carries ``x-request-timeout-ms``.

The value is the proxy's own read budget for that route (``proxy.read_timeout_for``)
plus ``client_request_timeout_margin``, so a generated client (n8n-nodes-coding)
needs no hand-written per-operation timeout table and always outlasts the gateway.

The backends are not running in unit tests, so one backend's /openapi.json is
served from the committed operation index (every published op); everything after
that -- merge, post-processing, the budget lookup, the HTTP response -- is real.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import openapi_aggregator

_INDEX = Path(__file__).resolve().parent.parent / "data" / "coding_operation_index.json"
_OPS = json.loads(_INDEX.read_text())["operations"]
_HTTP = {"get", "put", "post", "delete", "patch", "options", "head", "trace"}

# The ops that today carry a hand-written n8n timeout (n8n_module_scripts
# services.json [coding].requestTimeouts), pinned at the gateway's default budgets.
_EXPECTED_MS = {
    "trigger_architecture_analysis": 330_000,
    "run_quality_analysis_sync": 330_000,
    "evaluate_quality_gate": 330_000,
    "exec_in_workspace": 360_000,
    "push_workspace_to_fork": 990_000,
    "fetch_workspace_ref": 990_000,
    "import_repository_as_workspace": 690_000,
    "package_upgrade_in_workspace": 1_890_000,
    "package_modify_in_workspace": 1_890_000,
    "upgrade_in_range_packages": 1_890_000,
    "upgrade_cross_major_packages": 1_890_000,
    "upgrade_cves": 1_890_000,
    "add_dependencies": 1_890_000,
    "remove_dependencies": 1_890_000,
    "replace_dependencies": 1_890_000,
    "parity_workspace_openapi_diff": 660_000,
    "parity_workspace_schema_diff": 2_160_000,
    "run_workspace_tests": 660_000,
    "parity_behaviour_record": 720_000,
    "parity_behaviour_replay": 720_000,
    "parity_behaviour_compare": 720_000,
    "add_workspace_repo": 990_000,
    "start_workspace_service": 360_000,
    # Negative controls: cheap neighbours stay on the 120 s default.
    "get_workspace_test_run": 150_000,
    "parity_openapi_diff": 150_000,
    # Same PATH as a long POST, but a cheap GET: the budget is keyed on method + path.
    # RED against the path-only lookup, which published 990_000 / 360_000 for these.
    "list_workspace_repos": 150_000,
    "list_workspace_services": 150_000,
}


def _backend_spec() -> dict:
    paths: dict[str, dict] = {}
    for op in _OPS:
        paths.setdefault(op["path"], {})[op["method"]] = {
            "operationId": op["operationId"],
            "responses": {"200": {"description": "OK"}},
        }
    return {"openapi": "3.1.0", "info": {"title": "index", "version": "0"}, "paths": paths}


async def _fake_fetch(_client, name, _url):
    return _backend_spec() if name == "containers" else None


@pytest.fixture(autouse=True)
def _clear_cache():
    openapi_aggregator.invalidate_cache()
    yield
    openapi_aggregator.invalidate_cache()


async def _served_ops(client) -> dict[str, dict]:
    with patch("openapi_aggregator._fetch_spec", _fake_fetch):
        resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    return {
        op["operationId"]: op
        for item in resp.json()["paths"].values()
        for method, op in item.items()
        if method in _HTTP and isinstance(op, dict) and "operationId" in op
    }


async def test_every_operation_carries_an_integer_timeout(client):
    ops = await _served_ops(client)
    assert {o["operationId"] for o in _OPS} <= ops.keys()
    from routers import proxy

    margin_ms = int(proxy.settings.client_request_timeout_margin * 1000)
    default_ms = int(proxy.settings.proxy_read_timeout_default * 1000)
    for op_id, op in ops.items():
        value = op.get("x-request-timeout-ms")
        assert isinstance(value, int) and not isinstance(value, bool), op_id
        assert value >= default_ms + margin_ms, op_id


@pytest.mark.parametrize(("op_id", "expected"), sorted(_EXPECTED_MS.items()))
async def test_long_ops_publish_budget_plus_margin(client, op_id, expected):
    ops = await _served_ops(client)
    assert ops[op_id]["x-request-timeout-ms"] == expected


async def test_value_follows_the_proxy_budget_setting(client, monkeypatch):
    """One source of truth: retuning the proxy budget retunes the published timeout."""
    from routers import proxy

    monkeypatch.setattr(proxy.settings, "proxy_read_timeout_analysis", 900.0)
    ops = await _served_ops(client)
    assert ops["evaluate_quality_gate"]["x-request-timeout-ms"] == 930_000
    assert ops["get_workspace_test_run"]["x-request-timeout-ms"] == 150_000


async def test_only_post_operations_carry_a_write_budget(client):
    """Every published op above the default is one of the verbs a budget was derived for:
    POST writes, plus the analyzer's GET gate. No other GET may inherit a long budget."""
    from routers import proxy

    default_ms = int(
        (proxy.settings.proxy_read_timeout_default + proxy.settings.client_request_timeout_margin)
        * 1000
    )
    with patch("openapi_aggregator._fetch_spec", _fake_fetch):
        paths = (await client.get("/openapi.json")).json()["paths"]
    long_non_post = sorted(
        (method, path)
        for path, item in paths.items()
        for method, op in item.items()
        if method in _HTTP
        and method != "post"
        and isinstance(op, dict)
        and op.get("x-request-timeout-ms", default_ms) > default_ms
    )
    assert long_non_post == [("get", "/api/quality/{container_name}/gate")]


async def test_info_carries_n8n_node_identity(client):
    """info.title -> node displayName; info["x-n8n"] -> node description + projectId."""
    with patch("openapi_aggregator._fetch_spec", _fake_fetch):
        info = (await client.get("/openapi.json")).json()["info"]
    assert info["title"] == "Code Graph Helper"
    assert info["x-n8n"] == {
        "description": (
            "Code Graph Helper platform: code graphs, static and semantic analysis, "
            "quality and security checks, testing, GitHub and pull request actions, "
            "and workspaces"
        ),
        "projectIdRequired": True,
    }
