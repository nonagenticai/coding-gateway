"""Module for consistency rules engine and validation strategies."""

from __future__ import annotations

import ast
import logging
import re
from typing import Any

from services.code_graph.consistency_rules import (
    ConsistencyRule,
    ExceptionConsistencyRule,
    LogicOperationConsistencyRule,
    NamingConsistencyRule,
    ParameterConsistencyRule,
    ReturnConsistencyRule,
)

logger = logging.getLogger(__name__)


class ConsistencyRulesEngine:
    """Engine for managing and executing consistency rules."""

    def __init__(self):
        """Initialize the rules engine with default rules."""
        self.rules: list[ConsistencyRule] = [
            ParameterConsistencyRule(),
            ReturnConsistencyRule(),
            ExceptionConsistencyRule(),
            LogicOperationConsistencyRule(),
            NamingConsistencyRule(),
        ]
        self.rule_weights = {"high": 1.0, "medium": 0.7, "low": 0.3}

    def add_rule(self, rule: ConsistencyRule) -> None:
        """Add a custom consistency rule."""
        self.rules.append(rule)

    def remove_rule(self, rule_name: str) -> bool:
        """Remove a rule by name."""
        original_length = len(self.rules)
        self.rules = [rule for rule in self.rules if rule.get_rule_name() != rule_name]
        return len(self.rules) < original_length

    def check_consistency(
        self, code: str, description: str, context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """
        Run all consistency rules against code and description.

        Args:
            code: Source code to check
            description: Description to validate against
            context: Additional context for validation

        Returns:
            Dict containing all consistency check results
        """
        if context is None:
            context = {}

        all_issues = []
        rule_results = {}

        for rule in self.rules:
            try:
                issues = rule.check(code, description, context)
                rule_results[rule.get_rule_name()] = {
                    "issues": issues,
                    "passed": len(issues) == 0,
                    "severity": rule.get_severity(),
                }
                all_issues.extend(issues)
            except Exception as e:
                logger.error(f"Rule {rule.get_rule_name()} failed: {e}")
                rule_results[rule.get_rule_name()] = {
                    "issues": [
                        {
                            "type": "rule_error",
                            "rule": rule.get_rule_name(),
                            "description": f"Rule execution failed: {str(e)}",
                            "severity": "medium",
                        }
                    ],
                    "passed": False,
                    "severity": rule.get_severity(),
                    "error": str(e),
                }

        # Calculate overall consistency score
        consistency_score = self._calculate_consistency_score(all_issues)

        return {
            "consistency_score": consistency_score,
            "is_consistent": consistency_score >= 0.7,
            "total_issues": len(all_issues),
            "issues_by_severity": self._group_issues_by_severity(all_issues),
            "all_issues": all_issues,
            "rule_results": rule_results,
            "rules_passed": sum(1 for result in rule_results.values() if result["passed"]),
            "total_rules": len(self.rules),
        }

    def _calculate_consistency_score(self, issues: list[dict[str, Any]]) -> float:
        """Calculate overall consistency score based on issues."""
        if not issues:
            return 1.0

        total_penalty = 0.0
        for issue in issues:
            severity = issue.get("severity", "medium")
            weight = self.rule_weights.get(severity, 0.5)
            total_penalty += weight * 0.1  # Each issue reduces score by 10% of its weight

        score = 1.0 - min(total_penalty, 1.0)
        return max(0.0, score)

    def _group_issues_by_severity(self, issues: list[dict[str, Any]]) -> dict[str, int]:
        """Group issues by severity level."""
        severity_counts = {"high": 0, "medium": 0, "low": 0}

        for issue in issues:
            severity = issue.get("severity", "medium")
            if severity in severity_counts:
                severity_counts[severity] += 1

        return severity_counts

    def get_enabled_rules(self) -> list[str]:
        """Get list of enabled rule names."""
        return [rule.get_rule_name() for rule in self.rules]

    def enable_rule(self, rule_name: str) -> bool:
        """Enable a rule that was previously disabled."""
        # For now, all rules are always enabled
        # This method could be extended to support rule toggling
        return rule_name in self.get_enabled_rules()

    def disable_rule(self, rule_name: str) -> bool:
        """Disable a specific rule."""
        return self.remove_rule(rule_name)

    def get_rule_info(self, rule_name: str) -> dict[str, Any]:
        """Get information about a specific rule."""
        for rule in self.rules:
            if rule.get_rule_name() == rule_name:
                return {
                    "name": rule.get_rule_name(),
                    "severity": rule.get_severity(),
                    "description": rule.__doc__ or "No description available",
                    "class": rule.__class__.__name__,
                }
        return {}


class ModuleConsistencyChecker:
    """Specialized checker for module-level consistency."""

    def __init__(self):
        """Initialize module consistency checker."""
        self.naming_patterns = {
            "snake_case": re.compile(r"^[a-z][a-z0-9_]*$"),
            "camel_case": re.compile(r"^[a-z][a-zA-Z0-9]*$"),
            "pascal_case": re.compile(r"^[A-Z][a-zA-Z0-9]*$"),
        }

    def check_naming_conventions(
        self, parsed_modules: dict[str, ast.Module]
    ) -> list[dict[str, Any]]:
        """Check naming conventions across modules."""
        issues = []

        # Track naming styles
        function_styles = {"snake_case": set(), "camel_case": set(), "mixed": set()}
        class_styles = {"pascal_case": set(), "other": set()}

        for module_name, tree in parsed_modules.items():
            # Analyze function naming
            snake_count = camel_count = 0

            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
                    if self.naming_patterns["snake_case"].match(node.name):
                        snake_count += 1
                    elif self.naming_patterns["camel_case"].match(node.name):
                        camel_count += 1

            # Categorize module's function naming style
            if snake_count > 0 and camel_count > 0:
                function_styles["mixed"].add(module_name)
            elif snake_count > 0:
                function_styles["snake_case"].add(module_name)
            elif camel_count > 0:
                function_styles["camel_case"].add(module_name)

            # Analyze class naming
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    if self.naming_patterns["pascal_case"].match(node.name):
                        class_styles["pascal_case"].add(module_name)
                    else:
                        class_styles["other"].add(module_name)

        # Generate issues for inconsistencies
        if function_styles["mixed"]:
            issues.append(
                {
                    "type": "naming",
                    "description": "Inconsistent function naming within modules",
                    "severity": "medium",
                    "modules": list(function_styles["mixed"]),
                }
            )

        if function_styles["snake_case"] and function_styles["camel_case"]:
            issues.append(
                {
                    "type": "naming",
                    "description": "Inconsistent function naming across modules",
                    "severity": "medium",
                    "modules": list(function_styles["snake_case"] | function_styles["camel_case"]),
                }
            )

        if class_styles["other"]:
            issues.append(
                {
                    "type": "naming",
                    "description": "Non-PascalCase class names found",
                    "severity": "medium",
                    "modules": list(class_styles["other"]),
                }
            )

        return issues

    def check_import_patterns(self, parsed_modules: dict[str, ast.Module]) -> list[dict[str, Any]]:
        """Check for consistent import patterns."""
        issues = []

        # Track import styles per module
        import_styles = {}

        for module_name, tree in parsed_modules.items():
            absolute_imports = relative_imports = mixed_imports = 0

            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    if node.level > 0:  # Relative import
                        relative_imports += 1
                    else:  # Absolute import
                        absolute_imports += 1
                elif isinstance(node, ast.Import):
                    absolute_imports += 1

            if relative_imports > 0 and absolute_imports > 0:
                mixed_imports += 1

            import_styles[module_name] = {
                "absolute": absolute_imports,
                "relative": relative_imports,
                "mixed": mixed_imports > 0,
            }

        # Check for mixed import styles within modules
        mixed_modules = [name for name, style in import_styles.items() if style["mixed"]]
        if mixed_modules:
            issues.append(
                {
                    "type": "import_style",
                    "description": "Mixed import styles within modules",
                    "severity": "low",
                    "modules": mixed_modules,
                }
            )

        return issues
