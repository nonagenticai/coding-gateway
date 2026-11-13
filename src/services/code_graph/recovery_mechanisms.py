"""Module for error recovery and suggestion generation mechanisms."""

from __future__ import annotations

import ast
import logging
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


class SuggestionStrategy(ABC):
    """Abstract base class for suggestion generation strategies."""

    @abstractmethod
    def generate(self, issues: list[dict], context: dict[str, Any]) -> list[str]:
        """Generate suggestions for a specific type of issue."""

    @abstractmethod
    def get_strategy_name(self) -> str:
        """Get the name of this strategy."""

    @abstractmethod
    def get_applicable_types(self) -> list[str]:
        """Get the types of issues this strategy can handle."""


class ParameterSuggestionStrategy(SuggestionStrategy):
    """Strategy for generating parameter-related suggestions."""

    def generate(self, issues: list[dict], context: dict[str, Any]) -> list[str]:
        """Generate parameter-related suggestions."""
        suggestions = []

        # Group issues by parameter names
        missing_params = []
        for issue in issues:
            if (
                issue.get("type") == "parameter"
                and "parameter '" in issue.get("description", "").lower()
            ):
                try:
                    param_name = issue["description"].split("'")[1]
                    missing_params.append(param_name)
                except (IndexError, KeyError):  # fmt: skip
                    # Extract parameter name from context if available
                    if issue.get("parameter_name"):
                        missing_params.append(issue["parameter_name"])

        # Remove duplicates while preserving order
        seen = set()
        unique_params = []
        for param in missing_params:
            if param not in seen:
                seen.add(param)
                unique_params.append(param)

        if len(unique_params) == 1:
            suggestions.append(f"Add explanation for parameter '{unique_params[0]}' in description")
        elif len(unique_params) > 1:
            param_list = ", ".join([f"'{param}'" for param in unique_params])
            suggestions.append(f"Add explanations for parameters {param_list} in description")

        # Check for parameter type information
        function_node = context.get("function_node")
        if function_node and isinstance(function_node, ast.FunctionDef):
            typed_params = []
            for arg in function_node.args.args:
                if arg.annotation and arg.arg in unique_params:
                    typed_params.append(arg.arg)

            if typed_params:
                suggestions.append("Include parameter type information in the description")

        return suggestions

    def get_strategy_name(self) -> str:
        """Get strategy name.

        Returns:
            str
        """
        return "parameter_suggestion"

    def get_applicable_types(self) -> list[str]:
        """Get applicable types.

        Returns:
            List[str]
        """
        return ["parameter", "missing_parameters"]


class ReturnSuggestionStrategy(SuggestionStrategy):
    """Strategy for generating return-related suggestions."""

    def generate(self, issues: list[dict], context: dict[str, Any]) -> list[str]:
        """Generate return-related suggestions."""
        suggestions = []

        has_missing_return = any(
            "not mentioned" in issue.get("description", "")
            or "returns values" in issue.get("description", "")
            for issue in issues
            if issue.get("type") == "return"
        )

        if has_missing_return:
            suggestions.append("Include information about return values in the description")

        has_missing_type = any(
            "return type" in issue.get("description", "")
            for issue in issues
            if issue.get("type") == "return"
        )

        if has_missing_type:
            suggestions.append("Specify the return type in the description")

        # Check for specific return types mentioned in issues
        return_types = []
        for issue in issues:
            if issue.get("type") == "return" and issue.get("return_type"):
                return_types.append(issue["return_type"])

        if return_types:
            unique_types = list(set(return_types))
            if len(unique_types) == 1:
                suggestions.append(f"Mention that this function returns {unique_types[0]}")
            else:
                suggestions.append(f"Specify the return types: {', '.join(unique_types)}")

        return suggestions

    def get_strategy_name(self) -> str:
        """Get strategy name.

        Returns:
            str
        """
        return "return_suggestion"

    def get_applicable_types(self) -> list[str]:
        """Get applicable types.

        Returns:
            List[str]
        """
        return ["return", "missing_return_info"]


