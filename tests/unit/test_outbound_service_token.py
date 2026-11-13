"""The gateway's OWN routes must prove who they are, not just assert a tenant.

`routers/proxy.py` is a different path and is deliberately not covered here: it
forwards the client's own Authorization verbatim, so those requests already
carry a verifiable credential.

These cover the routes that build a FRESH header dict — graph queries, metrics,
cache stats, validation. They previously sent a bare X-Tenant-Id and nothing
else, which works only while coding-analysis and coding-containers still trust
that header. After the flip a bare tenant proves nothing and every one of these
401s.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from coding_client.auth.outbound import TenantMissingError

from clients.outbound_identity import identity_headers, service_tenant, service_token

_REPO_ROOT = Path(__file__).resolve().parents[2]


class TestServiceTenant:
    """The gateway's own tenant is configured, never guessed.

    ``service_tenant()`` replaced ``settings.default_tenant_id``, a pydantic
    field defaulting to the literal ``"n8n-dev"``. Because the same three call
    sites also send this service's licence — and ``svc-coding-gateway`` is in
    coding-analysis's ``CODING_ONBEHALF_SUBJECTS`` — a guessed tenant was not
    rejected downstream, it was VOUCHED FOR.
    """

    def test_returns_the_configured_tenant(self, monkeypatch):
        """The happy path, asserted against an EXPLICIT value.

        Not ``== settings.default_tenant_id`` or ``== DEFAULT_TENANT_ID``: that
        shape is a tautology that compares the code to itself and stays green
        for any value, which is precisely how the old literal survived.
        """
        monkeypatch.setattr(
            "clients.outbound_identity.DEFAULT_TENANT_ID",
            "0195f48b-d000-7000-8000-000000000001",
        )
        assert service_tenant() == "0195f48b-d000-7000-8000-000000000001"

    @pytest.mark.parametrize("unset", [None, ""])
    def test_refuses_when_unset_rather_than_guessing(self, monkeypatch, unset):
        """No tenant configured -> refuse. Empty string counts as unset."""
        monkeypatch.setattr("clients.outbound_identity.DEFAULT_TENANT_ID", unset)
        with pytest.raises(TenantMissingError) as excinfo:
            service_tenant()
        assert "DEFAULT_TENANT_ID" in str(excinfo.value)

    def test_never_returns_the_stale_n8n_dev_label(self, monkeypatch):
        """The specific regression: no code path may yield ``"n8n-dev"``.

        It is the pre-uuid7 label form of a tenant that really exists, so it
        fails silently rather than loudly.
        """
        monkeypatch.setattr("clients.outbound_identity.DEFAULT_TENANT_ID", None)
        with pytest.raises(TenantMissingError):
            service_tenant()

    def test_refusal_is_not_absorbed_by_the_broad_excepts_around_it(self, monkeypatch):
        """All three call sites sit in ``except Exception`` blocks that degrade.

        ``TenantMissingError`` is deliberately not a ValueError/RuntimeError/
        OSError/httpx.HTTPError, so a narrower handler cannot turn a
        misconfiguration into "coding-analysis looks down".
        """
        monkeypatch.setattr("clients.outbound_identity.DEFAULT_TENANT_ID", None)
        for narrower in (ValueError, RuntimeError, OSError, httpx.HTTPError):
            with pytest.raises(TenantMissingError):
                try:
                    service_tenant()
                except narrower:  # pragma: no cover - must not be reached
                    raise AssertionError(
                        f"TenantMissingError was absorbed by except {narrower.__name__}"
                    ) from None


class TestIdentityHeaders:
    def test_credential_and_tenant_are_separate_headers(self, monkeypatch):
        """The whole point: the customer tenant must survive alongside the credential.

        Putting the token into X-Tenant-Id instead would make every gateway call
        resolve to the gateway's OWN tenant rather than the customer's.
        """
        monkeypatch.setenv("CODING_SERVICE_TOKEN", "gw-token-abc")
        h = identity_headers("customer-tenant-0042")
        assert h["Authorization"] == "Bearer gw-token-abc"
        assert h["X-Tenant-Id"] == "customer-tenant-0042"

    def test_no_token_configured_means_no_authorization(self, monkeypatch):
        """Absent is absent — never a fabricated or empty credential."""
        monkeypatch.delenv("CODING_SERVICE_TOKEN", raising=False)
        h = identity_headers("customer-tenant-0042")
        assert "Authorization" not in h
        assert h["X-Tenant-Id"] == "customer-tenant-0042"

    def test_an_unscoped_call_still_proves_who_is_asking(self, monkeypatch):
        monkeypatch.setenv("CODING_SERVICE_TOKEN", "gw-token-abc")
        h = identity_headers(None)
        assert h["Authorization"] == "Bearer gw-token-abc"
        assert "X-Tenant-Id" not in h

    def test_the_token_is_read_per_call_not_at_import(self, monkeypatch):
        """A rotated secret must not need a pod restart to take effect."""
        monkeypatch.setenv("CODING_SERVICE_TOKEN", "first")
        assert service_token() == "first"
        monkeypatch.setenv("CODING_SERVICE_TOKEN", "second")
        assert service_token() == "second"


class TestGraphRoutesSendTheCredential:
    """Drive the real router helper so a dropped credential fails here."""

    @pytest.mark.parametrize(
        "rel_path",
        [
            "src/routers/graphs.py",
            "src/routers/metrics.py",
            "src/routers/cache.py",
            "src/services/validation.py",
        ],
    )
    def test_no_own_route_hand_rolls_the_tenant_header(self, rel_path):
        """A route reverting to a bare dict is the regression that matters.

        Asserting only that the module *imports* identity_headers does not catch
        it — the import survives while one call site quietly goes back to
        `{"x-tenant-id": ...}` and sends no credential at all. Mutation-verified:
        that exact revert survives an import-only assertion and fails this one.

        Excludes proxy.py on purpose: it forwards the client's own Authorization
        and re-injects the tenant it resolved from that same token, so it is
        allowed to build headers by hand.
        """
        source = (_REPO_ROOT / rel_path).read_text()
        offenders = [
            line.strip()
            for line in source.splitlines()
            if '"x-tenant-id":' in line.lower() or "'x-tenant-id':" in line.lower()
        ]
        assert offenders == [], (
            f"{rel_path} builds a tenant header by hand: {offenders}. "
            "Use clients.outbound_identity.identity_headers so the service "
            "licence goes out with it."
        )

    def test_the_census_above_can_actually_fail(self):
        """Positive control: prove the detector fires on a known-bad string.

        A census that matches nothing is indistinguishable from a census that
        cannot match.
        """
        bad = 'headers = {"x-tenant-id": tenant_id}'
        assert '"x-tenant-id":' in bad.lower()

    async def test_a_graph_query_carries_both_headers(self, monkeypatch):
        monkeypatch.setenv("CODING_SERVICE_TOKEN", "gw-token-abc")
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"ok": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await client.get(
                "http://analysis/api/analysis/graphs/c1/metrics",
                headers=identity_headers("customer-tenant-0042"),
            )

        assert seen[0].headers["authorization"] == "Bearer gw-token-abc"
        assert seen[0].headers["x-tenant-id"] == "customer-tenant-0042"


class TestWebSocketUpgradeCarriesOurLicence:
    """The WS upgrade must prove who the gateway is.

    AuthMiddleware never runs on a websocket scope, so backends resolve identity
    in-handler — and they cannot demand a credential from a browser, because the
    WebSocket API cannot set an Authorization header on a handshake. The gateway
    is a server, so it can, and that is what lets the backends stop trusting a
    bare x-tenant-id on the WS path.
    """

    def _forwarded(
        self,
        monkeypatch,
        client_headers: dict[str, str],
        *,
        license_verified: bool = True,
        vouched: bool = False,
        license_id: str | None = None,
    ) -> dict[str, str]:
        """Call the REAL function proxy.py uses, not a copy of its logic.

        The first version of this helper re-implemented the rule, and a mutation
        that clobbered a client-supplied credential survived — the test could not
        see the change it was guarding.
        """
        from routers.proxy import _attach_service_credential

        forwarded = dict(client_headers)
        _attach_service_credential(
            forwarded,
            license_verified=license_verified,
            vouched=vouched,
            license_id=license_id,
        )
        return forwarded

    def test_the_upgrade_carries_the_gateway_licence(self, monkeypatch):
        monkeypatch.setenv("CODING_SERVICE_TOKEN", "gw-token-abc")
        h = self._forwarded(monkeypatch, {"x-tenant-id": "customer-0042"})
        assert h["authorization"] == "Bearer gw-token-abc"
        # The CUSTOMER tenant must survive alongside it — that pair is the whole
        # on-behalf-of shape.
        assert h["x-tenant-id"] == "customer-0042"

    def test_a_client_supplied_licence_is_not_overwritten(self, monkeypatch):
        """Its tenant is the one this gateway resolved FROM it, so they agree and
        no vouch is needed. Overwriting would discard the customer's attribution."""
        monkeypatch.setenv("CODING_SERVICE_TOKEN", "gw-token-abc")
        h = self._forwarded(monkeypatch, {"authorization": "Bearer customer-licence"})
        assert h["authorization"] == "Bearer customer-licence"

    def test_no_token_configured_adds_no_authorization(self, monkeypatch):
        monkeypatch.delenv("CODING_SERVICE_TOKEN", raising=False)
        h = self._forwarded(monkeypatch, {"x-tenant-id": "customer-0042"})
        assert "authorization" not in h


