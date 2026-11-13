"""
Reverse Proxy Router

Forwards requests to backend microservices using httpx.AsyncClient.
Preserves method, headers, body, query params. Streams responses.

Also handles WebSocket upgrade requests for realtime/broadcast endpoints
via the ``websockets`` library.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from urllib.parse import unquote

import httpx
from coding_client.auth import tenant_license_verified, tenant_was_vouched
from coding_client.auth.outbound import LICENSE_ID_HEADER
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocketState

from auth.dependencies import require_tenant
from clients.outbound_identity import service_token
from config.settings import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter()

# ---------------------------------------------------------------------------
# Proxy routing table
# Maps path prefixes to backend service base URLs.
# URLs are resolved from settings with sensible defaults.
# ---------------------------------------------------------------------------

# URLs come from gitops env via Settings; production boot fails loudly if any
# are missing (see config.settings._REQUIRED_IN_PRODUCTION).
_ANALYSIS_URL = settings.analysis_url
_TESTING_URL = settings.testing_url
_AI_URL = settings.ai_url
_CONTAINERS_URL = settings.containers_url
_GITHUB_URL = settings.github_url
_SECURITY_URL = settings.security_url
_REALTIME_URL = settings.realtime_url

PROXY_ROUTES: dict[str, str] = {
    "/api/analysis": _ANALYSIS_URL,
    "/api/bug-hunter": _ANALYSIS_URL,
    "/api/quality": _ANALYSIS_URL,
    "/api/tests": _TESTING_URL,
    # py2rust A7 (coding-gateway#101): coding-testing's contract/schema/behaviour parity
    # ops (A5a schema-diff, A5b openapi-diff, A5c record/replay). The WHOLE prefix is
    # routed so later parity ops need no further gateway change. The workspace test
    # runs (A2, /api/tests/workspace/...) already ride the /api/tests prefix above.
    "/api/parity": _TESTING_URL,
    "/api/ai": _AI_URL,
    "/api/workspaces": _CONTAINERS_URL,
    "/api/containers": _CONTAINERS_URL,
    # Validation endpoint that requires container runtime (multi-line snippet)
    "/api/validation/multi-line-code-snippet": _CONTAINERS_URL,
    "/api/repositories": _GITHUB_URL,
    "/api/webhooks/pr-actions": _GITHUB_URL,
    "/api/security": _SECURITY_URL,
    "/api/dependencies": _SECURITY_URL,
    "/api/ws-health": _REALTIME_URL,
    "/api/realtime": _REALTIME_URL,
    "/api/broadcast": _REALTIME_URL,
    "/api/stats": _REALTIME_URL,
    "/api/violations": _REALTIME_URL,
    "/api/global-stats": _REALTIME_URL,
    "/api/active-connections": _REALTIME_URL,
    "/api/websocket-docs": _REALTIME_URL,
}

# Sorted by longest prefix first so more specific routes match before general ones
_SORTED_PREFIXES: list[str] = sorted(PROXY_ROUTES.keys(), key=len, reverse=True)  # type: ignore[invalid-assignment]  # ty: ignore[invalid-assignment]


# ---------------------------------------------------------------------------
# Per-route read timeouts
#
# THE DEFECT THIS EXISTS FOR (measured live 2026-08-28, six samples):
#   GET /api/quality/{container}/gate  -> coding-analysis answers **200** in
#   212 113 / 213 948 / 217 957 / 219 084 / 219 639 / 222 383 ms.
#   The shared client's global `read=120.0` hung up at 120 015 ms and this
#   module returned `504 Backend service timeout` (see the httpx.TimeoutException
#   handler at the bottom of proxy_request) -- THROWING AWAY A SUCCESSFUL
#   RESPONSE. The AA conveyor's `verification_test` card then blocked on
#   `verify_quality_gate [evaluateQualityGate]: Backend service timeout`.
#
# The route was never fast: it runs a FULL analysis (`analyzer.analyze`), whose
# own budgets are 300-1800s per tool and 1800s per batch. It surfaced only when
# coding-analysis#91 threaded `tenant_id` into the container exec -- before that
# every tool 404'd in ~25ms, so the "analysis" cost nothing and fit trivially.
#
# SCOPE IS DELIBERATELY EVIDENCE-BOUND. Only routes MEASURED to run the blocking
# analyzer are listed; everything else keeps the 120s default, so the blast
# radius of a genuinely-hung backend holding a connection longer is confined to
# these two low-volume paths. `analyze-sync` is included because it is the SAME
# blocking analyzer, measured at 43 979 ms -- one larger repo away from 120s.
#
# Do NOT widen this by pattern-matching a whole prefix (`/api/quality`): most of
# that surface is cheap reads of cached results, and giving them 300s would let
# a real hang look like slowness for five minutes.
_LONG_READ_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # GET /api/quality/{container_name}/gate  (evaluate_quality_gate)
    ("GET", re.compile(r"^/api/quality/[^/]+/gate/?$")),
    # POST /api/quality/analyze-sync  (run_quality_analysis_sync)
    ("POST", re.compile(r"^/api/quality/analyze-sync/?$")),
    # POST /api/analysis/architecture/analyze/{container_name}  (trigger_architecture_analysis)
    #
    # ADDED 2026-09-01, same defect, same evidence bar. This route runs the FULL architecture
    # analyzer and was MEASURED at 188 837 ms answering 200 (coding-analysis log, 04:40:30) --
    # comfortably past the 120 s default, so the proxy hung up and returned
    # `504 Backend service timeout` while the backend was still working.
    #
    # Live consequence: the AA rung-5 `architecture_refactoring` drive (card
    # 1854522267703182866) completed 8/10 typed steps, wrote a correct refactor, kept its tests
    # green and opened enterprise_mcp_server#125 -- then blocked on
    #     rebuild_code_graph [triggerArchitectureAnalysis]: Gateway timed out
    # with `assert_no_new_cycle` cascading behind it, because the graph it needed was never
    # rebuilt. The refactor was RIGHT; only the verification could not run.
    #
    # `cycle-check` is deliberately NOT added: it was measured SUCCEEDING inside the default
    # budget in the same drive (`capture_baseline_cycles` = ok), so there is no evidence for it
    # and a speculative widening is exactly what the note above forbids.
    ("POST", re.compile(r"^/api/analysis/architecture/analyze/[^/]+/?$")),
)


# ---------------------------------------------------------------------------
# WRITE routes whose backend DECLARES a limit above the default (added 2026-09-22)
#
# A different evidence bar from the analyzer list above, and a stronger one: the
# bound is not a measured duration but the backend's OWN declared ceiling, read
# in its source. When the proxy hangs up first on a WRITE, the 504 is worse than
# a lost read: the backend keeps going and the write LANDS (a commit, a push, a
# PR, a provisioned workspace) while the caller is told it failed. The retry then
# meets the finished write -- `git push` answers 400 "No commits to push" -- so
# a ghost success is reported as a failure. Each budget is the backend ceiling
# plus a margin for the handler's own pre/post work, so the backend's verdict
# (success, or ITS timeout error) always arrives before ours:
#
#   exec    coding-containers ExecRequest.timeout le=300           -> 330
#   push    coding-containers GIT_PUSH_TIMEOUT default 900 (+guard) -> 960
#   fetch   coding-containers GIT_FETCH_TIMEOUT default 900 (+guard) -> 960
#           (also POST {ws}/{c}/repos: the secondary-repo clone, same env var)
#   service coding-containers start ready_timeout le=240 (+25) / probe le=120 (+20) -> exec 330
#   import  coding-github asyncio.wait_for(provision, 600)          -> 660
#   upgrade timeout_seconds le=1800 PER COMMAND (containers + security) -> 1860
#
# ⚠️ `upgrade` is the one this CANNOT fully close. Its limit is per command and
# a run executes several, and neither backend offers an async (202 + job id)
# variant, so no single HTTP budget bounds it; 1860 covers one maximal command.
# The durable fix is an async job endpoint (as coding-security's scan/async),
# tracked as a follow-up. Commit (`git/commit`) stays on the default: its
# formatter step is several short execs with no declared total.
#
# The legacy `/api/containers/...` alias serves the same handlers as
# `/api/workspaces/...` (coding-containers main.py), so both prefixes match.
# ⛔ n8n's axios default is 300 s: a budget above that is only reachable when the
# node sets a matching per-op timeout (n8n-nodes-coding index.ts header rule).
_WS = r"^/api/(?:workspaces|containers)/[^/]+"
_WRITE_READ_BUDGETS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    # POST {ws}/{c}/exec  (exec_in_workspace)
    ("POST", re.compile(_WS + r"/exec/?$"), "proxy_read_timeout_exec"),
    # POST {ws}/{c}/git/push  (push_workspace_to_fork)
    ("POST", re.compile(_WS + r"/git/push/?$"), "proxy_read_timeout_push"),
    # POST /api/workspaces/{c}/git/fetch  (fetch_workspace_ref). Canonical prefix
    # ONLY: coding-containers git_fetch.py is not mounted on the legacy
    # /api/containers alias, so that path is a 404 and keeps the default budget.
    # Its own budget, not push's: the two backend ceilings are separate env vars.
    ("POST", re.compile(r"^/api/workspaces/[^/]+/git/fetch/?$"), "proxy_read_timeout_fetch"),
    # POST /api/workspaces/{c}/repos  (add_workspace_repo): a depth-1 clone under the
    # same GIT_FETCH_TIMEOUT (default 900) as git/fetch. Canonical prefix only.
    ("POST", re.compile(r"^/api/workspaces/[^/]+/repos/?$"), "proxy_read_timeout_fetch"),
    # POST /api/workspaces/{c}/services  (start_workspace_service): ready_timeout le=240
    # + 25 s stop/startup slack = 265; POST .../services/{id}/probe: timeout le=120 + 20
    # = 140. Both under the exec budget. Canonical prefix only.
    ("POST", re.compile(r"^/api/workspaces/[^/]+/services/?$"), "proxy_read_timeout_exec"),
    (
        "POST",
        re.compile(r"^/api/workspaces/[^/]+/services/[^/]+/probe/?$"),
        "proxy_read_timeout_exec",
    ),
    # POST /api/repositories/import  (import_repository_as_workspace) + hidden /provision alias
    (
        "POST",
        re.compile(r"^/api/repositories/(?:import|provision)/?$"),
        "proxy_read_timeout_import",
    ),
    # POST {ws}/{c}/package-upgrade | package-modify  (coding-containers)
    ("POST", re.compile(_WS + r"/package-(?:upgrade|modify)/?$"), "proxy_read_timeout_upgrade"),
    # POST /api/dependencies/{c}/upgrade|upgrade-major|upgrade-cves|add|remove|replace
    # (coding-security; each drives package-upgrade/-modify and may commit + open a PR)
    (
        "POST",
        re.compile(
            r"^/api/dependencies/[^/]+/(?:upgrade|upgrade-major|upgrade-cves|add|remove|replace)/?$"
        ),
        "proxy_read_timeout_upgrade",
    ),
    # py2rust A2/A5b (coding-testing; coding-gateway#101). Not writes to the repo, but each
    # drives SEVERAL workspace execs whose ceilings coding-testing declares, so the same
    # rule applies: a 504 here discards a computed verdict the backend still delivers.
    #   POST /api/tests/workspace/{c}/runs  (run_workspace_tests, sync):
    #     ls 60 + ws-pg 120 + write config 60 + suite timeout le=300 (sync cap) + read
    #     JUnit 60 = 600                                                       -> 630
    #     An `async: true` run answers 202 at once; GET .../runs/{id} is a cheap poll and
    #     stays on the default.
    #   POST /api/parity/{c}/openapi-diff  (parity_workspace_openapi_diff): one sync
    #     document command le=300 + parity_toml read 60 = 360, inside the same 630.
    #     ⚠️ NOT fully bounded: it obtains N sources + 1 target SEQUENTIALLY and a
    #     command with timeout > 300 is polled as an async exec job (le=3600), so no
    #     single HTTP budget covers every request -- same class as `upgrade` above. The
    #     durable fix is an async (202 + id) variant upstream.
    #   POST /api/parity/openapi-diff (inline documents) is pure compute: default budget.
    (
        "POST",
        re.compile(r"^/api/tests/workspace/[^/]+/runs/?$"),
        "proxy_read_timeout_workspace_run",
    ),
    (
        "POST",
        re.compile(r"^/api/parity/[^/]+/openapi-diff/?$"),
        "proxy_read_timeout_workspace_run",
    ),
    # py2rust A5a/A5c (coding-testing). Same rule: budgets from the declared ceilings.
    #   POST /api/parity/{c}/schema-diff  (parity_workspace_schema_diff, sync only):
    #     ws-pg start + createdb x2 + migrate x2 + introspect x2 = 7 execs, each bounded
    #     by the request's `timeout` (le=300) -> 2100                           -> 2130
    #     POST /api/parity/schema-diff (two DSNs, read-only) stays on the default.
    #   POST /api/parity/{c}/record|replay|compare (parity_behaviour_*):
    #     default `async: true` = 3 file writes (60 each) then a 202 -- fits easily.
    #     Sync (`async: false`, timeout le=300): writes 3x60 + driver 300 + reads of
    #     spec/result/recording 3x60 = 660                                     -> 690
    #     ⚠️ A write is chunked at 32 KB, so very large INLINE `cases` add 60 s per
    #     extra chunk; such requests should use `cases_file` or async.
    #     GET .../runs/{id} is a poll (job status + <=3 small reads): default budget.
    (
        "POST",
        re.compile(r"^/api/parity/[^/]+/schema-diff/?$"),
        "proxy_read_timeout_workspace_schema_diff",
    ),
    (
        "POST",
        re.compile(r"^/api/parity/[^/]+/(record|replay|compare)/?$"),
        "proxy_read_timeout_behaviour_parity",
    ),
)


def read_timeout_for(method: str, path: str) -> float:
    """Return the read-timeout budget (seconds) this request (method + path) is allowed.

    Everything not explicitly listed gets the default, so adding a route here is
    a deliberate act with a measurement (analyzers) or a declared backend
    ceiling (writes) behind it. The METHOD is part of the key: each budget was
    derived for one operation, and a cheap neighbour on the same path (e.g.
    ``GET {ws}/{c}/repos`` lists, ``POST`` clones) must not inherit it.
    """
    method = method.upper()
    for budget_method, pattern in _LONG_READ_PATTERNS:
        if method == budget_method and pattern.match(path):
            return float(settings.proxy_read_timeout_analysis)
    for budget_method, pattern, setting_name in _WRITE_READ_BUDGETS:
        if method == budget_method and pattern.match(path):
            return float(getattr(settings, setting_name))
    return float(settings.proxy_read_timeout_default)


def request_timeout_ms_for(method: str, path: str) -> int:
    """Client-side timeout (ms) for the operation ``method path``: the gateway's read
    budget plus ``client_request_timeout_margin``, so the gateway's verdict (the result,
    or its own 504) always arrives before the client gives up. Published per operation
    as ``x-request-timeout-ms`` in the aggregated /openapi.json; one source of truth with
    :func:`read_timeout_for`.
    """
    return int((read_timeout_for(method, path) + settings.client_request_timeout_margin) * 1000)


# ---------------------------------------------------------------------------
# Shared httpx client (created lazily, closed on shutdown via lifespan)
# ---------------------------------------------------------------------------

_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()


async def get_proxy_client() -> httpx.AsyncClient:
    """Return the shared httpx.AsyncClient, creating it if needed."""
    global _client
    async with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=5.0,
                    read=float(settings.proxy_read_timeout_default),
                    write=30.0,
                    pool=10.0,
                ),
                follow_redirects=True,
                max_redirects=5,
                limits=httpx.Limits(max_connections=200, max_keepalive_connections=40),
            )
    return _client


async def close_proxy_client() -> None:
    """Close the shared httpx client (called during shutdown)."""
    global _client
    async with _client_lock:
        if _client is not None and not _client.is_closed:
            await _client.aclose()
            _client = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Headers that must NOT be forwarded to the backend
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",  # httpx recalculates this
        "content-encoding",  # httpx already decompresses; let GZipMiddleware re-encode
    }
)

# Identity headers the gateway derives from the validated JWT and injects itself.
# A client must never be able to supply these — otherwise an authenticated caller
# whose token has, e.g., empty roles could spoof `X-User-Roles: admin` and have the
# gateway forward it verbatim to backends that trust it (privilege escalation).
# They are stripped from every inbound request before the validated values are
# re-injected; backends should never receive a client-supplied copy.
_CLIENT_SPOOFABLE_IDENTITY = frozenset(
    {
        "x-user-id",
        "x-user-roles",
        "x-user-name",
        "x-user-email",
        "x-tenant-id",
        "x-organization-id",
    }
)


def _resolve_backend(path: str) -> tuple[str, str] | None:
    """Match *path* against PROXY_ROUTES and return (backend_url, full_url)."""
    for prefix in _SORTED_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            backend_base = PROXY_ROUTES[prefix]
            # Forward the full path to the backend (it has the same route structure)
            target_url = f"{backend_base}{path}"
            return backend_base, target_url
    return None


# ---------------------------------------------------------------------------
# Path traversal refusal
#
# THE DEFECT THIS EXISTS FOR (auditor, measured 2026-09-22): this proxy used to
# forward ``request.url.path`` -- the DECODED path -- and httpx 0.28.1 resolves
# literal dot-segments when it parses a URL string. So
#     GET /api/repositories/files/..%2Fapprovals
# arrived here as ``/api/repositories/files/../approvals``, matched the
# ``/api/repositories`` prefix, and httpx sent ``/api/repositories/approvals``
# upstream: the caller escaped the path it was routed on. Encoding path params
# on the n8n side (n8n_module_scripts#85) cannot stop that, because it is this
# hop that undid the encoding.
#
# Two independent fixes, either of which closes the measured case:
#   1. refuse any path that carries a dot-segment, an encoded slash/backslash,
#      a backslash, or a NUL, at ANY decoding depth (double encoding included);
#   2. forward the RAW (still-encoded) path, so this hop never decodes anything.
# Plus a third belt: after building the upstream URL, its normalised path must
# still sit under the prefix the request was routed on.
#
# Rejecting ``%2F`` is safe for the fleet: no coding-* upstream declares a
# ``{param:path}`` converter (grepped 2026-09-22), so an encoded slash inside a
# single-segment parameter can never be a legitimate request.
# ---------------------------------------------------------------------------

_FORBIDDEN_ESCAPES = re.compile(r"%(?:2f|5c|00)", re.IGNORECASE)
# Enough to see through double/triple encoding; anything still changing after
# this many passes is refused rather than trusted.
_MAX_DECODE_PASSES = 4


def _path_rejection_reason(path: str) -> str | None:
    """Return why *path* must not be proxied, or None if it is safe.

    *path* may be raw (percent-encoded) or already decoded; it is decoded
    repeatedly and every intermediate form is checked, so ``%252e%252e`` is
    caught on the second pass exactly as ``..`` is on the first.
    """
    candidate = path
    for _ in range(_MAX_DECODE_PASSES):
        if "\x00" in candidate:
            return "NUL byte in path"
        if "\\" in candidate:
            return "backslash in path"
        if _FORBIDDEN_ESCAPES.search(candidate):
            return "encoded slash, backslash or NUL in path"
        if any(segment in (".", "..") for segment in candidate.split("/")):
            return "dot-segment in path"
        decoded = unquote(candidate)
        if decoded == candidate:
            return None
        candidate = decoded
    return "path is percent-encoded too many times"


def _raw_request_path(request: HTTPConnection) -> str:
    """The request path exactly as the client sent it (still percent-encoded).

    ASGI servers put it in ``scope["raw_path"]`` (bytes, no query string). Fall
    back to the decoded path only when a server does not provide it.
    """
    raw = request.scope.get("raw_path")
    if isinstance(raw, (bytes, bytearray)) and raw:
        return bytes(raw).decode("latin-1")
    return request.url.path


def _under_prefix(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _filter_headers(headers: dict[str, str]) -> dict[str, str]:
    """Strip hop-by-hop headers before forwarding."""
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}


def _strip_client_identity(headers: dict[str, str]) -> None:
    """Drop any client-supplied identity headers in place.

    Applied on BOTH proxy paths — HTTP (`proxy_request`) and WebSocket
    (`_proxy_websocket`). Each re-injects ``x-tenant-id`` from the value the
    gateway itself resolved via ``require_tenant`` (and the ``x-user-*`` headers
    from its own auth context) immediately after calling this, so a client can
    never have a raw identity header it supplied forwarded to a backend.

    This is defence-in-depth behind Istio ext_authz (the primary boundary, which
    sanitises inbound identity headers at the mesh edge); it closes in-cluster
    edge cases where ``AuthMiddleware`` does not mirror a header into
    ``state.user`` (e.g. an ``x-user-roles``-only request on a trusted network).
    """
    for header in _CLIENT_SPOOFABLE_IDENTITY:
        headers.pop(header, None)


def _attach_service_credential(
    headers: dict[str, str],
    *,
    license_verified: bool,
    vouched: bool,
    license_id: str | None = None,
) -> None:
    """Put THIS service's licence on an outbound call, in place.

    Extracted as a named function rather than inlined so it can be tested
    against the real code. An earlier version of the test re-implemented this
    logic instead, and a mutation that clobbered a client-supplied credential
    survived — the test could not see a change it was supposedly guarding.

    Two questions, in order, and the FIRST one is the security boundary.

    **1. Did we verify this tenant ourselves?** (``license_verified``)

    Only a licence introspection counts. A tenant that arrived on a trusted
    ``X-Tenant-Id`` header, or via the trusted-network bypass, is a string the
    caller typed. Lending our licence to it means signing for a claim we never
    checked — and because ``svc-coding-gateway`` is on the backends'
    ``CODING_ONBEHALF_SUBJECTS``, the callee's allowlist would then be satisfied
    by the wrong party.

    That was live. Driven on 2026-08-14 against the deployed pods::

        ws://…/api/quality/ws/probe-forged-tenant
        x-tenant-id: 0195f48b-…   (forged, NO credential)  -> UPGRADE ACCEPTED

        coding-analysis:
        Vouched on-behalf-of: subject '019ffa19-…' asserted tenant '0195f48b-…'

    Any pod could read any tenant over the WebSocket surface, and that was the
    fleet's first ever ``Vouched on-behalf-of`` line in production. So an
    unverified tenant gets **nothing** from us: the headers go out as they came
    in, and the backend refuses, which is exactly right.

    **2. Did we have to vouch?** (``vouched``)

    *No* — the credential we are forwarding already names the tenant we are
    injecting, because we resolved one from the other. Keep what the caller sent;
    overwriting discards the customer's own attribution for no gain, and this is
    the COMMON case (every north-south request the gateway re-injects a tenant
    into).

    *Yes* — it does not. The caller proved it may act for a tenant other than the
    one its own licence names, so its credential says "service A" while the
    header says "customer B". Forwarding that pair forwards a disagreement, and
    the next hop refuses disagreements — that is the point of the vouch. So we
    substitute OUR licence, which the backends allowlist, and carry the
    customer's licence id separately for attribution. The same shape the caller
    used on us, one hop down.

    Without step 2, n8n's AA dispatch 403s at coding-containers: an ``n8n-dev``
    licence arriving alongside an injected ``platform-cp-3``.
    """
    if not license_verified:
        # We did not establish this tenant; we do not underwrite it. Deliberately
        # NOT an error log: this is the ordinary shape of every request while
        # TRUST_TENANT_HEADER is still on, and it is the safe one.
        return

    if not vouched:
        if any(k.lower() == "authorization" for k in headers):
            return
    elif license_id:
        # Attribution for a vouched call rides the CUSTOMER's licence id, never
        # the vouching service's own — otherwise every customer's spend bills to
        # this gateway. Re-injected from the value `coding-client` validated as a
        # UUID, not passed through from the wire.
        headers[LICENSE_ID_HEADER.lower()] = license_id
    else:
        # Vouched but the caller forwarded no usable licence id. Drop any raw one
        # rather than let an unvalidated value ride alongside a credential that
        # is no longer the caller's — it would attribute the customer's spend to
        # whatever string they typed.
        headers.pop(LICENSE_ID_HEADER.lower(), None)

    token = service_token()
    if token:
        headers["authorization"] = f"Bearer {token}"
    elif vouched:
        # Misconfiguration, not an attack. Leaving the caller's credential in
        # place next to a tenant it does not name means the backend refuses —
        # which is the safe direction, but it will look like an auth bug, so say
        # so plainly here rather than let it be diagnosed from a bare 403.
        logger.error(
            "Vouched request but CODING_SERVICE_TOKEN is unset: forwarding the caller's "
            "credential alongside a tenant it does not name. The backend will refuse."
        )


def _proxy_error(request: Request, status_code: int, message: str) -> Response:
    """Build a KAG-standard error response for proxy failures."""
    request_id = getattr(request.state, "request_id", None) or str(uuid.uuid4())
    return Response(
        content=json.dumps(
            {
                "error": {
                    "code": f"HTTP_{status_code}",
                    "message": message,
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "request_id": request_id,
                }
            }
        ),
        status_code=status_code,
        media_type="application/json",
    )


# ---------------------------------------------------------------------------
# WebSocket proxy helpers
# ---------------------------------------------------------------------------

_WS_REALTIME_URL = _REALTIME_URL.replace("http://", "ws://").replace("https://", "wss://")
_WS_ANALYSIS_URL = _ANALYSIS_URL.replace("http://", "ws://").replace("https://", "wss://")


async def _resolve_ws_tenant(client_ws: WebSocket) -> str | None:
    """Resolve the authoritative tenant for a WebSocket upgrade, or None.

    The WS path has no ``request.state.user``: every middleware on this app
    (``AuthMiddleware``, ``RequestLoggingMiddleware``, ``CORSMiddleware``) is a
    ``BaseHTTPMiddleware``, which only runs for ``scope["type"] == "http"`` and
    passes a websocket scope straight through. So nothing populates the auth
    context on an upgrade, and the gateway must run the resolver itself.

    ``require_tenant`` is that resolver — the same single source of truth the
    HTTP path uses via ``Depends``. It duck-types onto a ``WebSocket``: both it
    and ``Request`` are Starlette ``HTTPConnection`` subclasses exposing the
    ``.state`` / ``.headers`` / ``.client`` the resolver reads. Running it here
    gives the WS path identical trust semantics to HTTP (state -> Istio-forwarded
    header -> license introspection -> user state -> trusted network), including
    the license-token introspection that the WS path previously never performed.

    Returns None when no tenant context can be established, so the caller can
    fail closed instead of proxying whatever the client sent.
    """
    try:
        return await require_tenant(client_ws)
    except HTTPException:
        # 401 from require_tenant: no tenant context at all. Fail closed.
        return None
    except Exception:
        # Introspection blew up / malformed connection. Fail closed rather than
        # degrading to the client-supplied header.
        logger.warning("WebSocket tenant resolution failed", exc_info=True)
        return None


async def _proxy_websocket(client_ws: WebSocket, backend_url: str) -> None:
    """Bi-directional WebSocket proxy between the client and a backend service.

    Uses the ``websockets`` library for the backend connection and FastAPI's
    native WebSocket support on the client side.

    Forwards the client's hop-by-hop-filtered headers to the backend on the
    upgrade handshake, plus the same `x-user-id` / `x-tenant-id` /
    `x-user-name` / `x-user-email` identity headers the HTTP catch-all path
    injects. Without those, backends that enforce auth on the upgrade (e.g.
    coding-analysis's /api/quality/ws/{container_name} requires `X-Tenant-Id`)
    reject the connection with HTTP 403 and the client sees an immediate close.

    SECURITY: client-supplied identity headers are stripped exactly as on the
    HTTP path, and `x-tenant-id` is re-injected ONLY from the value the gateway
    resolved itself (`_resolve_ws_tenant`). Previously the WS path forwarded the
    raw upgrade headers verbatim, so a caller reaching this route without
    traversing Istio's sanitisation could set `x-tenant-id` (cross-tenant BOLA on
    the websocket surface) or `x-user-roles: admin` (privilege escalation at any
    backend that trusts it). When no tenant resolves the upgrade is REJECTED
    rather than proxied with an unverified header.
    """
    import websockets

    # Resolve BEFORE accepting: a rejected upgrade must never be accepted and
    # then closed, and the resolver must read the ORIGINAL headers (below we
    # strip them off the forwarded copy).
    tenant_id = await _resolve_ws_tenant(client_ws)
    if not tenant_id:
        logger.warning(
            "Rejecting WebSocket upgrade to %s: no gateway-resolved tenant context",
            backend_url,
        )
        # close() before accept() denies the handshake (Starlette answers HTTP 403).
        await client_ws.close(code=1008, reason="Tenant context required")
        return

    await client_ws.accept()

    # Build the additional-headers list for the backend handshake.
    # FastAPI exposes the original upgrade request headers on `client_ws.headers`
    # as a case-insensitive `Headers` mapping.
    forwarded_headers = _filter_headers(dict(client_ws.headers))

    # Never forward client-supplied identity headers; re-inject resolved values below.
    _strip_client_identity(forwarded_headers)

    # The gateway-resolved tenant is authoritative. As on the HTTP path, it is NOT
    # re-derived from `state.user` below: the synthetic trusted-network user carries
    # tenant_id=DEFAULT_TENANT_ID and would clobber a correctly introspected tenant.
    forwarded_headers["x-tenant-id"] = tenant_id

    user = getattr(client_ws.state, "user", None)
    if user is not None:
        if getattr(user, "sub", None):
            forwarded_headers["x-user-id"] = user.sub
        if getattr(user, "preferred_username", None):
            forwarded_headers["x-user-name"] = user.preferred_username
        if getattr(user, "email", None):
            forwarded_headers["x-user-email"] = user.email
    # Present THIS service's licence on the upgrade, so the backend can verify who
    # is asserting the tenant we just injected above.
    #
    # This is what makes closing the WebSocket forgery hole possible at all.
    # AuthMiddleware is a BaseHTTPMiddleware and Starlette never runs it on a
    # websocket scope, so the backends have to resolve identity in-handler — and
    # they cannot demand a credential from a browser, because the WebSocket API
    # has no way to set an Authorization header on a handshake. The gateway is a
    # server, so it can. It presents its own licence while the CUSTOMER tenant
    # continues to ride the x-tenant-id injected above; coding-analysis and
    # coding-realtime honour that assertion only because svc-coding-gateway is in
    # their CODING_ONBEHALF_SUBJECTS.
    #
    # ...and substituted for the caller's own whenever the gateway had to VOUCH
    # to resolve this tenant: then the caller's credential names a DIFFERENT
    # tenant than the one injected above, and forwarding that pair forwards a
    # disagreement the backend exists to refuse. See _attach_service_credential.
    _attach_service_credential(
        forwarded_headers,
        license_verified=tenant_license_verified(client_ws),
        vouched=tenant_was_vouched(client_ws),
        license_id=getattr(client_ws.state, "license_id", None),
    )

    # `websockets.connect` strips the websocket-specific request headers
    # itself (Sec-WebSocket-Key, Upgrade, etc.) when given as additional
    # headers, so we drop them here to silence the duplicate warning.
    for ws_header in (
        "sec-websocket-key",
        "sec-websocket-version",
        "sec-websocket-extensions",
        "sec-websocket-protocol",
    ):
        forwarded_headers.pop(ws_header, None)

    try:
        async with websockets.connect(
            backend_url, additional_headers=forwarded_headers
        ) as backend_ws:

            async def client_to_backend() -> None:
                """Forward messages from the client to the backend."""
                try:
                    while True:
                        msg = await client_ws.receive()
                        if msg["type"] == "websocket.receive":
                            if "text" in msg:
                                await backend_ws.send(msg["text"])
                            elif "bytes" in msg:
                                await backend_ws.send(msg["bytes"])
                        elif msg["type"] == "websocket.disconnect":
                            break
                except WebSocketDisconnect:
                    await backend_ws.close()

            async def backend_to_client() -> None:
                """Forward messages from the backend to the client."""
                try:
                    async for message in backend_ws:
                        if isinstance(message, bytes):
                            await client_ws.send_bytes(message)
                        else:
                            await client_ws.send_text(str(message))
                except Exception:
                    logger.debug("WebSocket backend-to-client forwarding ended", exc_info=True)

            # Run both directions concurrently; when one side closes, cancel the other.
            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(client_to_backend()),
                    asyncio.create_task(backend_to_client()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()

            # Propagate the backend's close code + reason to the client. The
            # websockets library populates `close_code` / `close_reason` after
            # the connection closes; values of None mean the backend never
            # sent a Close frame (raw socket drop), in which case we let the
            # client see a generic close. Without this, downstream consumers
            # (e.g. fr-backend's /quality proxy) cannot translate upstream
            # policy violations such as coding-analysis's 1008 "Container
            # validation failed: …" into a typed NO_CONTAINER frame for the
            # frontend.
            if client_ws.client_state == WebSocketState.CONNECTED:
                code = backend_ws.close_code
                reason = backend_ws.close_reason or ""
                if code is not None:
                    await client_ws.close(code=code, reason=reason)
                else:
                    await client_ws.close()
    except Exception as exc:
        logger.error("WebSocket proxy error for %s: %s", backend_url, exc)
        if client_ws.client_state == WebSocketState.CONNECTED:
            await client_ws.close(code=1011, reason="Backend connection failed")


# ---------------------------------------------------------------------------
# WebSocket proxy endpoints (must be registered BEFORE the catch-all)
# ---------------------------------------------------------------------------


async def _refuse_ws_traversal(websocket: WebSocket) -> bool:
    """Deny the upgrade (before accept) if the path could traverse. True = refused.

    The WS endpoints below interpolate DECODED path params into the upstream URL,
    so they get the same refusal as the HTTP catch-all.
    """
    raw_path = _raw_request_path(websocket)
    reason = _path_rejection_reason(raw_path) or _path_rejection_reason(websocket.url.path)
    if reason is None:
        return False
    logger.warning("Refusing WebSocket upgrade %r: %s", raw_path, reason)
    await websocket.close(code=1008, reason=f"Invalid request path: {reason}")
    return True


@router.websocket("/api/realtime/ws/{path:path}")
async def ws_realtime_proxy(websocket: WebSocket, path: str) -> None:
    """Proxy WebSocket connections for realtime endpoints to coding-realtime."""
    if await _refuse_ws_traversal(websocket):
        return
    backend_url = f"{_WS_REALTIME_URL}/api/realtime/ws/{path}"
    if websocket.url.query:
        backend_url = f"{backend_url}?{websocket.url.query}"
    await _proxy_websocket(websocket, backend_url)


@router.websocket("/api/broadcast/subscribe")
async def ws_broadcast_subscribe_proxy(websocket: WebSocket) -> None:
    """Proxy WebSocket connections for broadcast subscriptions to coding-realtime."""
    if await _refuse_ws_traversal(websocket):
        return
    backend_url = f"{_WS_REALTIME_URL}/api/broadcast/subscribe"
    if websocket.url.query:
        backend_url = f"{backend_url}?{websocket.url.query}"
    await _proxy_websocket(websocket, backend_url)


@router.websocket("/api/quality/ws/{container_name}")
async def ws_quality_proxy(websocket: WebSocket, container_name: str) -> None:
    """Proxy WebSocket connections for quality analysis to coding-analysis."""
    if await _refuse_ws_traversal(websocket):
        return
    backend_url = f"{_WS_ANALYSIS_URL}/api/quality/ws/{container_name}"
    if websocket.url.query:
        backend_url = f"{backend_url}?{websocket.url.query}"
    await _proxy_websocket(websocket, backend_url)


# ---------------------------------------------------------------------------
# Catch-all proxy endpoint
# ---------------------------------------------------------------------------


@router.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    summary="Reverse Proxy",
    description="Forward requests to backend microservices.",
    include_in_schema=False,
    operation_id="proxy_request",
)
async def proxy_request(
    request: Request, path: str, tenant_id: str = Depends(require_tenant)
) -> Response:
    """Forward the incoming request to the appropriate backend service.

    The full original path is preserved so that backend services see the
    same URL structure that the client sent.
    """
    full_path = request.url.path
    raw_path = _raw_request_path(request)

    # Refuse traversal BEFORE routing and before any upstream call. Both forms
    # are checked: the raw one sees encodings the decoded one has lost, and the
    # decoded one covers a server that supplies no raw_path.
    reason = _path_rejection_reason(raw_path) or _path_rejection_reason(full_path)
    if reason is not None:
        logger.warning("Refusing proxied request %r: %s", raw_path, reason)
        return _proxy_error(request, 400, f"Invalid request path: {reason}")

    resolved = _resolve_backend(full_path)

    if resolved is None:
        return _proxy_error(request, 404, f"No backend route matched for path: {full_path}")

    backend_base, _ = resolved
    prefix = next(p for p in _SORTED_PREFIXES if _under_prefix(full_path, p))

    # Forward the RAW path: this hop must never decode what the client encoded.
    target_url = f"{backend_base}{raw_path}"

    # Defence in depth: whatever httpx makes of the URL (it normalises
    # dot-segments), the path it will actually send must still sit under the
    # prefix this request was routed on.
    if not _under_prefix(httpx.URL(target_url).path, prefix):
        logger.warning("Refusing proxied request %r: escapes route %s", raw_path, prefix)
        return _proxy_error(request, 400, "Invalid request path: escapes its route")

    # Append query string if present
    if request.url.query:
        target_url = f"{target_url}?{request.url.query}"

    # Forward headers (stripping hop-by-hop)
    forwarded_headers = _filter_headers(dict(request.headers))

    # Never forward client-supplied identity headers; re-inject validated values below.
    _strip_client_identity(forwarded_headers)

    # Forward the gateway-resolved tenant (from require_tenant, the single source of
    # truth) on every proxied request, so downstream services never fall through to
    # their trusted-network bypass / DEFAULT_TENANT_ID. require_tenant already integrates
    # license introspection (priority 2) AND request.state.user (priority 3), so the
    # resolved tenant_id is authoritative. The request.state.user block below MUST NOT
    # re-derive x-tenant-id from user.tenant_id: the base AuthMiddleware injects a
    # synthetic trusted-network user with tenant_id=DEFAULT_TENANT_ID for in-cluster
    # token-only requests, and that DEFAULT would otherwise clobber the correctly
    # introspected tenant -> 404 on the caller's tenant-scoped resources.
    if tenant_id:
        forwarded_headers["x-tenant-id"] = tenant_id

    # Inject auth context from middleware for backend services (identity headers only;
    # the tenant is owned by the resolved tenant_id above, never re-derived here).
    if hasattr(request.state, "user") and request.state.user:
        user = request.state.user
        forwarded_headers["x-user-id"] = user.sub or ""
        if user.preferred_username:
            forwarded_headers["x-user-name"] = user.preferred_username
        if user.email:
            forwarded_headers["x-user-email"] = user.email
        if user.organization_id:
            forwarded_headers["x-organization-id"] = user.organization_id
        roles = user.realm_access.get("roles", []) if user.realm_access else []
        if roles:
            forwarded_headers["x-user-roles"] = ",".join(roles)
    elif hasattr(request.state, "tenant_id") and request.state.tenant_id:
        forwarded_headers["x-tenant-id"] = request.state.tenant_id

    # Prove to the backend WHO is asserting the tenant injected above.
    #
    # This path used to forward the caller's `Authorization` and nothing else,
    # which was sound only while the backends still trusted a bare header. They
    # no longer do — all eight are `TRUST_TENANT_HEADER=false` — so a proxied
    # request that arrives WITHOUT a credential (fr's coding integration sends
    # `x-tenant-id` only, by design: it must never forward the end user's JWT)
    # reaches the backend with nothing to introspect and 401s there.
    #
    # And when the gateway had to VOUCH, keeping the caller's credential is worse
    # than useless: it names a different tenant than the one just injected, and
    # the backend refuses the pair. That is n8n's AA dispatch 403ing today.
    _attach_service_credential(
        forwarded_headers,
        license_verified=tenant_license_verified(request),
        vouched=tenant_was_vouched(request),
        license_id=getattr(request.state, "license_id", None),
    )

    # Standard proxy headers
    if request.client:
        existing = forwarded_headers.get("x-forwarded-for", "")
        forwarded_headers["x-forwarded-for"] = (
            f"{existing}, {request.client.host}" if existing else request.client.host
        )
    forwarded_headers["x-forwarded-proto"] = request.url.scheme
    forwarded_headers["x-forwarded-host"] = request.headers.get("host", "")

    # Read request body
    body = await request.body()

    client = await get_proxy_client()

    # Cancellation propagation: when the client disconnects mid-flight (UI
    # cancel button, page close, network drop), cancel the upstream request
    # so coding-* services don't keep burning LLM/token quota on a job
    # nobody is reading. Single-task pattern — we run the upstream call as
    # an asyncio.Task and poll request.is_disconnected() between
    # asyncio.wait checkpoints. Avoids the two-task race that interacted
    # badly with anyio.CancelScope inside Starlette's is_disconnected
    # under ASGITransport in unit tests.
    upstream_task: asyncio.Task[httpx.Response] = asyncio.create_task(
        client.request(
            method=request.method,
            url=target_url,
            headers=forwarded_headers,
            content=body if body else None,
            # Per-request override of the shared client's read budget. Only the
            # measured blocking-analyzer routes and the long WRITE routes get more
            # than the default; see _LONG_READ_PATTERNS / _WRITE_READ_BUDGETS.
            timeout=httpx.Timeout(
                connect=5.0,
                read=read_timeout_for(request.method, full_path),
                write=30.0,
                pool=10.0,
            ),
        ),
    )

    async def _client_gone() -> bool:
        # Defensive: is_disconnected raises RuntimeError on synthetic
        # Request objects that have no receive callable (some unit tests).
        # Treat that as "cannot determine disconnect state, assume still
        # connected" so we never falsely 499 a real request.
        try:
            return bool(await request.is_disconnected())
        except RuntimeError:
            return False

    try:
        # Poll loop: wait up to 250ms for the upstream task to finish, else
        # check disconnect and loop. asyncio.shield keeps the upstream from
        # being cancelled by the wait_for timeout itself.
        backend_response: httpx.Response
        while True:
            try:
                backend_response = await asyncio.wait_for(
                    asyncio.shield(upstream_task),
                    timeout=0.25,
                )
                break
            except TimeoutError:
                if await _client_gone():
                    upstream_task.cancel()
                    # Two separate except clauses: re-raise CancelledError
                    # if the OUTER request scope was cancelled (e.g.
                    # uvicorn graceful shutdown propagating Cancel to this
                    # coroutine while we're awaiting the now-cancelled
                    # upstream_task). Otherwise it's just the upstream's
                    # own cancel completion — log and fall through to 499.
                    try:
                        await upstream_task
                    except asyncio.CancelledError:
                        current = asyncio.current_task()
                        if current is not None and current.cancelling() > 0:
                            raise
                    except Exception as cancel_err:
                        logger.debug(
                            "Upstream request cancelled (client disconnected): %s",
                            cancel_err,
                        )
                    return _proxy_error(
                        request,
                        499,
                        "Client disconnected before upstream completed",
                    )
                # Still connected — keep waiting.
                continue

        # Reject responses that exceed the size limit (100 MB)
        max_response_size = 100 * 1024 * 1024
        content_length = backend_response.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > max_response_size:
                    return _proxy_error(request, 502, "Backend response too large")
            except (ValueError, TypeError):  # fmt: skip
                logger.debug(
                    "Invalid content-length header %r, skipping size check", content_length
                )

        content = backend_response.content
        if len(content) > max_response_size:
            return _proxy_error(request, 502, "Backend response too large")

        # Build response headers (strip hop-by-hop from response too)
        response_headers = _filter_headers(dict(backend_response.headers))

        # Rewrite Location headers in 3xx responses to replace internal
        # backend hostnames with the gateway's own URL.
        if 300 <= backend_response.status_code < 400 and "location" in response_headers:
            location = response_headers["location"]
            for _prefix, backend_url in PROXY_ROUTES.items():
                if location.startswith(backend_url):
                    gateway_base = str(request.base_url).rstrip("/")
                    response_headers["location"] = location.replace(backend_url, gateway_base, 1)
                    break

        return Response(
            content=content,
            status_code=backend_response.status_code,
            headers=response_headers,
            media_type=backend_response.headers.get("content-type", "application/json"),
        )

    except httpx.ConnectError as exc:
        logger.error("Proxy connect error for %s: %s", target_url, exc)
        return _proxy_error(request, 502, "Backend service unreachable")
    except httpx.TimeoutException as exc:
        logger.error("Proxy timeout for %s: %s", target_url, exc)
        return _proxy_error(request, 504, "Backend service timeout")
    except Exception as exc:
        logger.exception("Proxy error for %s: %s", target_url, exc)
        return _proxy_error(request, 502, "Proxy error")
