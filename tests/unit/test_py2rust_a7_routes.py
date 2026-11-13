"""py2rust A7 (coding-gateway#101): coding-testing's new operations are routable.

n8n-nodes-coding is generated from this gateway's aggregated OpenAPI, and every
conveyor call goes through this proxy. The aggregator publishes every backend
path whether or not the proxy routes it, so an operation can be in the spec and
still 404 here ("No backend route matched"). These tests pin:

* ``/api/parity`` (A5a schema-diff, A5b openapi-diff, A5c record/replay/compare) and
  ``/api/tests/workspace/...`` (A2 workspace test runs) resolve to coding-testing;
* the proxy forwards path, query, body and the auth/tenant headers unchanged
  (client-supplied identity is still stripped and the gateway tenant injected);
* the long-running workspace ops get a budget above their declared ceilings and
  their cheap neighbours stay on the default;
* G1: every operation in the pre-A7 baseline index is still published with the
  same method, path and operationId, and every indexed operation is routable.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

_DATA = Path(__file__).resolve().parent.parent / "data"
_INDEX = _DATA / "coding_operation_index.json"
_BASELINE = _DATA / "coding_operation_index.g1_baseline.json"

# Exactly what coding-testing's integration branch declares (generated from its app
# factory at py2rust/phase1-parity-and-tests 0c840c4: A2, A5a, A5b, A5c).
_A7_OPERATIONS = {
    ("post", "/api/tests/workspace/{container_name}/runs", "run_workspace_tests"),
    ("get", "/api/tests/workspace/{container_name}/runs/{run_id}", "get_workspace_test_run"),
    ("post", "/api/parity/openapi-diff", "parity_openapi_diff"),
    ("post", "/api/parity/{container_name}/openapi-diff", "parity_workspace_openapi_diff"),
    ("post", "/api/parity/schema-diff", "parity_schema_diff"),
    ("post", "/api/parity/{container_name}/schema-diff", "parity_workspace_schema_diff"),
    ("post", "/api/parity/{container_name}/record", "parity_behaviour_record"),
    ("post", "/api/parity/{container_name}/replay", "parity_behaviour_replay"),
    ("post", "/api/parity/{container_name}/compare", "parity_behaviour_compare"),
    ("get", "/api/parity/{container_name}/runs/{run_id}", "get_parity_behaviour_run"),
}

# Indexed AFTER A7 by later fleet merges; listed so the A7 delta check stays exact.
# coding-analysis #130 (structure/*) and coding-security #116 (sbom dependencies).
_POST_A7_OPERATIONS = {
    ("post", "/api/analysis/structure/module-graph", "structure_module_graph"),
    ("post", "/api/analysis/structure/api-surface", "structure_api_surface"),
    ("post", "/api/analysis/structure/tables", "structure_db_tables"),
    ("post", "/api/analysis/structure/entrypoints", "structure_entrypoints"),
    ("post", "/api/analysis/structure/service-graph", "structure_service_graph"),
    ("post", "/api/security/sbom/{container_name}/dependencies", "list_workspace_dependencies"),
}

# coding-testing#89 removed the double-mounted duplicate of POST /api/tests/execute
# (same handler, published as testing_execute_test).
_REMOVED_UPSTREAM = {
    ("post", "/api/tests/execute/execute", "execute_containerized_tests"),
}

# coding-testing#89: async (202 + run_id) starts and their polls.
_ASYNC_PARITY_OPERATIONS = {
    ("post", "/api/parity/{container_name}/schema-diff/runs", "parity_workspace_schema_diff_start"),
    (
        "get",
        "/api/parity/{container_name}/schema-diff/runs/{run_id}",
        "get_parity_workspace_schema_diff_run",
    ),
    (
        "post",
        "/api/parity/{container_name}/openapi-diff/runs",
        "parity_workspace_openapi_diff_start",
    ),
    (
        "get",
        "/api/parity/{container_name}/openapi-diff/runs/{run_id}",
        "get_parity_workspace_openapi_diff_run",
    ),
    ("post", "/api/parity/{container_name}/perf/runs", "parity_perf_start"),
    ("get", "/api/parity/{container_name}/perf/runs/{run_id}", "get_parity_perf_run"),
}

_TESTING_PATHS = [
    "/api/tests/workspace/aa-ws/runs",
    "/api/tests/workspace/aa-ws/runs/0123456789abcdef0123456789abcdef.job-1",
    "/api/parity/openapi-diff",
    "/api/parity/aa-ws/openapi-diff",
    "/api/parity/schema-diff",
    "/api/parity/aa-ws/schema-diff",
    "/api/parity/aa-ws/record",
    "/api/parity/aa-ws/replay",
    "/api/parity/aa-ws/compare",
    "/api/parity/aa-ws/runs/" + "a" * 32 + ".job-1",
    "/api/parity",
]


def _ops(path: Path) -> set[tuple[str, str, str]]:
    with open(path) as fh:
        return {(o["method"], o["path"], o["operationId"]) for o in json.load(fh)["operations"]}


def _mock_backend(status: int = 200, body: bytes = b'{"equal": true}') -> AsyncMock:
    resp = httpx.Response(status, content=body, headers={"content-type": "application/json"})
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=resp)
    return mock_client


# --------------------------------------------------------------------------- #
# Routing table
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", _TESTING_PATHS)
def test_new_paths_resolve_to_coding_testing(path: str):
    from config.settings import get_settings
    from routers.proxy import PROXY_ROUTES, _resolve_backend

    testing = get_settings().testing_url
    assert PROXY_ROUTES["/api/parity"] == testing
    assert _resolve_backend(path) == (testing, f"{testing}{path}")


@pytest.mark.parametrize("path", ["/api/parityx/aa-ws/openapi-diff", "/api/paritycheck"])
def test_prefix_match_is_segment_bounded(path: str):
    """Negative control: `/api/parity` must not swallow a lookalike prefix."""
    from routers.proxy import _resolve_backend

    assert _resolve_backend(path) is None


# --------------------------------------------------------------------------- #
# Forwarding: path + query + body + auth/tenant headers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        (
            "post",
            "/api/parity/aa-ws/openapi-diff",
            {"sources": [{"command": ["cat", "openapi.json"]}], "target": {"file": "rs.json"}},
        ),
        ("post", "/api/parity/openapi-diff", {"sources": [{"document": {}}], "target": {}}),
        (
            "post",
            "/api/parity/aa-ws/schema-diff",
            {"source": {"migrate": ["alembic", "upgrade", "head"]}, "target": {}},
        ),
        ("post", "/api/parity/aa-ws/compare", {"async": True, "cases_file": "cases.jsonl"}),
        ("get", "/api/parity/aa-ws/runs/" + "b" * 32 + ".job-3", None),
        ("post", "/api/tests/workspace/aa-ws/runs", {"async": True, "with_postgres": True}),
        ("get", "/api/tests/workspace/aa-ws/runs/" + "a" * 32 + ".job-7", None),
    ],
)
async def test_proxy_forwards_path_body_and_identity(app, client, method, path, body):
    from auth.dependencies import require_tenant

    resolved_tenant = "0195f48b-d000-7000-8000-000000000001"
    app.dependency_overrides[require_tenant] = lambda: resolved_tenant
    mock = _mock_backend()
    try:
        with patch("routers.proxy.get_proxy_client", return_value=mock):
            kwargs: dict = {
                "headers": {
                    "authorization": "Bearer caller-licence-token",
                    "x-project-id": "proj-1",
                    # client-supplied identity: must be replaced / dropped
                    "x-tenant-id": "0195f48b-d000-7000-8000-0000000000ff",
                    "x-user-roles": "admin",
                },
                "params": {"trace": "1"},
            }
            if body is not None:
                kwargs["json"] = body
            resp = await getattr(client, method)(path, **kwargs)
    finally:
        app.dependency_overrides.pop(require_tenant, None)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"equal": True}
    call = mock.request.call_args.kwargs
    assert call["method"] == method.upper()

    from config.settings import get_settings

    assert call["url"] == f"{get_settings().testing_url}{path}?trace=1"
    if body is None:
        assert call["content"] is None
    else:
        assert json.loads(call["content"]) == body
    headers = call["headers"]
    assert headers["x-tenant-id"] == resolved_tenant
    assert "x-user-roles" not in headers
    assert headers["authorization"] == "Bearer caller-licence-token"
    assert headers["x-project-id"] == "proj-1"


async def test_parity_refused_without_a_tenant(app, client):
    """No tenant context => refused at the gateway; coding-testing is never called."""
    from fastapi import HTTPException

    from auth.dependencies import require_tenant

    def _no_tenant() -> str:
        raise HTTPException(status_code=401, detail="tenant required")

    app.dependency_overrides[require_tenant] = _no_tenant
    mock = _mock_backend()
    try:
        with patch("routers.proxy.get_proxy_client", return_value=mock):
            resp = await client.post("/api/parity/aa-ws/openapi-diff", json={})
    finally:
        app.dependency_overrides.pop(require_tenant, None)
    assert resp.status_code == 401, resp.text
    mock.request.assert_not_called()


# --------------------------------------------------------------------------- #
# Read budgets
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("path", "ceiling"),
    [
        # ls 60 + ws-pg 120 + config 60 + suite le=300 (sync cap) + JUnit read 60
        ("/api/tests/workspace/aa-ws/runs", 600),
        ("/api/tests/workspace/aa-ws/runs/", 600),
        # one sync document command le=300 + parity_toml read 60
        ("/api/parity/aa-ws/openapi-diff", 360),
    ],
)
def test_workspace_ops_outlast_their_declared_ceiling(path: str, ceiling: int):
    from routers import proxy as pm

    assert pm.read_timeout_for("POST", path) == pytest.approx(
        pm.settings.proxy_read_timeout_workspace_run
    )
    assert pm.read_timeout_for("POST", path) > ceiling


@pytest.mark.parametrize(
    ("path", "setting", "ceiling"),
    [
        # ws-pg start + createdb x2 + migrate x2 + introspect x2, each le=300
        ("/api/parity/aa-ws/schema-diff", "proxy_read_timeout_workspace_schema_diff", 2100),
        ("/api/parity/aa-ws/schema-diff/", "proxy_read_timeout_workspace_schema_diff", 2100),
        # sync: 3 file writes x60 + driver le=300 + 3 reads x60
        ("/api/parity/aa-ws/record", "proxy_read_timeout_behaviour_parity", 660),
        ("/api/parity/aa-ws/replay", "proxy_read_timeout_behaviour_parity", 660),
        ("/api/parity/aa-ws/compare/", "proxy_read_timeout_behaviour_parity", 660),
    ],
)
def test_a5a_a5c_workspace_ops_outlast_their_declared_ceiling(path, setting, ceiling):
    from routers import proxy as pm

    assert pm.read_timeout_for("POST", path) == pytest.approx(getattr(pm.settings, setting))
    assert pm.read_timeout_for("POST", path) > ceiling


@pytest.mark.parametrize(
    "path",
    [
        "/api/tests/workspace/aa-ws/runs/" + "a" * 32,  # async poll: cheap
        "/api/parity/openapi-diff",  # inline documents: pure compute
        "/api/parity/schema-diff",  # two DSNs, read-only introspection
        "/api/parity/aa-ws/runs/" + "a" * 32 + ".job-1",  # behaviour poll: cheap
        "/api/parity/aa-ws/recordx",  # negative control: the op is segment-bounded
        "/api/tests/execute",
        # coding-testing#89 async starts answer 202 after staging; polls are cheap.
        "/api/parity/aa-ws/schema-diff/runs",
        "/api/parity/aa-ws/schema-diff/runs/" + "a" * 32 + ".job-1",
        "/api/parity/aa-ws/openapi-diff/runs",
        "/api/parity/aa-ws/openapi-diff/runs/" + "a" * 32 + ".job-1",
        "/api/parity/aa-ws/perf/runs",
        "/api/parity/aa-ws/perf/runs/" + "a" * 32 + ".job-1",
    ],
)
def test_neighbours_keep_the_default_budget(path: str):
    from routers import proxy as pm

    for method in ("GET", "POST"):
        assert pm.read_timeout_for(method, path) == pytest.approx(
            pm.settings.proxy_read_timeout_default
        )


# --------------------------------------------------------------------------- #
# Operation index (G1)
# --------------------------------------------------------------------------- #
def test_every_baseline_operation_is_unchanged():
    """G1: nothing that was published before A7 was removed, moved or renamed."""
    baseline = _ops(_BASELINE)
    assert len(baseline) > 250, "baseline looks truncated"
    missing = baseline - _ops(_INDEX) - _REMOVED_UPSTREAM
    assert not missing, f"pre-A7 operations no longer in the index: {sorted(missing)}"


def test_a7_adds_exactly_the_upstream_operations():
    current = _ops(_INDEX)
    assert _A7_OPERATIONS <= current
    assert not (_REMOVED_UPSTREAM & current), "removed upstream op is still indexed"
    added = current - _ops(_BASELINE)
    expected = _A7_OPERATIONS | _POST_A7_OPERATIONS | _ASYNC_PARITY_OPERATIONS
    assert added == expected, f"unexpected index delta: {sorted(added ^ expected)}"


def test_post_a7_operations_are_read_only_observers():
    """coding-analysis #130 / coding-security #116 ops are POST-but-read: no verifier owed."""
    import operation_metadata as om

    for _method, _path, op_id in _POST_A7_OPERATIONS:
        assert om.role_for(op_id, "post") == "observer", op_id
        op = {"operationId": op_id}
        om.annotate_operation(op, "post")
        assert op["x-operation-role"] == "observer", op_id
        assert op.get("x-idempotent") is not False, op_id