class ExceptionSuggestionStrategy(SuggestionStrategy):
    """Strategy for generating exception-related suggestions."""

    def generate(self, issues: list[dict], context: dict[str, Any]) -> list[str]:
        """Generate exception-related suggestions."""
        suggestions = []

        has_missing_exception = any(
            "not mentioned" in issue.get("description", "")
            or "handles exceptions" in issue.get("description", "")
            for issue in issues
            if issue.get("type") == "exception"
        )

        if has_missing_exception:
            suggestions.append("Describe exception handling behavior in the description")

        # Collect specific exception types
        specific_exceptions = []
        for issue in issues:
            if issue.get("type") == "exception" and issue.get("exception_type"):
                specific_exceptions.append(issue["exception_type"])
            elif "handles '" in issue.get("description", ""):
                try:
                    exc_type = issue["description"].split("'")[1]
                    specific_exceptions.append(exc_type)
                except (IndexError, KeyError):  # fmt: skip
                    logger.debug(
                        "Could not extract exception type from: %s", issue.get("description")
                    )

        if specific_exceptions:
            unique_exceptions = list(set(specific_exceptions))
            exc_list = ", ".join(unique_exceptions)
            suggestions.append(f"Mention specific exceptions handled: {exc_list}")

        return suggestions

    def get_strategy_name(self) -> str:
        """Get strategy name.

        Returns:
            str
        """
        return "exception_suggestion"

    def get_applicable_types(self) -> list[str]:
        """Get applicable types.

        Returns:
            List[str]
        """
        return ["exception"]


class LogicSuggestionStrategy(SuggestionStrategy):
    """Strategy for generating logic-related suggestions."""

    def generate(self, issues: list[dict], context: dict[str, Any]) -> list[str]:
        """Generate logic-related suggestions."""
        suggestions = []

        operation_suggestions = {
            "database": "Include information about database operations in the description",
            "file": "Include information about file operations in the description",
            "network": "Include information about network operations in the description",
            "async": "Mention that the function operates asynchronously",
            "recursion": "Describe the recursive nature of the function",
        }

        # Check for operation types mentioned in issues
        operation_types = set()
        for issue in issues:
            if issue.get("type") == "logic":
                operation_type = issue.get("operation_type")
                if operation_type in operation_suggestions:
                    operation_types.add(operation_type)
                else:
                    # Try to extract operation type from description
                    desc = issue.get("description", "").lower()
                    for op_type in operation_suggestions:
                        if op_type in desc:
                            operation_types.add(op_type)
                            break

        for operation_type in operation_types:
            suggestions.append(operation_suggestions[operation_type])

        return suggestions

    def get_strategy_name(self) -> str:
        """Get strategy name.

        Returns:
            str
        """
        return "logic_suggestion"

    def get_applicable_types(self) -> list[str]:
        """Get applicable types.

        Returns:
            List[str]
        """
        return ["logic"]


class StructuralSuggestionStrategy(SuggestionStrategy):
    """Strategy for generating structural improvement suggestions."""

    def generate(self, issues: list[dict], context: dict[str, Any]) -> list[str]:
        """Generate structural improvement suggestions."""
        suggestions = []

        issue_types = [issue.get("category", "") for issue in issues]

        if "empty_function" in issue_types:
            suggestions.append("Add implementation to empty function bodies")

        if "empty_class" in issue_types:
            suggestions.append("Add methods and attributes to empty class definitions")

        if "unreachable_code" in issue_types:
            suggestions.append("Remove or restructure unreachable code after return statements")

        if "infinite_loop" in issue_types:
            suggestions.append(
                "Add break conditions or modify loop logic to prevent infinite loops"
            )

        if "unused_variable" in issue_types:
            suggestions.append("Remove unused variables or use them in the code logic")

        return suggestions

    def get_strategy_name(self) -> str:
        """Get strategy name.

        Returns:
            str
        """
        return "structural_suggestion"

    def get_applicable_types(self) -> list[str]:
        """Get applicable types.

        Returns:
            List[str]
        """
        return ["structural", "logic"]


