"""Pydantic schemas for the coding-gateway.

Contains shared domain models, base response types, validation models,
and cache models.  Graph CRUD request/response models are defined in
routers/graphs.py alongside the endpoints that use them.

Domains covered:
- Common response / pagination / health models
- Standard and validation error responses
- Core entity/relation enums and domain models
- Multi-language code validation request/response models
- Cache statistics and eviction models
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ── SafeString pattern ────────────────────────────────────────────────
# Blocks dangerous control characters while allowing printable ASCII,
# tab, newline, carriage return, and Unicode.
SAFE_STRING_PATTERN = r"^[^\x00-\x08\x0B\x0C\x0E-\x1F\x7F]*$"


# =====================================================================
#  Common / base response models
# =====================================================================


class ResponseStatus(StrEnum):
    """Standard API response status values."""

    SUCCESS = "success"
    ERROR = "error"
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class BaseResponse(BaseModel):
    """Base response with standard metadata fields."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "success",
                "message": "Operation completed successfully",
                "timestamp": "2025-07-13T10:30:00Z",
                "request_id": "req_12345",
            }
        }
    )

    status: ResponseStatus = Field(..., description="Response status")
    message: str = Field(..., description="Human-readable message", pattern=SAFE_STRING_PATTERN)
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Response timestamp",
    )
    request_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        min_length=1,
        description="Unique request identifier",
    )


class SuccessResponse(BaseResponse):
    """Standard success response with optional data payload."""

    status: ResponseStatus = ResponseStatus.SUCCESS
    data: dict[str, Any] | None = Field(None, description="Response data")


class PaginationParams(BaseModel):
    """Pagination parameters for list endpoints."""

    model_config = ConfigDict(json_schema_extra={"example": {"page": 1, "limit": 20}})

    page: int = Field(1, ge=1, description="Page number (1-based)")
    limit: int = Field(50, ge=1, le=1000, description="Items per page")


class PaginatedResponse(SuccessResponse):
    """Response for paginated list endpoints."""

    pagination: dict[str, Any] = Field(..., description="Pagination metadata")
    items: list[Any] = Field(..., description="Response items")

    @field_validator("pagination")
    @classmethod
    def validate_pagination(cls, v: dict[str, Any]) -> dict[str, Any]:
        required_keys = {"page", "limit", "total", "pages"}
        if not all(key in v for key in required_keys):
            raise ValueError(f"Pagination must contain: {required_keys}")
        return v


class HealthStatus(StrEnum):
    """System and component health status values."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class ComponentHealth(BaseModel):
    """Health status for an individual system component."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "Database",
                "status": "healthy",
                "message": "All connections active",
                "last_check": "2025-07-13T10:30:00Z",
                "metrics": {"connections": 15, "response_time_ms": 12.5},
            }
        }
    )

    name: str = Field(..., min_length=1, pattern=SAFE_STRING_PATTERN, description="Component name")
    status: HealthStatus = Field(..., description="Health status")
    message: str | None = Field(None, pattern=SAFE_STRING_PATTERN, description="Status message")
    last_check: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Last health check",
    )
    metrics: dict[str, Any] | None = Field(None, description="Component metrics")


# =====================================================================
#  Error response models
# =====================================================================


class StandardErrorResponse(BaseModel):
    """Standard error response body."""

    model_config = ConfigDict(extra="forbid")

    error: str = Field(..., description="Error message", pattern=SAFE_STRING_PATTERN)
    error_code: str = Field(default="INTERNAL_ERROR", description="Machine-readable error code")
    status_code: int = Field(default=500, description="HTTP status code")
    request_id: str | None = Field(None, description="Request ID for correlation")
    details: dict[str, Any] | None = Field(None, description="Additional error details")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ValidationErrorResponse(BaseModel):
    """Validation error response with field-level detail."""

    model_config = ConfigDict(extra="forbid")

    error: str = Field(default="Validation Error", pattern=SAFE_STRING_PATTERN)
    error_code: str = Field(default="VALIDATION_ERROR")
    status_code: int = Field(default=422)
    request_id: str | None = Field(None)
    details: list[dict[str, Any]] = Field(default_factory=list, description="Field-level errors")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


