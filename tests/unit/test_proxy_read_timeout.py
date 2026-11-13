"""The quality-gate route's read budget, and the honest-failure guard around it.

WHY THIS FILE EXISTS (measured live 2026-08-28, six samples):
`GET /api/quality/{container}/gate` answers **HTTP 200** in 212-222s, while the
proxy's single shared httpx client hung up at exactly 120 015ms and returned
`504 Backend service timeout` -- discarding a successful response. The AA
conveyor's `verification_test` card blocked on that 504 at hop 5.

These tests pin the two halves that must both hold:
  * the measured blocking-analyzer routes get a budget that FITS 222s;
  * every other route is UNCHANGED at 120s, so the fix cannot be "make the
    gateway patient about everything", which would let a real hang look like
    slowness on the whole surface.

RED against the pre-fix tree: `read_timeout_for` did not exist and the client's
read timeout was the literal 120.0.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def pm():
    import routers.proxy as _pm

    return _pm


# ---------------------------------------------------------------------------
# The routes measured to exceed the default
# ---------------------------------------------------------------------------


class TestLongReadRoutes:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/quality/verify-1851506164123043156/gate",
            "/api/quality/verify-1851506164123043156/gate/",
            "/api/quality/some-container/gate",
            "/api/quality/analyze-sync",
            "/api/quality/analyze-sync/",
            # ADDED 2026-09-01: the architecture analyzer, same defect, same evidence bar.
            # ⚠️ Keep these container segments SHORT. The real name
            # (`it-flow5-<stamp>-break-dependency-cycle-...`) trips detect-secrets as a
            # "Base64 High Entropy String" and fails `ci / Lint, types, tests` on a literal that
            # is not a secret. The pattern matches `[^/]+`, so the segment's VALUE is irrelevant
            # to what this asserts -- realism here buys nothing and costs a red build.
            "/api/analysis/architecture/analyze/it-flow5-cycle",
            "/api/analysis/architecture/analyze/some-container/",
        ],
    )
    def test_blocking_analyzer_routes_get_the_analysis_budget(self, pm, path):
        # gate is a GET (evaluate_quality_gate); analyze-sync and architecture/analyze are POSTs.
        method = "GET" if path.rstrip("/").endswith("/gate") else "POST"
        assert pm.read_timeout_for(method, path) == pytest.approx(
            pm.settings.proxy_read_timeout_analysis
        )

    def test_the_budget_actually_fits_the_measured_duration(self, pm):
        """A budget that does not clear the worst measured sample fixes nothing.

        Worst of six live samples: 222 383ms. This is the assertion that would
        catch someone "tuning" the value back down to something that still 504s.
        """
        worst_measured_seconds = 222.383
        budget = pm.read_timeout_for("GET", "/api/quality/verify-abc/gate")
        assert budget > worst_measured_seconds

    def test_architecture_analyze_budget_clears_its_measured_duration(self, pm):
        """The architecture analyzer was measured at 188 837 ms answering 200.

        With the 120 s default the proxy hung up on a backend that was still working and
        returned `504 Backend service timeout`. Live consequence: the AA rung-5 drive
        (card 1854522267703182866) completed 8/10 steps, wrote a correct refactor, kept its
        tests green and opened enterprise_mcp_server#125 -- then blocked on
        `rebuild_code_graph [triggerArchitectureAnalysis]: Gateway timed out`.
        """
        measured_seconds = 188.837
        budget = pm.read_timeout_for("POST", "/api/analysis/architecture/analyze/abc")
        assert budget > measured_seconds, (
            "the architecture analyze budget no longer clears its measured duration -- "
            "this is the 2026-09-01 rung-5 blocker returning"
        )


# ---------------------------------------------------------------------------
# The negative half: everything else is untouched
# ---------------------------------------------------------------------------


class TestDefaultRoutesUnchanged:
    @pytest.mark.parametrize(
        "path",
        [
            # Cheap reads that live under the SAME /api/quality prefix. If these
            # ever return the analysis budget, the fix has been widened from
            # "two measured routes" to "a whole prefix" -- which is the mistake
            # the comment in proxy.py warns against.
            "/api/quality/verify-abc/results",
            "/api/quality/analyze",
            "/api/quality",
            # Unrelated backends.
            "/api/workspaces/abc/files",
            "/api/tests/run",
            "/api/security/scan",
            "/api/repositories",
            # Superficially similar, deliberately NOT matched.
            "/api/quality/verify-abc/gateway",
            "/api/quality/verify-abc/gate/extra",
            "/api/analysis/verify-abc/gate",
            # ⛔ cycle-check is deliberately NOT widened: it was measured SUCCEEDING inside the
            # default budget in the same drive (`capture_baseline_cycles` = ok). Adding it would
            # be a speculative widening, which is exactly what proxy.py's note forbids.
            "/api/analysis/architecture/cycle-check/abc",
            # Superficially similar to the new pattern, deliberately NOT matched.
            "/api/analysis/architecture/analyze",
            "/api/analysis/architecture/analyze/abc/extra",
            "/api/analysis/architecture/analyzer/abc",
        ],
    )
    @pytest.mark.parametrize("method", ["GET", "POST"])
    def test_other_routes_keep_the_default_budget(self, pm, method, path):
        assert pm.read_timeout_for(method, path) == pytest.approx(
            pm.settings.proxy_read_timeout_default
        )

    def test_default_is_still_120s(self, pm):
        """The default is load-bearing: raising it would change every route."""
        assert pm.settings.proxy_read_timeout_default == pytest.approx(120.0)


# ---------------------------------------------------------------------------
# Env-overridability -- tuning must not require an image rebuild
# ---------------------------------------------------------------------------


class TestEnvOverride:
    def test_both_budgets_are_settings_driven(self, pm, monkeypatch):
        """Read through `settings` at call time, not captured at import."""
        monkeypatch.setattr(pm.settings, "proxy_read_timeout_analysis", 601.0)
        monkeypatch.setattr(pm.settings, "proxy_read_timeout_default", 7.0)

        assert pm.read_timeout_for("GET", "/api/quality/verify-abc/gate") == pytest.approx(601.0)
        assert pm.read_timeout_for("POST", "/api/tests/run") == pytest.approx(7.0)

    def test_settings_accepts_env(self, monkeypatch):
        """`PROXY_READ_TIMEOUT_*` reach the fields (case-insensitive config)."""
        from config.settings import Settings

        monkeypatch.setenv("PROXY_READ_TIMEOUT_ANALYSIS", "450")
        monkeypatch.setenv("PROXY_READ_TIMEOUT_DEFAULT", "90")
        s = Settings()
        assert s.proxy_read_timeout_analysis == pytest.approx(450.0)
        assert s.proxy_read_timeout_default == pytest.approx(90.0)


# ---------------------------------------------------------------------------
# The shared client must not re-hardcode 120.0
# ---------------------------------------------------------------------------


class TestSharedClientHonoursSettings:
    async def test_client_read_timeout_comes_from_settings(self, pm, monkeypatch):
        monkeypatch.setattr(pm.settings, "proxy_read_timeout_default", 131.0)
        original = pm._client
        pm._client = None
        try:
            client = await pm.get_proxy_client()
            assert client.timeout.read == pytest.approx(131.0)
        finally:
            if pm._client is not None and not pm._client.is_closed:
                await pm._client.aclose()
            pm._client = original


# ---------------------------------------------------------------------------
# Long WRITE routes: sized to each backend's DECLARED ceiling (2026-09-22)
#
# A 504 on a write does not stop the write: the backend completes (a commit, a
# push, a provisioned workspace) while the caller is told it failed. So the
# proxy must outlast the backend's own limit, never undercut it. The ceilings
# below are copied from the backends' source; if one of them moves, the budget
# must move with it and this is the test that says so.
# ---------------------------------------------------------------------------

# (path, settings field, backend's own declared ceiling in seconds)
_WRITE_ROUTES = [
    # coding-containers container_exec.py ExecRequest.timeout le=300
    ("/api/workspaces/aa-ws/exec", "proxy_read_timeout_exec", 300),
    ("/api/workspaces/aa-ws/exec/", "proxy_read_timeout_exec", 300),
    ("/api/containers/aa-ws/exec", "proxy_read_timeout_exec", 300),  # legacy alias
    # coding-containers git_push.py GIT_PUSH_TIMEOUT default 900
    ("/api/workspaces/aa-ws/git/push", "proxy_read_timeout_push", 900),
    ("/api/containers/aa-ws/git/push", "proxy_read_timeout_push", 900),
    # coding-containers git_fetch.py GIT_FETCH_TIMEOUT default 900
    ("/api/workspaces/aa-ws/git/fetch", "proxy_read_timeout_fetch", 900),
    ("/api/workspaces/aa-ws/git/fetch/", "proxy_read_timeout_fetch", 900),
    # coding-containers secondary_repos.py: the clone runs under GIT_FETCH_TIMEOUT (900)
    ("/api/workspaces/aa-ws/repos", "proxy_read_timeout_fetch", 900),
    # coding-containers workspace_services.py: start = ready_timeout le=240 + 25 slack,
    # probe = timeout le=120 + 20 slack
    ("/api/workspaces/aa-ws/services", "proxy_read_timeout_exec", 265),
    ("/api/workspaces/aa-ws/services/svc-0123456789abcdef/probe", "proxy_read_timeout_exec", 140),
    # coding-github github_repository_management.py asyncio.wait_for(..., 600)
    ("/api/repositories/import", "proxy_read_timeout_import", 600),
    ("/api/repositories/provision", "proxy_read_timeout_import", 600),
    # timeout_seconds le=1800 (per command) -- containers and security
    ("/api/workspaces/aa-ws/package-upgrade", "proxy_read_timeout_upgrade", 1800),
    ("/api/workspaces/aa-ws/package-modify", "proxy_read_timeout_upgrade", 1800),
    ("/api/containers/aa-ws/package-upgrade", "proxy_read_timeout_upgrade", 1800),
    ("/api/dependencies/aa-ws/upgrade", "proxy_read_timeout_upgrade", 1800),
    ("/api/dependencies/aa-ws/upgrade-major", "proxy_read_timeout_upgrade", 1800),
    ("/api/dependencies/aa-ws/upgrade-cves", "proxy_read_timeout_upgrade", 1800),
    ("/api/dependencies/aa-ws/add", "proxy_read_timeout_upgrade", 1800),
    ("/api/dependencies/aa-ws/remove", "proxy_read_timeout_upgrade", 1800),
    ("/api/dependencies/aa-ws/replace", "proxy_read_timeout_upgrade", 1800),
]


class TestWriteRouteBudgets:
    @pytest.mark.parametrize(("path", "field", "_ceiling"), _WRITE_ROUTES)
    def test_route_gets_its_own_budget(self, pm, path, field, _ceiling):
        assert pm.read_timeout_for("POST", path) == pytest.approx(getattr(pm.settings, field))

    @pytest.mark.parametrize(("path", "_field", "ceiling"), _WRITE_ROUTES)
    def test_budget_outlasts_the_backend_ceiling(self, pm, path, _field, ceiling):
        """The backend's verdict -- success or ITS OWN timeout -- must arrive
        before the proxy gives up, or a completed write is reported as a 504."""
        assert pm.read_timeout_for("POST", path) > ceiling

    @pytest.mark.parametrize(
        "path",
        [
            # Cheap siblings under the same prefixes stay on the default.
            "/api/workspaces/aa-ws/git/status",
            "/api/workspaces/aa-ws/git/commit",
            "/api/workspaces/aa-ws/files",
            "/api/workspaces/aa-ws/exec/extra",
            "/api/workspaces/aa-ws/executor",
            "/api/workspaces/aa-ws/git/push/extra",
            "/api/workspaces/aa-ws/git/fetch/extra",
            # service status / stop are short execs: default budget
            "/api/workspaces/aa-ws/services/svc-0123456789abcdef",
            "/api/workspaces/aa-ws/repos/extra",
            # fetch is mounted on the canonical prefix only; the alias is a 404
            "/api/containers/aa-ws/git/fetch",
            "/api/workspaces/exec",
            "/api/dependencies/aa-ws/scan",
            "/api/dependencies/aa-ws/scan/async",
            "/api/dependencies/aa-ws/report",
            "/api/dependencies/aa-ws/upgrade/extra",
            "/api/repositories/create-pull-request",
            "/api/repositories/import/extra",
            "/api/repositories/importer",
        ],
    )
    def test_neighbours_keep_the_default(self, pm, path):
        assert pm.read_timeout_for("POST", path) == pytest.approx(
            pm.settings.proxy_read_timeout_default
        )

    def test_write_budgets_are_settings_driven(self, pm, monkeypatch):
        monkeypatch.setattr(pm.settings, "proxy_read_timeout_exec", 11.0)
        monkeypatch.setattr(pm.settings, "proxy_read_timeout_push", 12.0)
        monkeypatch.setattr(pm.settings, "proxy_read_timeout_fetch", 15.0)
        monkeypatch.setattr(pm.settings, "proxy_read_timeout_import", 13.0)
        monkeypatch.setattr(pm.settings, "proxy_read_timeout_upgrade", 14.0)
        assert pm.read_timeout_for("POST", "/api/workspaces/c/exec") == pytest.approx(11.0)
        assert pm.read_timeout_for("POST", "/api/workspaces/c/git/push") == pytest.approx(12.0)
        assert pm.read_timeout_for("POST", "/api/workspaces/c/git/fetch") == pytest.approx(15.0)
        assert pm.read_timeout_for("POST", "/api/repositories/import") == pytest.approx(13.0)
        assert pm.read_timeout_for("POST", "/api/dependencies/c/upgrade") == pytest.approx(14.0)

    def test_write_settings_accept_env(self, monkeypatch):
        from config.settings import Settings

        monkeypatch.setenv("PROXY_READ_TIMEOUT_EXEC", "301")
        monkeypatch.setenv("PROXY_READ_TIMEOUT_PUSH", "901")
        monkeypatch.setenv("PROXY_READ_TIMEOUT_FETCH", "902")
        monkeypatch.setenv("PROXY_READ_TIMEOUT_IMPORT", "601")
        monkeypatch.setenv("PROXY_READ_TIMEOUT_UPGRADE", "1801")
        s = Settings()
        assert s.proxy_read_timeout_exec == pytest.approx(301.0)
        assert s.proxy_read_timeout_push == pytest.approx(901.0)
        assert s.proxy_read_timeout_fetch == pytest.approx(902.0)
        assert s.proxy_read_timeout_import == pytest.approx(601.0)
        assert s.proxy_read_timeout_upgrade == pytest.approx(1801.0)


class TestBudgetReachesTheUpstreamCall:
    """The table is only half of it: the budget must be what httpx is handed."""

    @pytest.mark.parametrize(
        ("path", "field"),
        [
            ("/api/workspaces/aa-ws/exec", "proxy_read_timeout_exec"),
            ("/api/workspaces/aa-ws/git/push", "proxy_read_timeout_push"),
            ("/api/workspaces/aa-ws/git/fetch", "proxy_read_timeout_fetch"),
            ("/api/repositories/import", "proxy_read_timeout_import"),
            ("/api/dependencies/aa-ws/upgrade-cves", "proxy_read_timeout_upgrade"),
            ("/api/workspaces/aa-ws/git/status", "proxy_read_timeout_default"),
        ],
    )
    async def test_upstream_request_carries_the_route_budget(self, pm, client, path, field):
        from unittest.mock import AsyncMock, patch

        import httpx

        mock = AsyncMock()
        mock.request = AsyncMock(
            return_value=httpx.Response(
                200, content=b"{}", headers={"content-type": "application/json"}
            )
        )
        with patch("routers.proxy.get_proxy_client", return_value=mock):
            resp = await client.post(path, json={})
        assert resp.status_code == 200, resp.text
        sent = mock.request.call_args.kwargs["timeout"]
        assert sent.read == pytest.approx(getattr(pm.settings, field))


# ---------------------------------------------------------------------------
# The budget is keyed on METHOD + path, not path alone
#
# Every long budget above was derived for ONE operation (a POST write, or the
# analyzer's own verb). A cheap neighbour on the SAME path -- `GET {ws}/{c}/repos`
# lists, `POST` clones; `GET .../services` lists, `POST` starts one -- must stay on
# the default, both in the proxy and in the published x-request-timeout-ms.
# RED against the path-only lookup: there `GET .../repos` got the 900 s fetch budget.
# ---------------------------------------------------------------------------


class TestBudgetIsMethodAware:
    @pytest.mark.parametrize("method", ["GET", "PUT", "PATCH", "DELETE", "HEAD"])
    @pytest.mark.parametrize(("path", "_field", "_ceiling"), _WRITE_ROUTES)
    def test_non_post_on_a_write_path_gets_the_default(self, pm, method, path, _field, _ceiling):
        assert pm.read_timeout_for(method, path) == pytest.approx(
            pm.settings.proxy_read_timeout_default
        )

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("POST", "/api/quality/verify-abc/gate"),
            ("GET", "/api/quality/analyze-sync"),
            ("GET", "/api/analysis/architecture/analyze/abc"),
        ],
    )
    def test_analyzer_budget_only_for_the_analyzer_verb(self, pm, method, path):
        assert pm.read_timeout_for(method, path) == pytest.approx(
            pm.settings.proxy_read_timeout_default
        )

    def test_method_is_case_insensitive(self, pm):
        """The aggregator iterates lower-case OpenAPI keys; the proxy sees `POST`."""
        assert pm.read_timeout_for("post", "/api/workspaces/c/repos") == pytest.approx(
            pm.settings.proxy_read_timeout_fetch
        )
        assert pm.read_timeout_for("get", "/api/quality/c/gate") == pytest.approx(
            pm.settings.proxy_read_timeout_analysis
        )

    @pytest.mark.parametrize(
        ("method", "path", "field"),
        [
            # Negative control: the path-only lookup handed the two GETs the long budget.
            ("GET", "/api/workspaces/aa-ws/repos", "proxy_read_timeout_default"),
            ("GET", "/api/workspaces/aa-ws/services", "proxy_read_timeout_default"),
            ("POST", "/api/workspaces/aa-ws/repos", "proxy_read_timeout_fetch"),
            ("POST", "/api/workspaces/aa-ws/services", "proxy_read_timeout_exec"),
            ("GET", "/api/quality/aa-ws/gate", "proxy_read_timeout_analysis"),
        ],
    )
    async def test_upstream_read_budget_follows_the_method(self, pm, client, method, path, field):
        from unittest.mock import AsyncMock, patch

        import httpx

        mock = AsyncMock()
        mock.request = AsyncMock(
            return_value=httpx.Response(
                200, content=b"{}", headers={"content-type": "application/json"}
            )
        )
        with patch("routers.proxy.get_proxy_client", return_value=mock):
            if method == "POST":
                resp = await client.post(path, json={})
            else:
                resp = await client.get(path)
        assert resp.status_code == 200, resp.text
        assert mock.request.call_args.kwargs["method"] == method
        sent = mock.request.call_args.kwargs["timeout"]
        assert sent.read == pytest.approx(getattr(pm.settings, field))