class NamingSuggestionStrategy(SuggestionStrategy):
    """Strategy for generating naming-related suggestions."""

    def generate(self, issues: list[dict], context: dict[str, Any]) -> list[str]:
        """Generate naming-related suggestions."""
        suggestions = []

        missing_names = {"functions": [], "classes": []}

        for issue in issues:
            if issue.get("type") == "naming":
                entity_type = issue.get("entity_type")
                entity_name = issue.get("entity_name")

                if entity_type == "function" and entity_name:
                    missing_names["functions"].append(entity_name)
                elif entity_type == "class" and entity_name:
                    missing_names["classes"].append(entity_name)

        if missing_names["functions"]:
            if len(missing_names["functions"]) == 1:
                suggestions.append(
                    f"Mention the function name '{missing_names['functions'][0]}' in the description"
                )
            else:
                func_list = ", ".join([f"'{name}'" for name in missing_names["functions"]])
                suggestions.append(f"Mention the function names {func_list} in the description")

        if missing_names["classes"]:
            if len(missing_names["classes"]) == 1:
                suggestions.append(
                    f"Mention the class name '{missing_names['classes'][0]}' in the description"
                )
            else:
                class_list = ", ".join([f"'{name}'" for name in missing_names["classes"]])
                suggestions.append(f"Mention the class names {class_list} in the description")

        return suggestions

    def get_strategy_name(self) -> str:
        """Get strategy name.

        Returns:
            str
        """
        return "naming_suggestion"

    def get_applicable_types(self) -> list[str]:
        """Get applicable types.

        Returns:
            List[str]
        """
        return ["naming", "missing_function_name", "missing_class_name"]


class DefaultSuggestionStrategy(SuggestionStrategy):
    """Default strategy for handling unknown issue types."""

    def generate(self, issues: list[dict], context: dict[str, Any]) -> list[str]:
        """Generate default suggestions for unknown issue types."""
        suggestions = []

        if not issues:
            suggestions.append(
                "No specific issues found. The code and description appear to be well-aligned."
            )
        else:
            # Group by severity
            critical_issues = [i for i in issues if i.get("severity") == "critical"]
            high_issues = [i for i in issues if i.get("severity") == "high"]

            if critical_issues:
                suggestions.append("Address critical syntax and parsing errors first")
            elif high_issues:
                suggestions.append("Focus on resolving high-priority consistency issues")
            else:
                suggestions.append(
                    "Review and update the description to better match the implementation"
                )

        return suggestions

    def get_strategy_name(self) -> str:
        """Get strategy name.

        Returns:
            str
        """
        return "default_suggestion"

    def get_applicable_types(self) -> list[str]:
        """Get applicable types.

        Returns:
            List[str]
        """
        return ["unknown", "default"]


