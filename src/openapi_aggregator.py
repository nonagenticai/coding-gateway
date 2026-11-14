"""OpenAPI spec aggregator.

Fetches /openapi.json from each backend microservice and merges all paths
and components into a single unified spec that the gateway exposes.
Results are cached with a configurable TTL.

The merged spec is normalised for downstream codegen (orval) friendliness:
  * `servers` is set to the public gateway URL.
  * Global `security` is set to BearerAuth so generated clients send the token.
  * Operations missing `tags` are tagged with the originating backend
    (or "coding-gateway" for gateway-owned paths).
  * `application/json` response content without a schema is backfilled with
    a generic `{"type": "object", "additionalProperties": true}` schema so
    orval generates a typed response shape instead of `unknown`.
  * `operationId` collisions across services are resolved by prefixing with
    the originating backend name (orval errors hard on duplicate IDs).
"""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from typing import Any

import httpx

from config.settings import get_settings
from operation_metadata import annotate_operation

logger = logging.getLogger(__name__)

# Cache: (spec_dict, expires_at). expires_at is an absolute monotonic-ish wall
# clock deadline so complete and partial aggregations can carry DIFFERENT TTLs.
_cache: tuple[dict | None, float] = (None, 0.0)
_CACHE_TTL = 300  # 5 minutes — only a COMPLETE aggregation earns the full TTL.
# H1: a partial aggregation (>=1 backend down) must NOT be cached as if complete.
# A short TTL means a recovered backend reappears in the merged spec within
# seconds instead of being masked for up to 5 minutes, while still preserving
# the "gateway keeps serving when a backend is down" behavior.
_PARTIAL_CACHE_TTL = 15  # seconds

# Extension key advertising per-backend fetch status on the merged spec so
# consumers (fr codegen, drift checks) can tell a partial scrape from a
# legitimately-complete one instead of reading a missing service as a deletion.
#
# INVARIANT: every spec the gateway serves carries this key. A *missing* key is
# indistinguishable from "this consumer is talking to an older gateway", so a
# consumer gating on `complete` would read an un-aggregated spec as authoritative.
# The total-failure path therefore stamps an explicit ``complete: False`` via
# `spec_with_failure_marker` rather than serving a bare, unmarked gateway spec.
AGGREGATION_EXT = "x-coding-aggregation"

# Path prefixes owned by the gateway itself (not aggregated from a backend).
GATEWAY_TAG = "coding-gateway"

_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "options", "head", "trace"})


def _public_server_url() -> str:
    """Return the public-facing gateway URL from settings (gitops env)."""
    return get_settings().public_server_url


# Map of service name → base URL. URLs come from settings (gitops env);
# missing entries in production fail at Settings boot via _REQUIRED_IN_PRODUCTION.
def _backend_urls() -> dict[str, str]:
    settings = get_settings()
    return {
        "analysis": settings.analysis_url,
        "testing": settings.testing_url,
        "ai": settings.ai_url,
        "containers": settings.containers_url,
        "github": settings.github_url,
        "security": settings.security_url,
        "realtime": settings.realtime_url,
    }


async def _fetch_spec(client: httpx.AsyncClient, name: str, base_url: str) -> dict | None:
    """Fetch /openapi.json from a single backend. Returns None on failure."""
    url = f"{base_url.rstrip('/')}/openapi.json"
    try:
        resp = await client.get(url, timeout=5.0)
        if resp.status_code == 200:
            return resp.json()
        logger.warning("Backend %s returned %s for /openapi.json", name, resp.status_code)
    except Exception as exc:
        logger.warning("Failed to fetch OpenAPI from %s (%s): %s", name, url, exc)
    return None


