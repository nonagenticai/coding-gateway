"""X-Project-Id counter + REQUIRE_PROJECT_ID_HEADER gate (middleware/project_id.py).

Mirrors worker_management #355's contract: counted once per AUTHENTICATED
non-public request, caller = what the gateway authenticated (never a
self-declared header), 400 PROJECT_ID_REQUIRED only behind the flag.

The SDK AuthMiddleware is stubbed to a passthrough by conftest, so these tests
put a minimal stand-in in front of ``ProjectIdMiddleware`` that does what the
real chain does at the boundary that matters here: public paths pass with no
``request.state.user``, a credential-less request is a 401, anything else gets a
user. The licence introspection and the header-trust gate are the SDK's, so
they are replaced on the module under test.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI, Request
from httpx._transports.asgi import ASGITransport
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from starlette.responses import JSONResponse

import middleware.project_id as pid
from config.settings import Settings, get_settings

LIC_A = "11111111-1111-4111-8111-111111111111"
LIC_B = "22222222-2222-4222-8222-222222222222"
LIC_C = "33333333-3333-4333-8333-333333333333"
PROJECT = "0199aaaa-bbbb-7ccc-8ddd-eeeeeeeeeeee"
TOKENS = {"tok-a": LIC_A, "tok-b": LIC_B, "tok-c": LIC_C}


class _FakeAuth:
    """Stand-in for the SDK chain's admit/reject boundary (see module docstring)."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope["path"] != "/health":
            headers = dict(scope["headers"])
            if b"authorization" not in headers and b"x-trusted-net" not in headers:
                await JSONResponse({"detail": "Authentication required"}, 401)(scope, receive, send)
                return
            scope.setdefault("state", {})["user"] = object()
        await self.app(scope, receive, send)


@pytest.fixture
def reader(monkeypatch):
    """Swap both counters for ones on a private provider we can read back."""
    rdr = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[rdr]).get_meter("test")
    for attr, name in (
        ("PROJECT_ID_MISSING_TOTAL", "coding_gateway_project_id_missing_total"),
        ("PROJECT_ID_REFUSED_TOTAL", "coding_gateway_project_id_refused_total"),
    ):
        monkeypatch.setattr(pid, attr, meter.create_counter(name, unit="1"))
    return rdr


def _points(reader, name: str) -> dict[tuple[str, str], int]:
    data = reader.get_metrics_data()
    out: dict[tuple[str, str], int] = {}
    for rm in data.resource_metrics if data else []:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name != name:
                    continue
                for dp in metric.data.data_points:
                    attrs = dict(dp.attributes or {})
                    out[(str(attrs["caller"]), str(attrs["reason"]))] = int(dp.value)
    return out


def _missing(reader):
    return _points(reader, "coding_gateway_project_id_missing_total")


def _refused(reader):
    return _points(reader, "coding_gateway_project_id_refused_total")


@pytest.fixture
def gate_env(monkeypatch, reader):
    monkeypatch.setenv("ENABLE_AUTH", "true")
    monkeypatch.delenv("REQUIRE_PROJECT_ID_HEADER", raising=False)
    monkeypatch.delenv("PROJECT_ID_CALLER_LICENCES", raising=False)
    get_settings.cache_clear()
    pid._parsed_caller_licences.cache_clear()

    async def _introspect(token: str):
        lic = TOKENS.get(token)
        return (
            {"active": True, "tenant_id": "t", "subject_id": "s", "license_id": lic}
            if lic
            else None
        )

    monkeypatch.setattr(pid, "_introspect", _introspect)
    monkeypatch.setattr(pid, "_headers_trusted", lambda: False)
    return reader


def _set_env(monkeypatch, **env: str) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    pid._parsed_caller_licences.cache_clear()


@pytest.fixture
async def client():
    inner = FastAPI()

    @inner.get("/health")
    async def health():
        return {"ok": True}

    @inner.get("/api/graphs")
    async def graphs(request: Request):
        return {"project": request.headers.get("x-project-id")}

    inner.add_middleware(pid.ProjectIdMiddleware)  # type: ignore[invalid-argument-type]
    inner.add_middleware(_FakeAuth)  # type: ignore[invalid-argument-type]
    async with httpx.AsyncClient(transport=ASGITransport(app=inner), base_url="http://t") as c:
        yield c


def _auth(token: str = "tok-a", **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **extra}


# ── flag off: count, never refuse ─────────────────────────────────────────


async def test_flag_off_absent_is_counted_and_served(gate_env, client):
    r = await client.get("/api/graphs", headers=_auth())
    assert r.status_code == 200
    assert _missing(gate_env) == {(f"licence:{LIC_A}", "absent"): 1}
    assert _refused(gate_env) == {}


async def test_flag_off_invalid_and_nil_are_counted_invalid(gate_env, client):
    for bad in ("not-a-uuid", "{" + PROJECT + "}", "00000000-0000-0000-0000-000000000000"):
        r = await client.get("/api/graphs", headers=_auth(**{"X-Project-Id": bad}))
        assert r.status_code == 200
    assert _missing(gate_env) == {(f"licence:{LIC_A}", "invalid"): 3}