class TestAnUnverifiedTenantEarnsNoCredential:
    """The security boundary, and the one that was open in production.

    Driven live on 2026-08-14 against the deployed pods, BEFORE this change::

        ws://…/api/quality/ws/probe-forged-tenant
        x-tenant-id: 0195f48b-…   (forged, NO credential)   -> UPGRADE ACCEPTED

        coding-analysis:
        Vouched on-behalf-of: subject '019ffa19-…' (svc-coding-gateway)
        asserted tenant '0195f48b-…'

    The gateway accepted a tenant it never verified (header trust), attached its
    OWN allowlisted licence, and the callee honoured the assertion. Any pod could
    read any tenant over the WebSocket surface — and that was the fleet's first
    ever `Vouched on-behalf-of` line in production.

    The gateway's credential is an assertion that IT checked something. Lending it
    to a claim we never checked hands the callee's allowlist to the wrong party.
    """

    def _forwarded(self, headers, *, license_verified, vouched=False, license_id=None):
        from routers.proxy import _attach_service_credential

        forwarded = dict(headers)
        _attach_service_credential(
            forwarded,
            license_verified=license_verified,
            vouched=vouched,
            license_id=license_id,
        )
        return forwarded

    def test_a_trusted_header_tenant_gets_no_credential_from_us(self, monkeypatch):
        """THE regression test for the live hole. The forged upgrade, in unit form."""
        monkeypatch.setenv("CODING_SERVICE_TOKEN", "gw-token-abc")
        h = self._forwarded(
            {"x-tenant-id": "0195f48b-d000-7000-8000-000000000003"},
            license_verified=False,
        )
        assert "authorization" not in h, (
            "the gateway lent its allowlisted licence to a tenant it never verified; "
            "this is the forged-WebSocket read reproduced"
        )
        # The header still goes out — the BACKEND refuses it, which is correct and
        # is what makes this fail closed rather than fail silent.
        assert h["x-tenant-id"] == "0195f48b-d000-7000-8000-000000000003"

    def test_an_unverified_tenant_cannot_be_vouched_for_either(self, monkeypatch):
        """`license_verified` is checked FIRST. A `vouched` flag can never be True
        without it in real code, but the ordering must not depend on that: if the
        two ever disagree, unverified has to win."""
        monkeypatch.setenv("CODING_SERVICE_TOKEN", "gw-token-abc")
        h = self._forwarded({"x-tenant-id": "forged"}, license_verified=False, vouched=True)
        assert "authorization" not in h

    def test_an_unverified_request_keeps_the_callers_own_credential(self, monkeypatch):
        """Untouched, not stripped. Dropping it would turn a misconfiguration into
        a fleet-wide 401 on the trusted-header path that is still live today."""
        monkeypatch.setenv("CODING_SERVICE_TOKEN", "gw-token-abc")
        h = self._forwarded({"authorization": "Bearer customer-licence"}, license_verified=False)
        assert h["authorization"] == "Bearer customer-licence"

    def test_a_verified_tenant_still_gets_the_credential(self, monkeypatch):
        """Non-vacuity control. Without this, `return` at the top of the function
        would satisfy every assertion in this class and break the whole feature."""
        monkeypatch.setenv("CODING_SERVICE_TOKEN", "gw-token-abc")
        h = self._forwarded({"x-tenant-id": "customer-0042"}, license_verified=True)
        assert h["authorization"] == "Bearer gw-token-abc"


