from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

import openapi_aggregator


class _Response:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


@pytest.fixture(autouse=True)
def _clear_cache():
    openapi_aggregator.invalidate_cache()
    yield
    openapi_aggregator.invalidate_cache()


async def test_fetch_spec_success():
    client = AsyncMock()
    client.get.return_value = _Response(200, {"paths": {"/health": {}}})

    result = await openapi_aggregator._fetch_spec(client, "analysis", "http://analysis")

    assert result == {"paths": {"/health": {}}}
    client.get.assert_awaited_once_with("http://analysis/openapi.json", timeout=5.0)


async def test_fetch_spec_failure_paths():
    client = AsyncMock()
    client.get.return_value = _Response(503)

    assert await openapi_aggregator._fetch_spec(client, "analysis", "http://analysis") is None

    client.get.side_effect = httpx.ConnectError("down")
    assert await openapi_aggregator._fetch_spec(client, "analysis", "http://analysis") is None


def test_merge_spec_rewrites_conflicting_schema_refs():
    base = {
        "paths": {},
        "components": {"schemas": {"Thing": {"type": "object", "title": "gateway"}}},
    }
    backend_spec = {
        "paths": {
            "/analysis": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Thing"}
                                }
                            }
                        }
                    }
                }
            }
        },
        "components": {"schemas": {"Thing": {"type": "object", "title": "backend"}}},
    }

    openapi_aggregator._merge_spec(base, "analysis", backend_spec)

    assert "/analysis" in base["paths"]
    assert "analysis_Thing" in base["components"]["schemas"]
    schema_ref = base["paths"]["/analysis"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["$ref"]
    assert schema_ref == "#/components/schemas/analysis_Thing"


async def test_get_aggregated_spec_merges_results_and_uses_cache(monkeypatch):
    gateway_spec = {"paths": {"/gateway": {}}, "components": {"schemas": {}}}
    fetch_mock = AsyncMock(
        side_effect=[
            {"paths": {"/analysis": {}}, "components": {"schemas": {"A": {"type": "object"}}}},
            RuntimeError("boom"),
        ]
    )

    monkeypatch.setattr(openapi_aggregator, "_backend_urls", lambda: {"analysis": "a", "ai": "b"})
    monkeypatch.setattr(openapi_aggregator, "_fetch_spec", fetch_mock)

    merged = await openapi_aggregator.get_aggregated_spec(gateway_spec)
    cached = await openapi_aggregator.get_aggregated_spec(gateway_spec)

    assert merged is cached
    assert set(merged["paths"]) == {"/gateway", "/analysis"}
    assert merged["components"]["schemas"]["A"]["type"] == "object"
    assert fetch_mock.await_count == 2


def test_merge_spec_rewrites_refs_inside_component_schemas():
    """H2: a renamed colliding schema must be re-pointed everywhere THIS backend
    references it — including from inside another (non-colliding) component
    schema, not only from paths.

    analysis is merged first and keeps `ContainerInfo`. security collides, so its
    `ContainerInfo` becomes `security_ContainerInfo`. security's non-colliding
    `PortMap` schema $refs `ContainerInfo`; that ref must resolve to security's
    own renamed shape, NOT analysis's earlier-merged one.
    """
    base: dict[str, Any] = {"paths": {}, "components": {"schemas": {}}}
    analysis_spec = {
        "paths": {},
        "components": {"schemas": {"ContainerInfo": {"type": "object", "title": "analysis"}}},
    }
    security_spec = {
        "paths": {
            "/security/ports": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/ContainerInfo"}
                                }
                            }
                        }
                    }
                }
            }
        },
        "components": {
            "schemas": {
                "ContainerInfo": {"type": "object", "title": "security"},
                "PortMap": {
                    "type": "object",
                    "properties": {
                        "container": {"$ref": "#/components/schemas/ContainerInfo"},
                    },
                },
            }
        },
    }

    openapi_aggregator._merge_spec(base, "analysis", analysis_spec)
    openapi_aggregator._merge_spec(base, "security", security_spec)

    schemas = base["components"]["schemas"]
    # both shapes coexist, keyed by owner
    assert schemas["ContainerInfo"]["title"] == "analysis"
    assert schemas["security_ContainerInfo"]["title"] == "security"
    # NESTED ref inside security's non-colliding PortMap points at security's own shape
    assert (
        schemas["PortMap"]["properties"]["container"]["$ref"]
        == "#/components/schemas/security_ContainerInfo"
    )
    # ref inside security's contributed path is likewise rewritten
    path_ref = base["paths"]["/security/ports"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["$ref"]
    assert path_ref == "#/components/schemas/security_ContainerInfo"


async def test_partial_aggregation_is_marked_and_not_cached_as_complete(monkeypatch):
    """H1: a scrape with a down backend must be marked incomplete and cached only
    for the short partial TTL, never the full 300s success TTL."""
    gateway_spec = {"paths": {"/gateway": {}}, "components": {"schemas": {}}}
    fetch_mock = AsyncMock(
        side_effect=[
            {"paths": {"/analysis": {}}, "components": {"schemas": {}}},
            None,  # ai is down
        ]
    )
    monkeypatch.setattr(openapi_aggregator, "_backend_urls", lambda: {"analysis": "a", "ai": "b"})
    monkeypatch.setattr(openapi_aggregator, "_fetch_spec", fetch_mock)

    before = time.time()
    merged = await openapi_aggregator.get_aggregated_spec(gateway_spec)

    ext = merged[openapi_aggregator.AGGREGATION_EXT]
    assert ext["complete"] is False
    assert ext["succeeded"] == ["analysis"]
    assert ext["failed"] == ["ai"]
    assert {b["name"]: b["ok"] for b in ext["backends"]} == {"analysis": True, "ai": False}

    # partial result must expire quickly, not linger for the full TTL
    _, expires_at = openapi_aggregator._cache
    assert expires_at - before <= openapi_aggregator._PARTIAL_CACHE_TTL + 1
    assert expires_at - before < openapi_aggregator._CACHE_TTL


async def test_complete_aggregation_is_marked_and_cached_full_ttl(monkeypatch):
    """H1: an all-backends-OK scrape is marked complete and earns the full TTL."""
    gateway_spec = {"paths": {"/gateway": {}}, "components": {"schemas": {}}}
    fetch_mock = AsyncMock(
        side_effect=[{"paths": {"/analysis": {}}, "components": {"schemas": {}}}]
    )
    monkeypatch.setattr(openapi_aggregator, "_backend_urls", lambda: {"analysis": "a"})
    monkeypatch.setattr(openapi_aggregator, "_fetch_spec", fetch_mock)

    before = time.time()
    merged = await openapi_aggregator.get_aggregated_spec(gateway_spec)

    ext = merged[openapi_aggregator.AGGREGATION_EXT]
    assert ext["complete"] is True
    assert ext["failed"] == []
    _, expires_at = openapi_aggregator._cache
    assert expires_at - before > openapi_aggregator._PARTIAL_CACHE_TTL


def test_merge_spec_prefixes_duplicate_operation_ids():
    base: dict[str, Any] = {
        "paths": {
            "/gateway/health": {
                "get": {"operationId": "health_check", "responses": {}},
            }
        },
        "components": {"schemas": {}},
    }
    backend_spec: dict[str, Any] = {
        "paths": {
            "/analysis/health": {
                "get": {"operationId": "health_check", "responses": {}},
            }
        },
    }

    openapi_aggregator._merge_spec(base, "analysis", backend_spec)

    gateway_oid = base["paths"]["/gateway/health"]["get"]["operationId"]
    backend_oid = base["paths"]["/analysis/health"]["get"]["operationId"]
    assert gateway_oid == "health_check"
    assert backend_oid == "analysis_health_check"


def test_postprocess_spec_sets_servers_security_tags_and_response_schemas():
    spec: dict[str, Any] = {
        "paths": {
            "/gateway": {
                "get": {
                    "operationId": "root",
                    "responses": {
                        "200": {"content": {"application/json": {}}},
                    },
                }
            },
            "/api/foo": {
                "post": {
                    "operationId": "do_foo",
                    "tags": ["pre-existing"],
                    "responses": {
                        "200": {"content": {"application/json": {"schema": {"type": "string"}}}},
                    },
                }
            },
        },
        "components": {},
    }
    path_origin = {"/api/foo": "analysis"}

    openapi_aggregator._postprocess_spec(spec, path_origin)

    # servers — sourced from settings.public_server_url (gitops env)
    from config.settings import get_settings

    assert spec["servers"][0]["url"] == get_settings().public_server_url
    # global security
    assert spec["security"] == [{"BearerAuth": []}]
    # BearerAuth scheme present
    assert "BearerAuth" in spec["components"]["securitySchemes"]
    # missing tag backfilled with gateway tag
    assert spec["paths"]["/gateway"]["get"]["tags"] == [openapi_aggregator.GATEWAY_TAG]
    # pre-existing tags preserved
    assert spec["paths"]["/api/foo"]["post"]["tags"] == ["pre-existing"]
    # missing response schema backfilled
    backfilled = spec["paths"]["/gateway"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert backfilled["type"] == "object"
    assert backfilled["additionalProperties"] is True
    # existing response schema preserved
    kept = spec["paths"]["/api/foo"]["post"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    assert kept == {"type": "string"}


def test_postprocess_spec_injects_operation_role_and_verifiers():
    spec: dict[str, Any] = {
        "paths": {
            "/api/repositories/apply-changes": {
                "post": {"operationId": "apply_code_changes", "responses": {}},
            },
            "/api/quality/analyze-sync": {
                "post": {"operationId": "run_quality_analysis_sync", "responses": {}},
            },
            "/api/workspaces/{container_name}/files": {
                "get": {"operationId": "list_workspace_files", "responses": {}},
            },
        },
        "components": {},
    }

    openapi_aggregator._postprocess_spec(spec, {})

    apply_op = spec["paths"]["/api/repositories/apply-changes"]["post"]
    assert apply_op["x-operation-role"] == "producer"
    assert "testing_execute_test" in apply_op["x-verifies"]
    # POST-but-observer compute op is demoted, gets no verifiers
    quality_op = spec["paths"]["/api/quality/analyze-sync"]["post"]
    assert quality_op["x-operation-role"] == "observer"
    assert "x-verifies" not in quality_op
    # plain read
    assert (
        spec["paths"]["/api/workspaces/{container_name}/files"]["get"]["x-operation-role"]
        == "observer"
    )
