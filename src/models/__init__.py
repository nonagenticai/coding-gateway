"""Gateway models package.

Re-exports ORM models from .database and domain/API schemas from .schemas.
Graph CRUD models (InitResponse, GraphListResponse, etc.) live in
routers/graphs.py and are not re-exported here.
"""

from __future__ import annotations

from .database import Base, Edge, Node, NodeAudit, SchemaVersion
from .schemas import (
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

__all__ = [
    # Database ORM
    "Base",
    "SchemaVersion",
    "NodeAudit",
    "Node",
    "Edge",
    # Common / base response schemas
    "ResponseStatus",
    "BaseResponse",
    "SuccessResponse",
    "PaginationParams",
    "PaginatedResponse",
    "HealthStatus",
    "ComponentHealth",
    "StandardErrorResponse",
    "ValidationErrorResponse",
    # Core domain models
    "EntityType",
    "RelationType",
    "SourceLocation",
    "CoreCodeEntity",
    "EntityRelation",
    "OverallGraphMetrics",
    "CoreAnalysisResult",
    # Validation models
    "ValidationRequest",
    "JavaScriptValidationRequest",
    "PythonValidationRequest",
    "JavaValidationRequest",
    "RustValidationRequest",
    "CSharpValidationRequest",
    "GoValidationRequest",
    "ValidationResponse",
    "CodeValidationErrorResponse",
    # Cache models
    "CacheStatsResponse",
    "CacheClearResponse",
    "CacheKeysResponse",
    "CachePreloadOperation",
    "CachePreloadConfig",
    "CachePreloadConfigResponse",
    "CacheEvictionReason",
    "CacheEvictionEntry",
    "CacheEvictionHistoryResponse",
]