class TestAVouchedCallSubstitutesOurCredential:
    """The caller's credential is kept only while it AGREES with the tenant.

    On a vouched call it does not: the credential names the caller's own tenant
    while the injected `x-tenant-id` names the customer's. Forwarding that pair
    forwards a disagreement, and the next hop refuses disagreements — which is
    n8n's AA dispatch 403ing at coding-containers in production today.
    """

    def _forwarded(self, headers, *, vouched, license_id=None, license_verified=True):
        from routers.proxy import _attach_service_credential

        forwarded = dict(headers)
        _attach_service_credential(
            forwarded,
            license_verified=license_verified,
            vouched=vouched,
            license_id=license_id,
        )
        return forwarded

    def test_a_vouched_call_replaces_the_callers_credential(self, monkeypatch):
        monkeypatch.setenv("CODING_SERVICE_TOKEN", "gw-token-abc")
        h = self._forwarded(
            {"authorization": "Bearer n8n-licence", "x-tenant-id": "platform-cp-3"},
            vouched=True,
        )
        assert h["authorization"] == "Bearer gw-token-abc"
        # The asserted CUSTOMER tenant is what we are vouching for; it must survive.
        assert h["x-tenant-id"] == "platform-cp-3"

    def test_an_unvouched_call_keeps_it(self, monkeypatch):
        """The case that must NOT change — and the common one.

        coding-gateway re-injects the tenant it resolved from the caller's own
        token on every north-south request, so `asserted == token` is ordinary
        browser traffic. Substituting here would discard the customer's own
        attribution on every single request and bill their spend to the gateway.
        """
        monkeypatch.setenv("CODING_SERVICE_TOKEN", "gw-token-abc")
        h = self._forwarded(
            {"authorization": "Bearer customer-licence", "x-tenant-id": "customer-0042"},
            vouched=False,
        )
        assert h["authorization"] == "Bearer customer-licence"

    def test_a_vouched_call_carries_the_customers_license_id(self, monkeypatch):
        """Attribution must follow the customer, not the service that vouched."""
        monkeypatch.setenv("CODING_SERVICE_TOKEN", "gw-token-abc")
        h = self._forwarded(
            {"authorization": "Bearer n8n-licence"},
            vouched=True,
            license_id="3f7a1c0e-0000-4000-8000-00000000c0de",
        )
        assert h["x-license-id"] == "3f7a1c0e-0000-4000-8000-00000000c0de"

    def test_a_vouched_call_drops_an_unvalidated_license_id(self, monkeypatch):
        """A raw header value must not ride alongside a credential that is no
        longer the caller's — it would attribute the spend to whatever they typed.

        `coding-client` only populates `request.state.license_id` from an
        X-License-Id it parsed as a UUID, so `license_id=None` here IS the
        "caller sent something unusable" case.
        """
        monkeypatch.setenv("CODING_SERVICE_TOKEN", "gw-token-abc")
        h = self._forwarded(
            {"authorization": "Bearer n8n-licence", "x-license-id": "not-a-uuid"},
            vouched=True,
            license_id=None,
        )
        assert "x-license-id" not in h

    def test_an_unvouched_call_leaves_the_license_id_alone(self, monkeypatch):
        """Unchanged behaviour off the vouched path — the caller's own credential
        still travels, so the backend can introspect it directly."""
        monkeypatch.setenv("CODING_SERVICE_TOKEN", "gw-token-abc")
        h = self._forwarded(
            {"authorization": "Bearer customer-licence", "x-license-id": "whatever"},
            vouched=False,
        )
        assert h["x-license-id"] == "whatever"

    def test_a_vouch_with_no_service_token_says_so_loudly(self, monkeypatch, caplog):
        """Misconfiguration, not an attack. The backend will refuse — which is the
        safe direction — but a bare 403 reads as an auth bug, so name the cause."""
        import logging

        monkeypatch.delenv("CODING_SERVICE_TOKEN", raising=False)
        with caplog.at_level(logging.ERROR, logger="routers.proxy"):
            h = self._forwarded({"authorization": "Bearer n8n-licence"}, vouched=True)

        assert h["authorization"] == "Bearer n8n-licence"
        assert any("CODING_SERVICE_TOKEN is unset" in r.message for r in caplog.records)