async def test_valid_any_case_not_counted_and_relayed_unchanged(gate_env, client):
    for value in (PROJECT, PROJECT.upper()):
        r = await client.get("/api/graphs", headers=_auth(**{"X-Project-Id": value}))
        assert r.status_code == 200
        assert r.json() == {"project": value}
    assert _missing(gate_env) == {}


# ── flag on: 400 typed body, counted in both ─────────────────────────────


async def test_flag_on_refuses_400_with_typed_body(gate_env, client, monkeypatch):
    _set_env(monkeypatch, REQUIRE_PROJECT_ID_HEADER="true")
    r = await client.get("/api/graphs", headers=_auth())
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "PROJECT_ID_REQUIRED"
    assert err["reason"] == "absent"
    assert {"message", "timestamp", "request_id"} <= err.keys()
    r = await client.get("/api/graphs", headers=_auth(**{"X-Project-Id": "nope"}))
    assert r.status_code == 400
    assert r.json()["error"]["reason"] == "invalid"
    expected = {(f"licence:{LIC_A}", "absent"): 1, (f"licence:{LIC_A}", "invalid"): 1}
    assert _missing(gate_env) == expected
    assert _refused(gate_env) == expected


async def test_flag_on_valid_is_served(gate_env, client, monkeypatch):
    _set_env(monkeypatch, REQUIRE_PROJECT_ID_HEADER="true")
    r = await client.get("/api/graphs", headers=_auth(**{"X-Project-Id": PROJECT}))
    assert r.status_code == 200
    assert _missing(gate_env) == {} and _refused(gate_env) == {}


# ── not counted: public paths, 401, auth disabled ────────────────────────


async def test_public_path_and_401_are_not_counted(gate_env, client, monkeypatch):
    _set_env(monkeypatch, REQUIRE_PROJECT_ID_HEADER="true")
    assert (await client.get("/health")).status_code == 200
    assert (await client.get("/api/graphs")).status_code == 401
    assert _missing(gate_env) == {} and _refused(gate_env) == {}


async def test_auth_disabled_is_not_counted(gate_env, client, monkeypatch):
    _set_env(monkeypatch, ENABLE_AUTH="false", REQUIRE_PROJECT_ID_HEADER="true")
    assert (await client.get("/api/graphs", headers=_auth())).status_code == 200
    assert _missing(gate_env) == {}


# ── caller attribution ───────────────────────────────────────────────────


async def test_forged_app_and_licence_headers_do_not_move_the_label(gate_env, client):
    forged = {"X-App-Id": "n8n", "X-License-Id": LIC_B, "X-Caller": "fr"}
    await client.get("/api/graphs", headers=_auth(**forged))
    assert _missing(gate_env) == {(f"licence:{LIC_A}", "absent"): 1}


async def test_trusted_network_and_header_branch_labels(gate_env, client, monkeypatch):
    await client.get("/api/graphs", headers={"X-Trusted-Net": "1"})
    monkeypatch.setattr(pid, "_headers_trusted", lambda: True)
    # Identity-header branch: unverified claims → `unknown`, whatever they say.
    await client.get("/api/graphs", headers=_auth(**{"X-Tenant-Id": "n8n-svc"}))
    assert _missing(gate_env) == {
        ("trusted_network", "absent"): 1,
        ("unknown", "absent"): 1,
    }


async def test_licence_map_shares_names_and_leaves_unmapped_visible(gate_env, client, monkeypatch):
    _set_env(monkeypatch, PROJECT_ID_CALLER_LICENCES=f"{LIC_A}=n8n, {LIC_B.upper()}=n8n")
    for token in ("tok-a", "tok-b", "tok-c"):
        await client.get("/api/graphs", headers=_auth(token))
    assert _missing(gate_env) == {("n8n", "absent"): 2, (f"licence:{LIC_C}", "absent"): 1}


def test_licence_label_is_sanitised_and_capped(gate_env):
    weird = {"license_id": "AB/c d{" + "x" * 60 + "}"}
    label = pid.licence_caller(weird)
    assert label == "licence:abcd" + "x" * 32
    assert pid.licence_caller({"license_id": "!!"}) == "licence"
    assert pid.licence_caller(None) == "licence"


async def test_bad_map_flag_off_warns_and_falls_back(gate_env, client, monkeypatch, caplog):
    _set_env(monkeypatch, PROJECT_ID_CALLER_LICENCES=f"{LIC_A}=n8n,{LIC_A}=other")
    get_settings()  # flag off ⇒ settings still load
    await client.get("/api/graphs", headers=_auth())
    await client.get("/api/graphs", headers=_auth())
    assert _missing(gate_env) == {(f"licence:{LIC_A}", "absent"): 2}
    assert sum("PROJECT_ID_CALLER_LICENCES ignored" in m for m in caplog.messages) == 1