class RecoveryMechanisms:
    """
    Main class for error recovery and suggestion generation.

    Coordinates different suggestion strategies and provides
    comprehensive recovery recommendations.
    """

    def __init__(self):
        """Initialize recovery mechanisms with available strategies."""
        self.strategies: list[SuggestionStrategy] = [
            ParameterSuggestionStrategy(),
            ReturnSuggestionStrategy(),
            ExceptionSuggestionStrategy(),
            LogicSuggestionStrategy(),
            StructuralSuggestionStrategy(),
            NamingSuggestionStrategy(),
            DefaultSuggestionStrategy(),
        ]
        self.strategy_map: dict[str, SuggestionStrategy] = {
            strategy.get_strategy_name(): strategy for strategy in self.strategies
        }

    def generate_suggestions(
        self, issues: list[dict], context: dict[str, Any] | None = None
    ) -> list[str]:
        """
        Generate comprehensive suggestions for fixing issues.

        Args:
            issues: List of detected issues
            context: Additional context for suggestion generation

        Returns:
            List of actionable suggestions
        """
        if context is None:
            context = {}

        # Validate input
        if not self._validate_input(issues, context):
            return self._handle_invalid_input(issues=issues, context=context)

        all_suggestions = []

        # Group issues by type
        grouped_issues = self._group_issues_by_type(issues)

        # Generate suggestions using appropriate strategies
        for issue_type, issue_list in grouped_issues.items():
            applicable_strategies = self._find_applicable_strategies(issue_type)

            for strategy in applicable_strategies:
                try:
                    suggestions = strategy.generate(issue_list, context)
                    all_suggestions.extend(suggestions)
                except Exception as e:
                    logger.warning(f"Strategy {strategy.get_strategy_name()} failed: {e}")

        # If no specific suggestions were generated, use default
        if not all_suggestions and issues:
            default_strategy = self._get_default_strategy()
            all_suggestions = default_strategy.generate(issues, context)

        # Remove duplicates and prioritize
        return self._prioritize_suggestions(all_suggestions, issues)

    def generate_recovery_plan(
        self, issues: list[dict], context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """
        Generate a comprehensive recovery plan for fixing issues.

        Args:
            issues: List of detected issues
            context: Additional context

        Returns:
            Structured recovery plan with priorities and steps
        """
        if context is None:
            context = {}

        suggestions = self.generate_suggestions(issues, context)

        # Categorize issues by severity and type
        severity_groups = self._group_issues_by_severity(issues)
        type_groups = self._group_issues_by_type(issues)

        # Create prioritized action plan
        action_plan: list[dict[str, Any]] = []

        # Critical issues first
        if severity_groups.get("critical"):
            action_plan.append(
                {
                    "priority": "immediate",
                    "phase": "Critical Error Resolution",
                    "issues_count": len(severity_groups["critical"]),
                    "description": "Address syntax errors and critical parsing issues",
                    "actions": [
                        s
                        for s in suggestions
                        if "syntax" in s.lower()
                        or "critical" in s.lower()
                        or "parsing" in s.lower()
                    ][:3],
                }
            )

        # High priority issues
        if severity_groups.get("high"):
            action_plan.append(
                {
                    "priority": "high",
                    "phase": "Consistency Improvement",
                    "issues_count": len(severity_groups["high"]),
                    "description": "Fix major consistency and structural issues",
                    "actions": [
                        s
                        for s in suggestions
                        if "consistency" in s.lower() or "structural" in s.lower()
                    ][:4],
                }
            )

        # Medium priority issues
        if severity_groups.get("medium"):
            action_plan.append(
                {
                    "priority": "medium",
                    "phase": "Documentation Enhancement",
                    "issues_count": len(severity_groups["medium"]),
                    "description": "Improve documentation and descriptions",
                    "actions": [
                        s
                        for s in suggestions
                        if "description" in s.lower() or "mention" in s.lower()
                    ][:3],
                }
            )

        # Low priority issues
        if severity_groups.get("low"):
            action_plan.append(
                {
                    "priority": "low",
                    "phase": "Polish and Refinement",
                    "issues_count": len(severity_groups["low"]),
                    "description": "Address minor style and naming issues",
                    "actions": [
                        s
                        for s in suggestions
                        if s
                        not in [
                            action for phase in action_plan for action in phase.get("actions", [])
                        ]
                    ][:2],
                }
            )

        return {
            "total_issues": len(issues),
            "total_suggestions": len(suggestions),
            "estimated_effort": self._estimate_effort(issues),
            "action_plan": action_plan,
            "all_suggestions": suggestions,
            "issue_breakdown": {
                "by_severity": {k: len(v) for k, v in severity_groups.items()},
                "by_type": {k: len(v) for k, v in type_groups.items()},
            },
        }

    def _validate_input(self, issues: list[dict], context: dict[str, Any]) -> bool:
        """Validate input parameters."""
        if not isinstance(issues, list):
            return False

        if not isinstance(context, dict):
            return False

        # Check each issue has required structure
        for issue in issues:
            if not isinstance(issue, dict):
                return False
            if "type" not in issue or "description" not in issue:
                return False

        return True

    def _handle_invalid_input(self, issues=None, context=None) -> list[str]:
        """Handle invalid input scenarios."""
        logger.warning("Invalid input provided to recovery mechanisms")

        if not issues:
            return ["No issues provided for recovery analysis"]

        if not isinstance(issues, list):
            return ["Invalid issues format provided"]

        return ["Unable to generate specific suggestions due to invalid input format"]

    def _group_issues_by_type(self, issues: list[dict]) -> dict[str, list[dict]]:
        """Group issues by their type."""
        grouped = {}
        for issue in issues:
            issue_type = issue.get("type", "unknown")
            if issue_type not in grouped:
                grouped[issue_type] = []
            grouped[issue_type].append(issue)
        return grouped

    def _group_issues_by_severity(self, issues: list[dict]) -> dict[str, list[dict]]:
        """Group issues by their severity."""
        grouped = {}
        for issue in issues:
            severity = issue.get("severity", "medium")
            if severity not in grouped:
                grouped[severity] = []
            grouped[severity].append(issue)
        return grouped

    def _find_applicable_strategies(self, issue_type: str) -> list[SuggestionStrategy]:
        """Find strategies that can handle the given issue type."""
        applicable = []
        for strategy in self.strategies:
            if issue_type in strategy.get_applicable_types():
                applicable.append(strategy)
        return applicable

    def _get_default_strategy(self) -> SuggestionStrategy:
        """Get the default strategy."""
        return self.strategy_map.get("default_suggestion", DefaultSuggestionStrategy())

    def _prioritize_suggestions(self, suggestions: list[str], issues: list[dict]) -> list[str]:
        """Prioritize and deduplicate suggestions."""
        # Remove duplicates while preserving order
        seen = set()
        prioritized = []
        for suggestion in suggestions:
            if suggestion not in seen:
                seen.add(suggestion)
                prioritized.append(suggestion)

        # Sort by priority (critical issues first)
        critical_keywords = ["syntax", "critical", "parsing", "error"]
        high_keywords = ["consistency", "structural", "major"]

        def get_priority(suggestion):
            suggestion_lower = suggestion.lower()
            if any(keyword in suggestion_lower for keyword in critical_keywords):
                return 0  # Highest priority
            elif any(keyword in suggestion_lower for keyword in high_keywords):
                return 1  # High priority
            else:
                return 2  # Normal priority

        prioritized.sort(key=get_priority)

        return prioritized

    def _estimate_effort(self, issues: list[dict]) -> str:
        """Estimate effort required to fix issues."""
        if not issues:
            return "minimal"

        critical_count = len([i for i in issues if i.get("severity") == "critical"])
        high_count = len([i for i in issues if i.get("severity") == "high"])
        total_count = len(issues)

        if critical_count > 0:
            return "high"
        elif high_count > 3 or total_count > 10:
            return "medium"
        elif total_count > 5:
            return "low"
        else:
            return "minimal"

    def add_custom_strategy(self, strategy: SuggestionStrategy) -> None:
        """Add a custom suggestion strategy."""
        self.strategies.append(strategy)
        self.strategy_map[strategy.get_strategy_name()] = strategy

    def remove_strategy(self, strategy_name: str) -> bool:
        """Remove a strategy by name."""
        if strategy_name in self.strategy_map:
            strategy = self.strategy_map[strategy_name]
            self.strategies.remove(strategy)
            del self.strategy_map[strategy_name]
            return True
        return False

    def get_available_strategies(self) -> list[str]:
        """Get list of available strategy names."""
        return list(self.strategy_map.keys())