class TestTheCodingClientStubKeepsUpWithTheRealPackage:
    """`tests/conftest.py` replaces `coding_client.auth` with a hand-built module.

    Every symbol is listed by hand, so a name added to the real SDK is simply
    absent from the stub — and the failure is not a helpful "missing attribute"
    on the one test that uses it. It is an `ImportError` inside `routers.proxy`,
    swallowed by `pkgutil.resolve_name` in an unrelated autouse fixture, which
    surfaces as `module 'routers' has no attribute 'graphs'` on EVERY test in the
    file. That cost real time to diagnose when `tenant_was_vouched` was added.
    """

    @staticmethod
    def _names_the_gateway_imports() -> set[str]:
        """Every name this repo imports from `coding_client.auth`, from source.

        Derived from the corpus rather than hand-listed: a hand-listed set is
        exactly what conftest already has, and it is what went stale. The real
        contract is not "the SDK's whole `__all__`" — a symbol the gateway never
        imports cannot break it — it is "everything src/ actually asks for".
        """
        import ast

        wanted: set[str] = set()
        for path in (_REPO_ROOT / "src").rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom) and node.module == "coding_client.auth":
                    wanted.update(alias.name for alias in node.names)
        return wanted

    def test_the_stub_covers_every_symbol_the_gateway_imports(self):
        import coding_client.auth as stubbed

        wanted = self._names_the_gateway_imports()
        missing = sorted(name for name in wanted if not hasattr(stubbed, name))
        assert missing == [], (
            f"tests/conftest.py's coding_client.auth stub is missing {missing}. "
            "Add them there — preferring the REAL implementation when the symbol is "
            "a pure function, as `outbound` and `tenant_was_vouched` already are. "
            "Left unfixed this does not fail here; it fails as "
            "\"module 'routers' has no attribute 'graphs'\" across the whole suite."
        )

    def test_the_census_is_not_empty(self):
        """Non-vacuity: an empty `wanted` set makes the assertion above pass while
        checking nothing — and `rglob` over the wrong root would produce exactly
        that."""
        wanted = self._names_the_gateway_imports()
        assert "tenant_was_vouched" in wanted
        assert len(wanted) >= 2

    def test_the_stub_is_still_a_stub(self):
        """Control: the fake really is in play, so the check above is meaningful."""
        import coding_client.auth as stubbed

        assert getattr(stubbed, "__file__", None) is None