# Published by a backend but deliberately NOT reachable through the gateway.
# coding-github's internal clone-token mint is called pod-to-pod by
# coding-containers only; exposing it north-south would let any gateway caller
# mint a repo token. Pre-existing (not touched by A7) -- listed so the check
# below stays strict for everything else.
_NOT_PROXIED_BY_DESIGN = {"/api/internal/github/clone-token"}


def _route_paths(routes, prefix=""):
    """Every path the app actually serves, flattened and prefixed.

    FastAPI <= 0.136 splices `include_router` into `app.routes` as plain
    APIRoute objects carrying their full path. FastAPI >= 0.162 (starlette
    1.10) keeps the included router WRAPPED: `app.routes` holds an
    `_IncludedRouter` whose routes live on `.original_router.routes` with only
    their RELATIVE paths, and the mount prefix sits on
    `.include_context.prefix`. Nothing is flattened, not even by
    `app.openapi()` — iterating the top level finds 4 routes instead of the 14
    the app serves, so every proxied backend operation looked unroutable. That
    is exactly how this failed on the 2026-11-06 dependency upgrade.
    Walk both shapes so the assertion tests the app, not the framework version.
    """
    from fastapi.routing import APIRoute

    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path
            continue
        inner = getattr(route, "original_router", None)
        if inner is not None:
            context = getattr(route, "include_context", None)
            yield from _route_paths(
                inner.routes,
                prefix
                + (getattr(context, "prefix", "") or "")
                + (getattr(inner, "prefix", "") or ""),
            )
        elif hasattr(route, "routes"):
            yield from _route_paths(route.routes, prefix)