@pytest.mark.parametrize("raw", [f"{LIC_A}=n8n,{LIC_A}=x", "nope=n8n", f"{LIC_A}=Bad Name", LIC_A])
def test_bad_map_flag_on_fails_settings(gate_env, monkeypatch, raw):
    _set_env(monkeypatch, REQUIRE_PROJECT_ID_HEADER="true", PROJECT_ID_CALLER_LICENCES=raw)
    with pytest.raises(ValueError, match="PROJECT_ID_CALLER_LICENCES"):
        Settings()


def test_service_prefixed_env_names_bind(gate_env, monkeypatch):
    _set_env(
        monkeypatch,
        CODING_GATEWAY_REQUIRE_PROJECT_ID_HEADER="true",
        CODING_GATEWAY_PROJECT_ID_CALLER_LICENCES=f"{LIC_A}=n8n",
    )
    s = Settings()
    assert s.require_project_id_header is True
    assert s.project_id_caller_licences == f"{LIC_A}=n8n"


# ── never raising ────────────────────────────────────────────────────────


class _Boom:
    def add(self, *args, **kwargs):
        raise RuntimeError("exporter down")


async def test_raising_counter_flag_off_still_serves(gate_env, client, monkeypatch):
    monkeypatch.setattr(pid, "PROJECT_ID_MISSING_TOTAL", _Boom())
    r = await client.get("/api/graphs", headers=_auth())
    assert r.status_code == 200


async def test_raising_introspection_labels_unknown_and_serves(gate_env, client, monkeypatch):
    async def _raise(token):
        raise RuntimeError("licences down")

    monkeypatch.setattr(pid, "_introspect", _raise)
    r = await client.get("/api/graphs", headers=_auth())
    assert r.status_code == 200
    assert _missing(gate_env) == {("unknown", "absent"): 1}


# ── zero-seed + wiring ───────────────────────────────────────────────────


def test_preinit_zero_seeds_bounded_callers(gate_env, monkeypatch):
    _set_env(monkeypatch, PROJECT_ID_CALLER_LICENCES=f"{LIC_A}=n8n,{LIC_B}=n8n,{LIC_C}=fr")
    pid.preinit_project_id_metrics()
    expected = {
        (caller, reason): 0
        for caller in ("trusted_network", "unknown", "n8n", "fr")
        for reason in ("absent", "invalid")
    }
    assert _missing(gate_env) == expected
    assert _refused(gate_env) == expected


async def test_create_app_registers_gate_inside_auth(app):
    names = [m.cls.__name__ for m in app.user_middleware]
    # user_middleware is outermost-first: the gate must sit INSIDE (after) Auth.
    assert names.index("ProjectIdMiddleware") > names.index("AuthMiddleware")


def test_module_counters_carry_the_dashboard_names():
    def _name(counter) -> str:
        return getattr(counter, "name", None) or counter._name

    assert _name(pid.PROJECT_ID_MISSING_TOTAL) == "coding_gateway_project_id_missing_total"
    assert _name(pid.PROJECT_ID_REFUSED_TOTAL) == "coding_gateway_project_id_refused_total"


@pytest.mark.parametrize(
    ("result", "admitted"),
    [
        ({"active": True, "tenant_id": "t", "subject_id": "s", "license_id": "abc"}, True),
        ({"active": True, "tenant_id": "t", "subject_id": None, "license_id": "abc"}, True),
        ({"active": True, "tenant_id": "t", "license_id": "abc"}, True),
        ({"active": False, "tenant_id": "t", "subject_id": "s", "license_id": "abc"}, False),
        ({"active": True, "tenant_id": "", "subject_id": "s", "license_id": "abc"}, False),
        ({"active": True, "tenant_id": "   ", "subject_id": "s", "license_id": "abc"}, False),
        ({"active": True, "tenant_id": 123, "subject_id": "s", "license_id": "abc"}, False),
        ({"active": True, "tenant_id": "t", "subject_id": 123, "license_id": "abc"}, False),
        (None, False),
    ],
)
async def test_licence_label_follows_the_sdk_admit_decision(monkeypatch, result, admitted):
    # The label is licence:<id> exactly when the SDK's own licence branch would have
    # admitted the request; otherwise the SDK fell through to its trusted-network
    # check, and so does the label (validator findings on 02b0fb5 and 2678d88).
    from coding_client.auth.middleware import _user_from_license_data
    from starlette.requests import Request

    from src.middleware import project_id as pid

    assert (_user_from_license_data(result) is not None) is admitted  # fixture == SDK truth

    async def _fake(_token):
        return result

    monkeypatch.setattr(pid, "_introspect", _fake)
    monkeypatch.setattr(pid, "_dev_key_branch", lambda _t: False)
    monkeypatch.setattr(pid, "_headers_trusted", lambda: False)
    req = Request({"type": "http", "headers": [(b"authorization", b"Bearer tok")]})
    expected = "licence:abc" if admitted else pid.PROJECT_ID_CALLER_TRUSTED_NETWORK
    assert await pid.resolve_caller(req) == expected