class TestBothProxyPathsAreWired:
    """Pins the WIRING, not a copy of the logic.

    The behavioural tests above call `_attach_service_credential` directly, so
    they stay green even if a proxy path stops calling it — or calls it with a
    hardcoded `vouched=False`, which would silently restore today's bug. Checked
    by parsing the module, because a substring grep would also match a call in a
    comment, a docstring or dead code.
    """

    def _calls(self):
        import ast
        from pathlib import Path

        src = (Path(__file__).resolve().parents[2] / "src/routers/proxy.py").read_text()
        tree = ast.parse(src)
        found: dict[str, list[ast.Call]] = {}
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            calls = [
                node
                for node in ast.walk(fn)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_attach_service_credential"
            ]
            if calls:
                found[fn.name] = calls
        return found

    @pytest.mark.parametrize("fn_name", ["proxy_request", "_proxy_websocket"])
    def test_the_path_attaches_a_credential(self, fn_name):
        assert fn_name in self._calls(), (
            f"{fn_name} no longer attaches this service's licence, so a proxied "
            "request reaches a TRUST_TENANT_HEADER=false backend with nothing to "
            "introspect"
        )

    @pytest.mark.parametrize("fn_name", ["proxy_request", "_proxy_websocket"])
    def test_the_vouched_flag_is_computed_not_hardcoded(self, fn_name):
        """`vouched=False` at a call site would compile, pass every behavioural
        test above, and reinstate the exact 403 this change exists to fix."""
        import ast

        for call in self._calls()[fn_name]:
            kw = {k.arg: k.value for k in call.keywords}
            for arg in ("vouched", "license_verified"):
                assert arg in kw, f"{fn_name} does not pass `{arg}=`"
                assert not isinstance(kw[arg], ast.Constant), (
                    f"{fn_name} hardcodes `{arg}=`; it must be derived from the "
                    "request state that coding-client wrote"
                )

    def test_the_census_above_can_actually_fail(self):
        """Non-vacuity: a name that is definitely NOT wired must be reported missing."""
        assert "definitely_not_a_proxy_function" not in self._calls()