# =====================================================================
#  Entity / relation enums and core domain models
# =====================================================================


class EntityType(StrEnum):
    """Types of code entities that can be represented in the graph."""

    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    VARIABLE = "variable"
    IMPORT = "import"
    DECORATOR = "decorator"
    PARAMETER = "parameter"
    ATTRIBUTE = "attribute"
    PROPERTY = "property"
    INTERFACE = "interface"
    ENUM = "enum"
    CONSTANT = "constant"
    TYPE_ALIAS = "type_alias"
    PROTOCOL = "protocol"
    FILE = "file"
    PACKAGE = "package"
    NAMESPACE = "namespace"


class RelationType(StrEnum):
    """Types of relationships between code entities."""

    CALLS = "calls"
    IMPORTS = "imports"
    INHERITS = "inherits"
    IMPLEMENTS = "implements"
    CONTAINS = "contains"
    USES = "uses"
    REFERENCES = "references"
    DECORATES = "decorates"
    OVERRIDES = "overrides"
    DEPENDS_ON = "depends_on"
    MEMBER_OF = "member_of"
    RETURNS = "returns"
    RAISES = "raises"
    CATCHES = "catches"
    TYPE_OF = "type_of"
    INSTANTIATES = "instantiates"


class SourceLocation(BaseModel):
    """Source-code location for a code entity."""

    file_path: str = Field(..., description="Relative file path")
    line_start: int = Field(..., ge=1, description="Starting line number")
    line_end: int | None = Field(None, ge=1, description="Ending line number")
    column_start: int | None = Field(None, ge=0, description="Starting column")
    column_end: int | None = Field(None, ge=0, description="Ending column")


class CoreCodeEntity(BaseModel):
    """A code entity (function, class, module, etc.) in the graph."""

    model_config = ConfigDict(extra="forbid")

    entity_id: str = Field(..., description="Unique entity identifier")
    name: str = Field(..., description="Entity name")
    entity_type: EntityType = Field(..., description="Type of code entity")
    qualified_name: str | None = Field(None, description="Fully qualified name")
    location: SourceLocation | None = Field(None, description="Source location")
    language: str | None = Field(None, description="Programming language")
    docstring: str | None = Field(None, description="Documentation string")
    complexity: int | None = Field(None, ge=0, description="Cyclomatic complexity")
    properties: dict[str, Any] = Field(default_factory=dict, description="Additional properties")


class EntityRelation(BaseModel):
    """A relationship between two code entities."""

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(..., description="Source entity ID")
    target_id: str = Field(..., description="Target entity ID")
    relation_type: RelationType = Field(..., description="Relationship type")
    properties: dict[str, Any] = Field(default_factory=dict, description="Relationship properties")
    weight: float = Field(default=1.0, ge=0.0, description="Relationship weight")


class OverallGraphMetrics(BaseModel):
    """Summary metrics for a code graph."""

    node_count: int = Field(default=0, ge=0)
    edge_count: int = Field(default=0, ge=0)
    node_types: dict[str, int] = Field(default_factory=dict)
    relationship_types: dict[str, int] = Field(default_factory=dict)
    average_degree: float = Field(default=0.0)
    density: float = Field(default=0.0)
    connected_components: int = Field(default=0, ge=0)
    calculation_time_seconds: float = Field(default=0.0)


class CoreAnalysisResult(BaseModel):
    """Result of a code analysis run."""

    analysis_id: str = Field(..., description="Unique analysis identifier")
    status: str = Field(..., description="Analysis status")
    entities: list[CoreCodeEntity] = Field(default_factory=list)
    relations: list[EntityRelation] = Field(default_factory=list)
    metrics: OverallGraphMetrics | None = Field(None)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    processing_time_ms: float | None = Field(None)


# =====================================================================
#  Code-graph request / response models
#  NOTE: Graph CRUD request/response models (InitResponse, CodebaseInitRequest,
#  GraphListResponse, GraphDetailResponse, GraphDeletionResponse) and related
#  query/search models are defined directly in routers/graphs.py where they are
#  consumed.  They were removed from this file to eliminate divergent duplicates.
# =====================================================================


