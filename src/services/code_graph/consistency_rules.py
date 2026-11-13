"""Core consistency rules for code validation."""

from __future__ import annotations

import ast
import logging
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


class ConsistencyRule(ABC):
    """Abstract base class for consistency rules."""

    @abstractmethod
    def check(self, code: str, description: str, context: dict[str, Any]) -> list[dict[str, Any]]:
        """Check if the rule is satisfied."""

    @abstractmethod
    def get_rule_name(self) -> str:
        """Get the name of this rule."""

    @abstractmethod
    def get_severity(self) -> str:
        """Get the severity level of this rule."""


class ParameterConsistencyRule(ConsistencyRule):
    """Rule for checking parameter consistency between code and description."""

    def check(self, code: str, description: str, context: dict[str, Any]) -> list[dict[str, Any]]:
        """Check if parameters are mentioned in description."""
        issues = []

        try:
            tree = ast.parse(code)
            function_node = None

            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    function_node = node
                    break

            if not function_node:
                return issues

            # Extract parameter names (excluding 'self')
            param_names = [arg.arg for arg in function_node.args.args if arg.arg != "self"]

            if not param_names:
                return issues

            # Check if parameters are mentioned in description
            for param in param_names:
                if param not in description:
                    issues.append(
                        {
                            "type": "parameter",
                            "rule": self.get_rule_name(),
                            "description": f"Parameter '{param}' is not mentioned in the description",
                            "severity": self.get_severity(),
                            "parameter_name": param,
                        }
                    )

        except SyntaxError:
            issues.append(
                {
                    "type": "syntax",
                    "rule": self.get_rule_name(),
                    "description": "Cannot parse function code to check parameters",
                    "severity": "high",
                }
            )
        except Exception as e:
            logger.debug(f"Parameter consistency check failed: {e}")

        return issues

    def get_rule_name(self) -> str:
        """Get rule name.

        Returns:
            str
        """
        return "parameter_consistency"

    def get_severity(self) -> str:
        """Get severity.

        Returns:
            str
        """
        return "medium"


class ReturnConsistencyRule(ConsistencyRule):
    """Rule for checking return value consistency."""

    def check(self, code: str, description: str, context: dict[str, Any]) -> list[dict[str, Any]]:
        """Check if return values are mentioned in description."""
        issues = []

        try:
            tree = ast.parse(code)
            function_node = None

            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    function_node = node
                    break

            if not function_node:
                return issues

            # Look for return statements
            return_nodes = []
            for node in ast.walk(function_node):
                if isinstance(node, ast.Return) and node.value is not None:
                    return_nodes.append(node)

            # If there are return statements but no mention in description
            if return_nodes and "return" not in description.lower():
                issues.append(
                    {
                        "type": "return",
                        "rule": self.get_rule_name(),
                        "description": "Function returns values but this is not mentioned in description",
                        "severity": self.get_severity(),
                    }
                )

            # Check return type annotation
            if function_node.returns:
                return_type = ast.unparse(function_node.returns)
                if return_type not in description:
                    issues.append(
                        {
                            "type": "return",
                            "rule": self.get_rule_name(),
                            "description": f"Return type '{return_type}' is not mentioned in description",
                            "severity": "low",
                            "return_type": return_type,
                        }
                    )

        except Exception as e:
            logger.debug(f"Return consistency check failed: {e}")

        return issues

    def get_rule_name(self) -> str:
        """Get rule name.

        Returns:
            str
        """
        return "return_consistency"

    def get_severity(self) -> str:
        """Get severity.

        Returns:
            str
        """
        return "medium"


class ExceptionConsistencyRule(ConsistencyRule):
    """Rule for checking exception handling consistency."""

    def check(self, code: str, description: str, context: dict[str, Any]) -> list[dict[str, Any]]:
        """Check if exception handling is mentioned in description."""
        issues = []

        try:
            tree = ast.parse(code)
            function_node = None

            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    function_node = node
                    break

            if not function_node:
                return issues

            # Look for try-except blocks
            try_except_blocks = []
            for node in ast.walk(function_node):
                if isinstance(node, ast.Try):
                    try_except_blocks.append(node)

            if try_except_blocks and not any(
                word in description.lower()
                for word in ["exception", "error", "catch", "handle", "try", "except"]
            ):
                issues.append(
                    {
                        "type": "exception",
                        "rule": self.get_rule_name(),
                        "description": "Function handles exceptions but this is not mentioned in description",
                        "severity": self.get_severity(),
                    }
                )

            # Check specific exception types
            exception_types = []
            for try_block in try_except_blocks:
                for handler in try_block.handlers:
                    if handler.type:
                        if isinstance(handler.type, ast.Name):
                            exception_types.append(handler.type.id)
                        elif isinstance(handler.type, ast.Attribute):
                            exception_types.append(handler.type.attr)

            for exc_type in exception_types:
                if exc_type not in description:
                    issues.append(
                        {
                            "type": "exception",
                            "rule": self.get_rule_name(),
                            "description": f"Function handles '{exc_type}' exceptions but this is not mentioned in description",
                            "severity": "low",
                            "exception_type": exc_type,
                        }
                    )

        except Exception as e:
            logger.debug(f"Exception consistency check failed: {e}")

        return issues

    def get_rule_name(self) -> str:
        """Get rule name.

        Returns:
            str
        """
        return "exception_consistency"

    def get_severity(self) -> str:
        """Get severity.

        Returns:
            str
        """
        return "low"


