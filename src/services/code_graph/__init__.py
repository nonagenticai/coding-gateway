"""Code graph services for consistency checking, validation, and structure analysis."""

from __future__ import annotations

from services.code_graph.consistency_checker import CodeConsistencyChecker
from services.code_graph.consistency_rules import ConsistencyRule
from services.code_graph.consistency_rules_engine import (
    ConsistencyRulesEngine,
    ModuleConsistencyChecker,
)
from services.code_graph.error_detection_reporting import ErrorDetector, ErrorReporter
from services.code_graph.graph_validation import GraphValidator
from services.code_graph.recovery_mechanisms import RecoveryMechanisms
from services.code_graph.reference_validator import (
    PackageReferenceValidator,
    ReferenceValidator,
)
from services.code_graph.validation_utilities import (
    ASTUtils,
    GraphUtils,
    HashUtils,
    TextAnalysisUtils,
    ValidationCache,
    ValidationScoreCalculator,
    validation_cache,
)

__all__ = [
    # Main checker
    "CodeConsistencyChecker",
    # Rules
    "ConsistencyRule",
    "ConsistencyRulesEngine",
    "ModuleConsistencyChecker",
    # Error detection
    "ErrorDetector",
    "ErrorReporter",
    # Graph
    "GraphValidator",
    # Recovery
    "RecoveryMechanisms",
    # Reference validation
    "ReferenceValidator",
    "PackageReferenceValidator",
    # Utilities
    "ASTUtils",
    "GraphUtils",
    "HashUtils",
    "TextAnalysisUtils",
    "ValidationCache",
    "ValidationScoreCalculator",
    "validation_cache",
]