def _merge_spec(
    base: dict,
    backend_name: str,
    backend_spec: dict,
    path_origin: dict[str, str] | None = None,
) -> None:
    """Merge paths and components from a backend spec into the base spec.

    If `path_origin` is provided, each newly-merged path is recorded as
    originating from `backend_name` for later post-processing (tagging,
    operationId disambiguation).
    """
    # Track existing operationIds (from base + previously-merged backends) so
    # we can detect collisions when this backend tries to add a duplicate.
    existing_op_ids: set[str] = set()
    for _path, _item in base.get("paths", {}).items():
        if not isinstance(_item, dict):
            continue
        for _method, _op in _item.items():
            if _method in _HTTP_METHODS and isinstance(_op, dict):
                _oid = _op.get("operationId")
                if isinstance(_oid, str):
                    existing_op_ids.add(_oid)

    # Merge paths. Track exactly which paths THIS backend contributed so ref
    # rewriting can be scoped to this backend's own content (see _rewrite_refs).
    merged_paths: list[str] = []
    for path, path_item in backend_spec.get("paths", {}).items():
        if path not in base["paths"]:
            new_item = copy.deepcopy(path_item)
            # Resolve operationId collisions by prefixing with the backend name.
            if isinstance(new_item, dict):
                for method, operation in new_item.items():
                    if method not in _HTTP_METHODS or not isinstance(operation, dict):
                        continue
                    op_id = operation.get("operationId")
                    if isinstance(op_id, str) and op_id in existing_op_ids:
                        operation["operationId"] = f"{backend_name}_{op_id}"
                    if isinstance(operation.get("operationId"), str):
                        existing_op_ids.add(operation["operationId"])
            base["paths"][path] = new_item
            merged_paths.append(path)
            if path_origin is not None:
                path_origin[path] = backend_name

    # Merge components/schemas with a service prefix on name collisions.
    backend_schemas = backend_spec.get("components", {}).get("schemas", {})
    if backend_schemas:
        base.setdefault("components", {}).setdefault("schemas", {})

        # Compute the rename map (colliding name -> prefixed name) BEFORE copying
        # so it reflects the base as it was prior to this backend's contribution.
        rename: dict[str, str] = {}
        for schema_name in backend_schemas:
            if schema_name in base["components"]["schemas"]:
                rename[schema_name] = f"{backend_name}_{schema_name}"

        # Copy all of this backend's schemas into base under their target names,
        # tracking the target names so we can rewrite refs across them below.
        contributed_targets: list[str] = []
        for schema_name, schema_def in backend_schemas.items():
            target_name = rename.get(schema_name, schema_name)
            base["components"]["schemas"][target_name] = copy.deepcopy(schema_def)
            contributed_targets.append(target_name)

        # If anything was renamed, rewrite every $ref to the old name across ALL
        # of this backend's contributed content — its paths AND its component
        # schemas (nested $refs). H2: a non-colliding schema of this backend that
        # $refs the colliding name would otherwise still resolve to the earlier-
        # merged (other service's) shape — silent cross-service type contamination.
        if rename:
            _rewrite_refs(base, rename, merged_paths, contributed_targets)


def _rewrite_refs(
    base: dict,
    rename: dict[str, str],
    backend_paths: list[str],
    contributed_schema_targets: list[str],
) -> None:
    """Rewrite renamed-schema $refs across everything a single backend contributed.

    Scope is deliberately limited to this backend's own contributed content —
    its merged paths plus its merged component schemas (which may themselves
    reference each other). Refs inside the gateway's or another backend's
    content are left untouched: a `#/components/schemas/X` there means THAT
    owner's X, which was already disambiguated when it was merged.
    """
    schemas = base.get("components", {}).get("schemas", {})
    targets: list[Any] = []
    for path in backend_paths:
        if path in base.get("paths", {}):
            targets.append(base["paths"][path])
    for name in contributed_schema_targets:
        if name in schemas:
            targets.append(schemas[name])

    for old_name, new_name in rename.items():
        old_ref = f"#/components/schemas/{old_name}"
        new_ref = f"#/components/schemas/{new_name}"
        for obj in targets:
            _deep_replace_ref(obj, old_ref, new_ref)


def _deep_replace_ref(obj, old_ref: str, new_ref: str) -> None:
    """Recursively replace $ref values in a nested dict/list."""
    if isinstance(obj, dict):
        if obj.get("$ref") == old_ref:
            obj["$ref"] = new_ref
        for v in obj.values():
            _deep_replace_ref(v, old_ref, new_ref)
    elif isinstance(obj, list):
        for item in obj:
            _deep_replace_ref(item, old_ref, new_ref)


_GENERIC_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "title": "JSONResponse",
    "description": (
        "Untyped JSON object. Backfilled by the gateway aggregator when the "
        "originating handler did not declare a response_model."
    ),
}


def _postprocess_spec(spec: dict, path_origin: dict[str, str]) -> None:
    """Normalise the merged spec for orval / external consumers.

    Applies in-place:
      * sets `servers` to the public gateway URL,
      * sets a global BearerAuth `security` requirement,
      * backfills missing operation `tags` based on path origin,
      * backfills missing `application/json` response schemas with a generic
        object schema so orval generates a typed (if loose) response shape.
    """
    # Deferred: routers.proxy reads settings at import time.
    from routers.proxy import request_timeout_ms_for

    spec["servers"] = [
        {"url": _public_server_url(), "description": "Production gateway"},
    ]

    # Node identity for the n8n generator: displayName from info.title, node
    # description + projectId requirement from info["x-n8n"].
    settings = get_settings()
    info = spec.setdefault("info", {})
    info["title"] = settings.openapi_title
    info["x-n8n"] = {"description": settings.n8n_node_description, "projectIdRequired": True}

    # Ensure BearerAuth security scheme exists, then set global security.
    components = spec.setdefault("components", {})
    sec_schemes = components.setdefault("securitySchemes", {})
    sec_schemes.setdefault(
        "BearerAuth",
        {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "Bearer JWT issued by portal.nonagentic.ai (Keycloak).",
        },
    )
    spec["security"] = [{"BearerAuth": []}]

    paths = spec.get("paths") or {}
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        origin = path_origin.get(path, GATEWAY_TAG)
        for method, operation in path_item.items():
            if method not in _HTTP_METHODS or not isinstance(operation, dict):
                continue

            # Backfill tags from the originating backend.
            tags = operation.get("tags")
            if not tags:
                operation["tags"] = [origin]

            # Inject structural-oracle metadata (producer/observer role + the
            # in-node verifier topology) so downstream tooling (n8n node, AA
            # qa.verify) can confirm producer effects against ground truth.
            annotate_operation(operation, method)

            # Client timeout = the proxy's read budget for this route + a margin
            # (routers.proxy.request_timeout_ms_for), so generated clients never
            # need a hand-written per-operation timeout table.
            operation["x-request-timeout-ms"] = request_timeout_ms_for(method, path)

            # Backfill missing response schemas for application/json content.
            responses = operation.get("responses") or {}
            for _code, resp in responses.items():
                if not isinstance(resp, dict):
                    continue
                content = resp.get("content")
                if not isinstance(content, dict):
                    continue
                json_content = content.get("application/json")
                if isinstance(json_content, dict) and not json_content.get("schema"):
                    json_content["schema"] = copy.deepcopy(_GENERIC_JSON_SCHEMA)


