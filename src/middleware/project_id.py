"""X-Project-Id observation (always) and enforcement (flagged) for the gateway.

Owner ruling 2026-10-08: every conveyor call carries its fr projectId. The n8n
coding node sends ``X-Project-Id`` on every call (n8n-nodes-coding#56). This
middleware counts each AUTHENTICATED, non-public request whose ``X-Project-Id``
is absent or not a canonical non-nil UUID, by caller and reason, on
``coding_gateway_project_id_missing_total``. ``REQUIRE_PROJECT_ID_HEADER``
(default OFF) changes only the OUTCOME — served vs 400 PROJECT_ID_REQUIRED —
never the count. gitops flips it once the counter reads zero for the callers
that matter. Same contract as worker_management #355.

It is registered INSIDE ``AuthMiddleware``, so it only ever sees requests that
authentication admitted: a 401 / 403 never reaches it and is not counted, and a
public path is recognised by ``request.state.user`` being unset (the auth chain
skips public paths without minting a user).

⛔ ``caller`` is ONLY what this service AUTHENTICATED, never X-App-Id or a
forwarded X-License-Id: a self-declared label could hide a caller's misses
under someone else's name and make "zero missing ⇒ safe to enforce" a lie.
coding-gateway verifies no service identity of its own (no Keycloak azp, no
internal secret), so the precedence is:

1. licence branch (an introspected-active ``Authorization: Bearer``) — the
   configured name for the INTROSPECTED ``license_id``
   (``PROJECT_ID_CALLER_LICENCES``), else ``licence:<license_id>``. n8n sends
   only its licence token, so this is its strongest verified identity;
2. ``trusted_network`` — the tokenless TRUSTED_CIDRS branch (empty in prod);
3. ``unknown`` — the unverified identity-header branch (TRUST_TENANT_HEADER,
   off in prod) and the dev/test API key. Never labelled from header content.

A valid value is relayed unchanged: the proxy forwards it as-is (it is in
neither ``_HOP_BY_HOP`` nor ``_CLIENT_SPOOFABLE_IDENTITY``).
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from functools import lru_cache
from typing import Any

from coding_client.auth.middleware import _user_from_license_data as _sdk_user_from_license_data
from opentelemetry import metrics
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from config.settings import get_settings, parse_project_id_caller_licences

logger = logging.getLogger(__name__)

PROJECT_ID_CALLER_LICENCE = "licence"
PROJECT_ID_CALLER_TRUSTED_NETWORK = "trusted_network"
PROJECT_ID_CALLER_UNKNOWN = "unknown"
PROJECT_ID_REASON_ABSENT = "absent"
PROJECT_ID_REASON_INVALID = "invalid"
_PROJECT_ID_REASONS = (PROJECT_ID_REASON_ABSENT, PROJECT_ID_REASON_INVALID)

_CANONICAL_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
_NIL_UUID = "00000000-0000-0000-0000-000000000000"
_LICENCE_ID_LABEL_RE = re.compile(r"[^a-z0-9-]")

# OTel counters (pushed to alloy-gateway, see observability.telemetry). Created
# from the global meter at import: before ``setup_metrics`` installs the
# provider these are API proxies that bind to the real provider once it is set.
# NOTE ON ``unit``: keep it "1" — the OTLP -> Prometheus conversion appends a
# descriptive unit to the name (worker_management agent_metrics.py has the
# measured case), which would silently rename the series dashboards match.
_meter = metrics.get_meter("coding-gateway.project_id")
PROJECT_ID_MISSING_TOTAL = _meter.create_counter(
    name="coding_gateway_project_id_missing_total",
    description=(
        "Authenticated coding-gateway requests whose X-Project-Id is absent or not a "
        "canonical non-nil UUID, by authenticated caller and reason."
    ),
    unit="1",
)
PROJECT_ID_REFUSED_TOTAL = _meter.create_counter(
    name="coding_gateway_project_id_refused_total",
    description=(
        "Requests refused 400 for a missing/invalid X-Project-Id "
        "(REQUIRE_PROJECT_ID_HEADER on), by authenticated caller and reason."
    ),
    unit="1",
)


@lru_cache(maxsize=4)
def _parsed_caller_licences(raw: str) -> dict[str, str]:
    """Parse once per distinct config value; a malformed map warns ONCE and is empty.

    Settings refuses a malformed map only while REQUIRE_PROJECT_ID_HEADER is on;
    with it off, observability config must never take the gateway down.
    """
    try:
        return parse_project_id_caller_licences(raw)
    except ValueError as exc:
        logger.warning(
            "PROJECT_ID_CALLER_LICENCES ignored (%s); licence callers are labelled "
            "licence:<license_id> until it is fixed",
            exc,
        )
        return {}


def _project_id_caller_licences() -> dict[str, str]:
    return _parsed_caller_licences(get_settings().project_id_caller_licences)


def preinit_project_id_metrics() -> None:
    """Materialise both counters at 0 for every bounded label pair.

    Called from ``create_app`` AFTER ``setup_metrics`` — an ``add(0)`` on a
    proxy instrument before the provider exists is dropped. Not ``licence:<id>``:
    those are per-licence and appear on first use.
    """
    callers = {
        PROJECT_ID_CALLER_TRUSTED_NETWORK,
        PROJECT_ID_CALLER_UNKNOWN,
        *_project_id_caller_licences().values(),
    }
    for caller in sorted(callers):
        for reason in _PROJECT_ID_REASONS:
            attributes = {"caller": caller, "reason": reason}
            PROJECT_ID_MISSING_TOTAL.add(0, attributes)
            PROJECT_ID_REFUSED_TOTAL.add(0, attributes)


def project_id_problem(raw: str | None) -> str:
    """``""`` when ``raw`` is a canonical non-nil UUID (any case), else the reason."""
    candidate = (raw or "").strip()
    if not candidate:
        return PROJECT_ID_REASON_ABSENT
    if not _CANONICAL_UUID_RE.match(candidate) or candidate == _NIL_UUID:
        return PROJECT_ID_REASON_INVALID
    return ""


def licence_caller(license_data: dict[str, Any] | None) -> str:
    """Mapped name for the INTROSPECTED license_id, else ``licence:<license_id>``.

    Unmapped ids stay individually visible (an unknown n8n instance is not
    folded into a shared bucket); cardinality is bounded by the number of
    licences. Sanitised to [a-z0-9-] and capped at 36 so a surprise value can
    never mint unbounded label text.
    """
    verified = str((license_data or {}).get("license_id") or "").strip().lower()
    mapped = _project_id_caller_licences().get(verified)
    if mapped:
        return mapped
    label_id = _LICENCE_ID_LABEL_RE.sub("", verified)[:36]
    return f"{PROJECT_ID_CALLER_LICENCE}:{label_id}" if label_id else PROJECT_ID_CALLER_LICENCE


def _headers_trusted() -> bool:
    """The SDK's TRUST_TENANT_HEADER gate, read exactly as AuthMiddleware reads it."""
    from coding_client.auth._header_trust import trust_tenant_header

    return trust_tenant_header()