def test_every_indexed_operation_is_routable(app):
    """Every published operation is either gateway-owned or proxied somewhere.

    The aggregator merges backend paths regardless of PROXY_ROUTES, so a missing
    prefix shows up as a spec entry that 404s -- exactly the A7 gap for /api/parity.
    """

    from routers.proxy import _resolve_backend

    owned = {p for p in _route_paths(app.routes) if p != "/{path:path}"}
    unroutable = sorted(
        (m, p, i)
        for m, p, i in _ops(_INDEX)
        if p not in owned and p not in _NOT_PROXIED_BY_DESIGN and _resolve_backend(p) is None
    )
    assert not unroutable, f"indexed operations no route reaches: {unroutable}"
    # The exemption must not hide a path the proxy can actually reach (stale entry).
    assert all(_resolve_backend(p) is None for p in _NOT_PROXIED_BY_DESIGN)


def test_new_operations_are_classified():
    import operation_metadata as om

    assert om.role_for("run_workspace_tests", "post") == "observer"
    assert om.role_for("parity_openapi_diff", "post") == "observer"
    assert om.role_for("parity_workspace_openapi_diff", "post") == "observer"
    assert om.role_for("get_workspace_test_run", "get") == "observer"
    for op_id in (
        "parity_schema_diff",
        "parity_workspace_schema_diff",
        "parity_behaviour_record",
        "parity_behaviour_replay",
        "parity_behaviour_compare",
    ):
        assert om.role_for(op_id, "post") == "observer", op_id
    assert om.role_for("get_parity_behaviour_run", "get") == "observer"
    for op_id in (
        "run_workspace_tests",
        "parity_workspace_openapi_diff",
        "parity_workspace_schema_diff",
        "parity_behaviour_record",
        "parity_behaviour_replay",
        "parity_behaviour_compare",
    ):
        op = {"operationId": op_id}
        om.annotate_operation(op, "post")
        assert op["x-idempotent"] is False, op_id
    # read-only / pure-compute observers stay retry-safe (no explicit false)
    for op_id, method in (("parity_schema_diff", "post"), ("get_parity_behaviour_run", "get")):
        op = {"operationId": op_id}
        om.annotate_operation(op, method)
        assert op.get("x-idempotent") is not False, op_id


def test_async_parity_operations_are_classified():
    """Starts mirror the sync parity ops (observer, not replay-safe); polls are observers."""
    import operation_metadata as om

    for method, _path, op_id in _ASYNC_PARITY_OPERATIONS:
        op = {"operationId": op_id}
        om.annotate_operation(op, method)
        assert op["x-operation-role"] == "observer", op_id
        if method == "post":
            assert op["x-idempotent"] is False, op_id
        else:
            assert op.get("x-idempotent") is not False, op_id
