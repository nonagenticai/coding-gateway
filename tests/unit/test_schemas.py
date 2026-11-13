"""Tests for models/schemas.py – Pydantic schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from models.schemas import (
    BaseResponse,
    CacheClearResponse,
    CacheEvictionEntry,
    CacheEvictionHistoryResponse,
    CacheEvictionReason,
    CacheKeysResponse,
    CachePreloadConfig,
    CachePreloadConfigResponse,
    CachePreloadOperation,
    CacheStatsResponse,
    CodeValidationErrorResponse,
    ComponentHealth,
    CoreAnalysisResult,
    CoreCodeEntity,
    CSharpValidationRequest,
    EntityRelation,
    EntityType,
    GoValidationRequest,
    HealthStatus,
    JavaScriptValidationRequest,
    JavaValidationRequest,
    OverallGraphMetrics,
    PaginatedResponse,
    PaginationParams,
    PythonValidationRequest,
    RelationType,
    ResponseStatus,
    RustValidationRequest,
    SourceLocation,
    StandardErrorResponse,
    SuccessResponse,
    ValidationErrorResponse,
    ValidationRequest,
    ValidationResponse,
)

# ---------------------------------------------------------------------------
# BaseResponse / SuccessResponse
# ---------------------------------------------------------------------------


class TestBaseResponse:
    def test_defaults(self):
        resp = BaseResponse(status=ResponseStatus.SUCCESS, message="ok")
        assert resp.status == "success"
        assert resp.message == "ok"
        assert isinstance(resp.request_id, str) and len(resp.request_id) > 0
        assert resp.timestamp is not None

    def test_custom_request_id(self):
        resp = BaseResponse(status=ResponseStatus.ERROR, message="fail", request_id="custom_id")
        assert resp.request_id == "custom_id"


class TestSuccessResponse:
    def test_default_status(self):
        resp = SuccessResponse(message="done")
        assert resp.status == ResponseStatus.SUCCESS

    def test_with_data(self):
        resp = SuccessResponse(message="done", data={"key": "val"})
        assert resp.data["key"] == "val"

    def test_no_data(self):
        resp = SuccessResponse(message="done")
        assert resp.data is None


# ---------------------------------------------------------------------------
# PaginationParams / PaginatedResponse
# ---------------------------------------------------------------------------


class TestPaginationParams:
    def test_defaults(self):
        p = PaginationParams()
        assert p.page == 1
        assert p.limit == 50

    def test_custom(self):
        p = PaginationParams(page=3, limit=10)
        assert p.page == 3
        assert p.limit == 10

    def test_page_ge_1(self):
        with pytest.raises(ValidationError):
            PaginationParams(page=0)

    def test_limit_ge_1(self):
        with pytest.raises(ValidationError):
            PaginationParams(limit=0)

    def test_limit_le_1000(self):
        with pytest.raises(ValidationError):
            PaginationParams(limit=1001)


class TestPaginatedResponse:
    def test_valid_pagination(self):
        resp = PaginatedResponse(
            message="ok",
            pagination={"page": 1, "limit": 10, "total": 100, "pages": 10},
            items=[1, 2, 3],
        )
        assert resp.pagination["total"] == 100
        assert len(resp.items) == 3

    def test_invalid_pagination_missing_keys(self):
        with pytest.raises(ValidationError):
            PaginatedResponse(
                message="ok",
                pagination={"page": 1},
                items=[],
            )


# ---------------------------------------------------------------------------
# Health models
# ---------------------------------------------------------------------------


class TestHealthModels:
    def test_health_status_enum(self):
        assert HealthStatus.HEALTHY == "healthy"
        assert HealthStatus.DEGRADED == "degraded"
        assert HealthStatus.UNHEALTHY == "unhealthy"
        assert HealthStatus.UNKNOWN == "unknown"

    def test_component_health(self):
        ch = ComponentHealth(name="DB", status=HealthStatus.HEALTHY)
        assert ch.name == "DB"
        assert ch.message is None
        assert ch.metrics is None

    def test_component_health_with_metrics(self):
        ch = ComponentHealth(
            name="Redis",
            status=HealthStatus.DEGRADED,
            message="Slow",
            metrics={"latency_ms": 50},
        )
        assert ch.metrics["latency_ms"] == 50


# ---------------------------------------------------------------------------
# Error responses
# ---------------------------------------------------------------------------


class TestErrorResponses:
    def test_standard_error_defaults(self):
        err = StandardErrorResponse(error="Internal error")
        assert err.error_code == "INTERNAL_ERROR"
        assert err.status_code == 500

    def test_standard_error_custom(self):
        err = StandardErrorResponse(error="Not found", error_code="NOT_FOUND", status_code=404)
        assert err.status_code == 404

    def test_standard_error_extra_forbidden(self):
        with pytest.raises(ValidationError):
            StandardErrorResponse(error="err", extra_field="bad")

    def test_validation_error_response(self):
        ver = ValidationErrorResponse(details=[{"field": "email", "message": "invalid"}])
        assert ver.error_code == "VALIDATION_ERROR"
        assert ver.status_code == 422
        assert len(ver.details) == 1


# ---------------------------------------------------------------------------
# Entity / Relation enums
# ---------------------------------------------------------------------------


class TestEnums:
    def test_entity_types(self):
        assert EntityType.MODULE == "module"
        assert EntityType.CLASS == "class"
        assert EntityType.FUNCTION == "function"
        assert EntityType.FILE == "file"

    def test_relation_types(self):
        assert RelationType.CALLS == "calls"
        assert RelationType.IMPORTS == "imports"
        assert RelationType.INHERITS == "inherits"


# ---------------------------------------------------------------------------
# Core domain models
# ---------------------------------------------------------------------------


class TestCoreCodeEntity:
    def test_minimal(self):
        e = CoreCodeEntity(entity_id="e1", name="Foo", entity_type=EntityType.CLASS)
        assert e.entity_id == "e1"
        assert e.properties == {}
        assert e.language is None

    def test_full(self):
        e = CoreCodeEntity(
            entity_id="e1",
            name="Foo",
            entity_type=EntityType.FUNCTION,
            qualified_name="mod.Foo",
            language="python",
            complexity=5,
            properties={"async": True},
        )
        assert e.qualified_name == "mod.Foo"
        assert e.complexity == 5

    def test_extra_forbidden(self):
        with pytest.raises(ValidationError):
            CoreCodeEntity(entity_id="e1", name="Foo", entity_type="class", bad_field="x")


class TestEntityRelation:
    def test_minimal(self):
        r = EntityRelation(source_id="a", target_id="b", relation_type=RelationType.CALLS)
        assert r.weight == 1.0
        assert r.properties == {}

    def test_extra_forbidden(self):
        with pytest.raises(ValidationError):
            EntityRelation(source_id="a", target_id="b", relation_type="calls", extra="bad")


class TestSourceLocation:
    def test_minimal(self):
        sl = SourceLocation(file_path="src/main.py", line_start=1)
        assert sl.line_end is None
        assert sl.column_start is None

    def test_full(self):
        sl = SourceLocation(
            file_path="src/main.py", line_start=10, line_end=20, column_start=0, column_end=50
        )
        assert sl.line_end == 20


class TestOverallGraphMetrics:
    def test_defaults(self):
        m = OverallGraphMetrics()
        assert m.node_count == 0
        assert m.edge_count == 0
        assert m.average_degree == 0.0
        assert m.density == 0.0


class TestCoreAnalysisResult:
    def test_minimal(self):
        r = CoreAnalysisResult(analysis_id="a1", status="completed")
        assert r.entities == []
        assert r.relations == []


# ---------------------------------------------------------------------------
# Validation models
# ---------------------------------------------------------------------------


class TestValidationModels:
    def test_validation_request_requires_code(self):
        with pytest.raises(ValidationError):
            ValidationRequest()

    def test_validation_request_strips_whitespace(self):
        vr = ValidationRequest(code="  hello  ")
        assert vr.code == "hello"

    def test_javascript_defaults(self):
        jvr = JavaScriptValidationRequest(code="const x = 1")
        assert jvr.filename == "test.js"

    def test_python_defaults(self):
        pvr = PythonValidationRequest(code="x = 1")
        assert pvr.filename == "test.py"

    def test_java_defaults(self):
        jvr = JavaValidationRequest(code="class Test {}")
        assert jvr.className == "Test"

    def test_rust_defaults(self):
        rvr = RustValidationRequest(code="fn main() {}")
        assert rvr.crateType == "bin"

    def test_csharp_defaults(self):
        cvr = CSharpValidationRequest(code="class Test {}")
        assert cvr.projectType == "console"
        assert cvr.targetFramework == "net8.0"

    def test_go_defaults(self):
        gvr = GoValidationRequest(code="package main")
        assert gvr.filename == "main.go"

    def test_validation_response(self):
        vr = ValidationResponse(validationId="v1", valid=True)
        assert vr.errors == []
        assert vr.output == ""

    def test_code_validation_error_response(self):
        cvr = CodeValidationErrorResponse(error="syntax error")
        assert cvr.valid is False
        assert cvr.validationId is None


# ---------------------------------------------------------------------------
# Cache models
# ---------------------------------------------------------------------------


class TestCacheModels:
    def test_cache_stats_defaults(self):
        cs = CacheStatsResponse()
        assert cs.total_keys == 0
        assert cs.hit_ratio == 0.0

    def test_cache_stats_computed_ratio_when_zero_provided(self):
        """When hit_ratio=0.0 is explicitly passed, it computes from hits/misses."""
        cs = CacheStatsResponse(hits=75, misses=25, hit_ratio=0.0)
        assert cs.hit_ratio == 0.75

    def test_cache_stats_zero_total_computed(self):
        cs = CacheStatsResponse(hits=0, misses=0, hit_ratio=0.0)
        assert cs.hit_ratio == 0.0

    def test_cache_stats_explicit_nonzero_ratio_preserved(self):
        """When hit_ratio is explicitly non-zero, it is preserved as-is."""
        cs = CacheStatsResponse(hits=10, misses=10, hit_ratio=0.99)
        assert cs.hit_ratio == 0.99

    def test_cache_stats_default_ratio_without_explicit(self):
        """When hit_ratio is omitted, the field default (0.0) is used."""
        cs = CacheStatsResponse(hits=75, misses=25)
        assert cs.hit_ratio == 0.0

    def test_cache_clear_response(self):
        ccr = CacheClearResponse(cleared_keys=5)
        assert ccr.message == "Cache cleared successfully"

    def test_cache_keys_response(self):
        ckr = CacheKeysResponse(keys=["k1", "k2"], total=2, pattern="prefix:*")
        assert ckr.total == 2

    def test_cache_preload_operation(self):
        op = CachePreloadOperation(key="test:key", ttl=60)
        assert op.key == "test:key"

    def test_cache_preload_config(self):
        config = CachePreloadConfig(operations=[CachePreloadOperation(key="k1")])
        assert len(config.operations) == 1

    def test_cache_preload_config_response(self):
        resp = CachePreloadConfigResponse(preloaded=5, failed=1)
        assert resp.status == "ok"

    def test_cache_eviction_reason(self):
        assert CacheEvictionReason.TTL_EXPIRED == "ttl_expired"
        assert CacheEvictionReason.MANUAL == "manual"

    def test_cache_eviction_entry(self):
        entry = CacheEvictionEntry(key="k1", reason=CacheEvictionReason.LRU)
        assert entry.evicted_at is not None
        assert entry.size_bytes is None

    def test_cache_eviction_history_response(self):
        resp = CacheEvictionHistoryResponse(total=0)
        assert resp.entries == []


# ---------------------------------------------------------------------------
# Nested serialization
# ---------------------------------------------------------------------------


class TestNestedSerialization:
    def test_core_entity_with_location(self):
        e = CoreCodeEntity(
            entity_id="e1",
            name="Foo",
            entity_type=EntityType.FUNCTION,
            location=SourceLocation(file_path="main.py", line_start=10, line_end=20),
        )
        data = e.model_dump()
        assert data["location"]["file_path"] == "main.py"
        assert data["location"]["line_start"] == 10

    def test_analysis_result_with_entities(self):
        result = CoreAnalysisResult(
            analysis_id="a1",
            status="completed",
            entities=[CoreCodeEntity(entity_id="e1", name="Foo", entity_type=EntityType.CLASS)],
            relations=[
                EntityRelation(source_id="e1", target_id="e2", relation_type=RelationType.CALLS)
            ],
        )
        data = result.model_dump()
        assert len(data["entities"]) == 1
        assert len(data["relations"]) == 1
        assert data["entities"][0]["entity_id"] == "e1"
        assert data["relations"][0]["relation_type"] == "calls"


# ---------------------------------------------------------------------------
# ResponseStatus enum
# ---------------------------------------------------------------------------


class TestResponseStatus:
    def test_all_values(self):
        assert ResponseStatus.SUCCESS == "success"
        assert ResponseStatus.ERROR == "error"
        assert ResponseStatus.PENDING == "pending"
        assert ResponseStatus.RUNNING == "running"
        assert ResponseStatus.COMPLETED == "completed"
        assert ResponseStatus.FAILED == "failed"