class LogicOperationConsistencyRule(ConsistencyRule):
    """Rule for checking if key operations are mentioned in description."""

    def check(self, code: str, description: str, context: dict[str, Any]) -> list[dict[str, Any]]:
        """Check if key operations are mentioned in description."""
        issues = []

        try:
            tree = ast.parse(code)
            function_node = None

            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    function_node = node
                    break

            if not function_node:
                return issues

            function_source = ast.unparse(function_node)
            description_lower = description.lower()

            # Define operation indicators and their description keywords
            operations = {
                "database": {
                    "indicators": [
                        "execute",
                        "query",
                        "cursor",
                        "connection",
                        "commit",
                        "rollback",
                        "transaction",
                    ],
                    "description_keywords": [
                        "database",
                        "db",
                        "sql",
                        "query",
                        "persist",
                    ],
                },
                "file": {
                    "indicators": [
                        "open",
                        "read",
                        "write",
                        "file",
                        "save",
                        "load",
                        "path",
                        "json",
                        "csv",
                    ],
                    "description_keywords": ["file", "save", "load", "read", "write"],
                },
                "network": {
                    "indicators": [
                        "request",
                        "response",
                        "http",
                        "get",
                        "post",
                        "url",
                        "endpoint",
                        "api",
                    ],
                    "description_keywords": [
                        "network",
                        "http",
                        "request",
                        "api",
                        "endpoint",
                    ],
                },
                "async": {
                    "indicators": ["async", "await"],
                    "description_keywords": [
                        "async",
                        "asynchronous",
                        "await",
                        "concurrent",
                    ],
                },
            }

            # Check each operation type
            for operation_type, config in operations.items():
                has_operation = any(
                    indicator in function_source.lower() for indicator in config["indicators"]
                )

                has_description = any(
                    keyword in description_lower for keyword in config["description_keywords"]
                )

                if has_operation and not has_description:
                    issues.append(
                        {
                            "type": "logic",
                            "rule": self.get_rule_name(),
                            "description": f"Function performs {operation_type} operations but this is not mentioned in description",
                            "severity": self.get_severity(),
                            "operation_type": operation_type,
                        }
                    )

            # Check for recursion
            if (
                function_node.name
                in function_source[
                    function_source.find(function_node.name) + len(function_node.name) :
                ]
            ):
                if not any(
                    keyword in description_lower
                    for keyword in ["recursion", "recursive", "calls itself"]
                ):
                    issues.append(
                        {
                            "type": "logic",
                            "rule": self.get_rule_name(),
                            "description": "Function uses recursion but this is not mentioned in description",
                            "severity": self.get_severity(),
                            "operation_type": "recursion",
                        }
                    )

        except Exception as e:
            logger.debug(f"Logic operation consistency check failed: {e}")

        return issues

    def get_rule_name(self) -> str:
        """Get rule name.

        Returns:
            str
        """
        return "logic_operation_consistency"

    def get_severity(self) -> str:
        """Get severity.

        Returns:
            str
        """
        return "medium"


class NamingConsistencyRule(ConsistencyRule):
    """Rule for checking naming consistency."""

    def check(self, code: str, description: str, context: dict[str, Any]) -> list[dict[str, Any]]:
        """Check if function/class names are mentioned in description."""
        issues = []

        try:
            tree = ast.parse(code)

            # Check function names
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    if node.name not in description:
                        issues.append(
                            {
                                "type": "naming",
                                "rule": self.get_rule_name(),
                                "description": f"Function name '{node.name}' not mentioned in description",
                                "severity": self.get_severity(),
                                "entity_name": node.name,
                                "entity_type": "function",
                            }
                        )

                elif isinstance(node, ast.ClassDef):
                    if node.name not in description:
                        issues.append(
                            {
                                "type": "naming",
                                "rule": self.get_rule_name(),
                                "description": f"Class name '{node.name}' not mentioned in description",
                                "severity": self.get_severity(),
                                "entity_name": node.name,
                                "entity_type": "class",
                            }
                        )

        except Exception as e:
            logger.debug(f"Naming consistency check failed: {e}")

        return issues

    def get_rule_name(self) -> str:
        """Get rule name.

        Returns:
            str
        """
        return "naming_consistency"

    def get_severity(self) -> str:
        """Get severity.

        Returns:
            str
        """
        return "low"