# =====================================================================
#  Validation models (multi-language)
# =====================================================================


class ValidationRequest(BaseModel):
    """Base validation request."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    code: str = Field(..., description="Code to validate")

    @field_validator("code")
    @classmethod
    def validate_code_not_none(cls, v: str) -> str:
        if v is None:
            raise ValueError("Code field is required")
        return v


class JavaScriptValidationRequest(ValidationRequest):
    filename: str | None = Field(default="test.js")


class PythonValidationRequest(ValidationRequest):
    filename: str | None = Field(default="test.py")


class JavaValidationRequest(ValidationRequest):
    className: str | None = Field(default="Test")  # noqa: N815


class RustValidationRequest(ValidationRequest):
    crateType: Literal["bin", "lib"] | None = Field(default="bin")  # noqa: N815


class CSharpValidationRequest(ValidationRequest):
    projectType: Literal["console", "library"] | None = Field(default="console")  # noqa: N815
    targetFramework: str | None = Field(default="net8.0")  # noqa: N815


class GoValidationRequest(ValidationRequest):
    filename: str | None = Field(default="main.go")


class ValidationResponse(BaseModel):
    """Validation response."""

    model_config = ConfigDict(extra="forbid")

    validationId: str = Field(...)  # noqa: N815
    valid: bool = Field(...)
    errors: list[str] = Field(default_factory=list)
    output: str = Field(default="")
    warnings: list[str] | None = Field(default=None)


class CodeValidationErrorResponse(BaseModel):
    """Validation error response."""

    model_config = ConfigDict(extra="forbid")

    validationId: str | None = Field(default=None)  # noqa: N815
    valid: bool = Field(default=False)
    error: str = Field(...)
    details: str | None = Field(default=None)


# =====================================================================
#  Cache models
# =====================================================================


class CacheStatsResponse(BaseModel):
    """Cache statistics."""

    total_keys: int = Field(default=0, ge=0)
    memory_used_bytes: int = Field(default=0, ge=0)
    hits: int = Field(default=0, ge=0)
    misses: int = Field(default=0, ge=0)
    hit_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    evictions: int = Field(default=0, ge=0)
    expired_keys: int = Field(default=0, ge=0)

    @field_validator("hit_ratio", mode="before")
    @classmethod
    def compute_hit_ratio(cls, v: float, info: Any) -> float:
        if v != 0.0:
            return v
        data = info.data if hasattr(info, "data") else {}
        hits = data.get("hits", 0)
        misses = data.get("misses", 0)
        total = hits + misses
        return round(hits / total, 4) if total > 0 else 0.0


class CacheClearResponse(BaseModel):
    """Cache clear operation result."""

    cleared_keys: int = Field(default=0)
    message: str = Field(default="Cache cleared successfully")


class CacheKeysResponse(BaseModel):
    """Cache keys listing."""

    keys: list[str] = Field(default_factory=list)
    total: int = Field(default=0)
    pattern: str | None = Field(default=None)


class CachePreloadOperation(BaseModel):
    """A single cache preload operation."""

    key: str
    ttl: int | None = None


class CachePreloadConfig(BaseModel):
    """Cache preload configuration."""

    operations: list[CachePreloadOperation] = Field(default_factory=list)


class CachePreloadConfigResponse(BaseModel):
    """Cache preload config response."""

    status: str = "ok"
    preloaded: int = 0
    failed: int = 0


class CacheEvictionReason(StrEnum):
    """Reasons for cache eviction."""

    TTL_EXPIRED = "ttl_expired"
    MAX_MEMORY = "max_memory"
    MANUAL = "manual"
    LRU = "lru"


class CacheEvictionEntry(BaseModel):
    """A cache eviction event."""

    key: str
    reason: CacheEvictionReason
    evicted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    size_bytes: int | None = None


class CacheEvictionHistoryResponse(BaseModel):
    """Cache eviction history."""

    entries: list[CacheEvictionEntry] = Field(default_factory=list)
    total: int = 0