async def get_aggregated_spec(gateway_spec: dict) -> dict:
    """Return the gateway's OpenAPI spec merged with all backend specs.

    Uses a cached result if available and fresh (< TTL seconds old).
    """
    global _cache

    cached_spec, expires_at = _cache
    if cached_spec is not None and time.time() < expires_at:
        return cached_spec

    merged = copy.deepcopy(gateway_spec)
    merged.setdefault("paths", {})

    # Track which backend supplied each path so post-processing can apply
    # service-aware defaults (tags, fallback schemas, operationId prefixing).
    path_origin: dict[str, str] = {}

    backends = _backend_urls()
    succeeded = []
    failed = []
    async with httpx.AsyncClient() as client:
        tasks = {name: _fetch_spec(client, name, url) for name, url in backends.items()}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for name, result in zip(tasks.keys(), results):
            if isinstance(result, BaseException):
                logger.warning("Backend %s raised exception: %s", name, result)
                failed.append(name)
                continue
            if result:
                _merge_spec(merged, name, result, path_origin)
                succeeded.append(name)
                logger.info("Merged %d paths from %s", len(result.get("paths", {})), name)
            else:
                failed.append(name)

    if failed:
        logger.warning(
            "OpenAPI aggregation: %d backends failed: %s", len(failed), ", ".join(failed)
        )

    # Normalise the merged spec for orval / external consumers.
    _postprocess_spec(merged, path_origin)

    # H1: make partial aggregation VISIBLE. Stamp a per-backend status block onto
    # the merged spec so a downstream consumer can distinguish "this service has
    # no routes because it was legitimately removed" from "this service was down
    # during the scrape". `complete` is the single boolean a drift check / client
    # generator should gate on before treating the spec as authoritative.
    succeeded_set = set(succeeded)
    complete = not failed
    merged[AGGREGATION_EXT] = {
        "complete": complete,
        "generated_at": time.time(),
        "backends": [{"name": name, "ok": name in succeeded_set} for name in backends],
        "succeeded": succeeded,
        "failed": failed,
    }

    # H1: a degraded aggregation is cached for only a short TTL (not the full 5
    # min) so it is not silently served as complete for minutes after a backend
    # recovers. A complete aggregation earns the full TTL.
    ttl = _CACHE_TTL if complete else _PARTIAL_CACHE_TTL
    _cache = (merged, time.time() + ttl)
    logger.info(
        "Aggregated OpenAPI: %d total paths (%d backends OK, %d failed, complete=%s, ttl=%ss)",
        len(merged.get("paths", {})),
        len(succeeded),
        len(failed),
        complete,
        ttl,
    )
    return merged


def failure_marker(error: str) -> dict[str, Any]:
    """Build an honest ``x-coding-aggregation`` block for a TOTAL aggregation failure.

    `get_aggregated_spec` raising means nothing was merged: no backend contributed
    a path, so every configured backend is reported as failed and `complete` is
    False. This is the marker the gateway-only fallback spec carries so a consumer
    gating on `complete` sees an explicit False instead of a missing key.
    """
    try:
        backends = list(_backend_urls())
    except Exception:  # pragma: no cover - settings failure is already fatal upstream
        logger.warning("Could not enumerate backends for the failure marker", exc_info=True)
        backends = []
    return {
        "complete": False,
        "generated_at": time.time(),
        "backends": [{"name": name, "ok": False} for name in backends],
        "succeeded": [],
        "failed": backends,
        "error": error,
    }


def spec_with_failure_marker(spec: dict[str, Any], error: str) -> dict[str, Any]:
    """Return a shallow copy of *spec* stamped with a total-failure marker.

    Shallow: only a top-level key is added, and the input must not be mutated —
    `app.openapi()` hands back FastAPI's own cached spec object.
    """
    return {**spec, AGGREGATION_EXT: failure_marker(error)}


def invalidate_cache() -> None:
    """Force re-fetch on next request."""
    global _cache
    _cache = (None, 0.0)