async def _introspect(token: str) -> dict[str, Any] | None:
    """The SDK's ONE introspection function — a positive-cache hit right after auth."""
    from coding_client.auth.tenant import _introspect_license

    return await _introspect_license(token)


def _dev_key_branch(token: str) -> bool:
    if os.getenv("ENVIRONMENT", "production") not in ("development", "testing"):
        return False
    keys = {os.getenv("DEV_API_KEY", ""), os.getenv("TEST_API_KEY", "")} - {""}
    return bool(token) and token in keys


async def resolve_caller(request: Request) -> str:
    """Re-derive which ``AuthMiddleware`` branch admitted ``request``, in its order.

    The SDK middleware records the user but not the branch, so the branch is
    recomputed with the SDK's own predicates (dev key → identity headers →
    licence → trusted network). The licence branch re-reads the SAME cached
    introspection the auth chain just populated, so it costs no extra call.
    """
    auth_header = request.headers.get("authorization", "")
    token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
    if _dev_key_branch(token):
        return PROJECT_ID_CALLER_UNKNOWN
    if _headers_trusted() and (
        request.headers.get("x-user-id") or request.headers.get("x-tenant-id")
    ):
        return PROJECT_ID_CALLER_UNKNOWN
    if token:
        license_data = await _introspect(token)
        # The licence branch admitted the request iff the SDK's OWN admit predicate
        # accepts this introspection result; otherwise the SDK fell through to its
        # trusted-network check. Calling the SDK function (not a copy of it) keeps
        # the label exact across SDK changes — a hand copy drifted twice in review.
        if _sdk_user_from_license_data(license_data) is not None:
            return licence_caller(license_data)
    return PROJECT_ID_CALLER_TRUSTED_NETWORK


def _refusal(request: Request, reason: str) -> JSONResponse:
    # The nested KAG envelope every other gateway error uses
    # (coding_client.api.exception_handlers), plus ``reason`` so a caller can
    # tell "forgot it" from "sent garbage" without parsing the message.
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "code": "PROJECT_ID_REQUIRED",
                "message": "X-Project-Id must be the fr project UUID this call belongs to",
                "reason": reason,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "request_id": getattr(request.state, "request_id", None) or str(uuid.uuid4()),
            }
        },
    )


async def gate_project_id(request: Request) -> JSONResponse | None:
    """Count a missing/invalid X-Project-Id; return a 400 iff the flag is on.

    Observability never fails a request: a caller-resolution or counter error is
    logged and the request proceeds (or, flag on, is still refused as ``unknown``).
    """
    if os.getenv("ENABLE_AUTH", "true").lower() != "true":
        # Auth off: the anonymous user is a dev affordance, not a caller.
        return None
    if getattr(request.state, "user", None) is None:
        # Public path — the auth chain admitted it without an identity.
        return None
    reason = project_id_problem(request.headers.get("x-project-id"))
    if not reason:
        return None
    try:
        caller = await resolve_caller(request)
    except Exception:
        logger.warning("project_id caller resolution failed", exc_info=True)
        caller = PROJECT_ID_CALLER_UNKNOWN
    attributes = {"caller": caller, "reason": reason}
    try:
        PROJECT_ID_MISSING_TOTAL.add(1, attributes)
    except Exception:
        logger.warning("project_id_missing counter failed", exc_info=True)
    if not get_settings().require_project_id_header:
        return None
    try:
        PROJECT_ID_REFUSED_TOTAL.add(1, attributes)
    except Exception:
        logger.warning("project_id_refused counter failed", exc_info=True)
    logger.info(
        "X-Project-Id refused reason=%s caller=%s path=%s",
        reason,
        caller,
        request.url.path[:200],
    )
    return _refusal(request, reason)


class ProjectIdMiddleware:
    """Pure-ASGI wrapper around :func:`gate_project_id` (HTTP only; streams untouched)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            refusal = await gate_project_id(Request(scope))
            if refusal is not None:
                await refusal(scope, receive, send)
                return
        await self.app(scope, receive, send)


__all__ = [
    "PROJECT_ID_MISSING_TOTAL",
    "PROJECT_ID_REFUSED_TOTAL",
    "ProjectIdMiddleware",
    "gate_project_id",
    "licence_caller",
    "preinit_project_id_metrics",
    "project_id_problem",
    "resolve_caller",
]
