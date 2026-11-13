"""Module for error detection and reporting in consistency checks."""

from __future__ import annotations

import ast
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class ErrorDetector:
    """
    Detects various types of errors in code consistency and structure.

    This module focuses on identifying inconsistencies, structural problems,
    and validation errors in code analysis.
    """

    def __init__(self):
        """Initialize error detector with configuration."""
        self.error_patterns = {
            "syntax_errors": [],
            "structural_errors": [],
            "consistency_errors": [],
            "logic_errors": [],
        }
        self.severity_thresholds = {
            "critical": 0.9,  # Issues that prevent basic functionality
            "high": 0.7,  # Issues that significantly impact quality
            "medium": 0.4,  # Issues that moderately impact quality
            "low": 0.1,  # Minor issues or style problems
        }

    def detect_syntax_errors(
        self, code: str, context: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """
        Detect syntax errors in code.

        Args:
            code: Source code to analyze
            context: Additional context for error reporting

        Returns:
            List of syntax error details
        """
        errors = []

        if not code or not code.strip():
            errors.append(
                {
                    "type": "syntax",
                    "category": "empty_code",
                    "severity": "high",
                    "description": "Empty or whitespace-only code provided",
                    "line": 0,
                    "column": 0,
                    "context": context or {},
                }
            )
            return errors

        try:
            ast.parse(code)
        except SyntaxError as e:
            errors.append(
                {
                    "type": "syntax",
                    "category": "parse_error",
                    "severity": "critical",
                    "description": f"Syntax error: {str(e)}",
                    "line": e.lineno or 0,
                    "column": e.offset or 0,
                    "error_text": e.text or "",
                    "context": context or {},
                }
            )
        except Exception as e:
            errors.append(
                {
                    "type": "syntax",
                    "category": "unknown_parse_error",
                    "severity": "high",
                    "description": f"Failed to parse code: {str(e)}",
                    "line": 0,
                    "column": 0,
                    "context": context or {},
                }
            )

        return errors

    def detect_structural_errors(
        self, parsed_ast: ast.AST, context: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """
        Detect structural errors in parsed AST.

        Args:
            parsed_ast: Parsed AST to analyze
            context: Additional context for error reporting

        Returns:
            List of structural error details
        """
        errors: list[dict[str, Any]] = []

        try:
            # Check for missing function definitions
            function_nodes = [
                node for node in ast.walk(parsed_ast) if isinstance(node, ast.FunctionDef)
            ]
            class_nodes = [node for node in ast.walk(parsed_ast) if isinstance(node, ast.ClassDef)]

            if not function_nodes and not class_nodes:
                errors.append(
                    {
                        "type": "structural",
                        "category": "no_definitions",
                        "severity": "medium",
                        "description": "No function or class definitions found",
                        "context": context or {},
                    }
                )

            # Check for malformed function definitions
            for func_node in function_nodes:
                if not func_node.body:
                    errors.append(
                        {
                            "type": "structural",
                            "category": "empty_function",
                            "severity": "medium",
                            "description": f"Function '{func_node.name}' has empty body",
                            "function_name": func_node.name,
                            "line": func_node.lineno,
                            "context": context or {},
                        }
                    )

            # Check for malformed class definitions
            for class_node in class_nodes:
                if not class_node.body:
                    errors.append(
                        {
                            "type": "structural",
                            "category": "empty_class",
                            "severity": "medium",
                            "description": f"Class '{class_node.name}' has empty body",
                            "class_name": class_node.name,
                            "line": class_node.lineno,
                            "context": context or {},
                        }
                    )

            # Check for unreachable code
            unreachable_errors = self._detect_unreachable_code(parsed_ast)
            errors.extend(unreachable_errors)

        except Exception as e:
            errors.append(
                {
                    "type": "structural",
                    "category": "analysis_error",
                    "severity": "medium",
                    "description": f"Failed to analyze structure: {str(e)}",
                    "context": context or {},
                }
            )

        return errors

    def detect_consistency_errors(
        self, code: str, description: str, context: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """
        Detect consistency errors between code and description.

        Args:
            code: Source code
            description: Description to validate against
            context: Additional context

        Returns:
            List of consistency error details
        """
        errors = []

        try:
            tree = ast.parse(code)

            # Check for missing descriptions
            if not description or not description.strip():
                errors.append(
                    {
                        "type": "consistency",
                        "category": "missing_description",
                        "severity": "high",
                        "description": "No description provided for code analysis",
                        "context": context or {},
                    }
                )
                return errors

            # Find main code elements
            functions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
            classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]

            # Check function-description consistency
            for func in functions:
                func_errors = self._check_function_description_consistency(func, description)
                errors.extend(func_errors)

            # Check class-description consistency
            for cls in classes:
                class_errors = self._check_class_description_consistency(cls, description)
                errors.extend(class_errors)

            # Check for description-code mismatches
            description_errors = self._detect_description_mismatches(tree, description)
            errors.extend(description_errors)

        except SyntaxError:
            errors.append(
                {
                    "type": "consistency",
                    "category": "unparseable_code",
                    "severity": "critical",
                    "description": "Cannot analyze consistency due to syntax errors",
                    "context": context or {},
                }
            )
        except Exception as e:
            errors.append(
                {
                    "type": "consistency",
                    "category": "analysis_error",
                    "severity": "medium",
                    "description": f"Consistency analysis failed: {str(e)}",
                    "context": context or {},
                }
            )

        return errors

    def detect_logic_errors(
        self, parsed_ast: ast.AST, context: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """
        Detect potential logic errors in code.

        Args:
            parsed_ast: Parsed AST to analyze
            context: Additional context

        Returns:
            List of logic error details
        """
        errors = []

        try:
            # Check for infinite loops
            loop_errors = self._detect_infinite_loops(parsed_ast)
            errors.extend(loop_errors)

            # Check for unused variables
            unused_errors = self._detect_unused_variables(parsed_ast)
            errors.extend(unused_errors)

            # Check for potential null pointer issues
            null_errors = self._detect_null_pointer_issues(parsed_ast)
            errors.extend(null_errors)

        except Exception as e:
            errors.append(
                {
                    "type": "logic",
                    "category": "analysis_error",
                    "severity": "medium",
                    "description": f"Logic analysis failed: {str(e)}",
                    "context": context or {},
                }
            )

        return errors

    def _detect_unreachable_code(self, parsed_ast: ast.AST) -> list[dict[str, Any]]:
        """Detect unreachable code patterns."""
        errors = []

        for node in ast.walk(parsed_ast):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Check for code after return statements
                for i, stmt in enumerate(node.body):
                    if isinstance(stmt, ast.Return):
                        if i < len(node.body) - 1:  # Not the last statement
                            errors.append(
                                {
                                    "type": "structural",
                                    "category": "unreachable_code",
                                    "severity": "medium",
                                    "description": f"Unreachable code after return in function '{node.name}'",
                                    "function_name": node.name,
                                    "line": (
                                        node.body[i + 1].lineno
                                        if hasattr(node.body[i + 1], "lineno")
                                        else 0
                                    ),
                                }
                            )
                            break  # Only report first occurrence per function

        return errors

    def _check_function_description_consistency(
        self, func_node: ast.FunctionDef, description: str
    ) -> list[dict[str, Any]]:
        """Check consistency between function and its description."""
        errors: list[dict[str, Any]] = []

        # Check if function name is mentioned
        if func_node.name not in description:
            errors.append(
                {
                    "type": "consistency",
                    "category": "missing_function_name",
                    "severity": "low",
                    "description": f"Function name '{func_node.name}' not mentioned in description",
                    "function_name": func_node.name,
                }
            )

        # Check parameters
        params = [arg.arg for arg in func_node.args.args if arg.arg != "self"]
        mentioned_params = [param for param in params if param in description]

        if params and len(mentioned_params) == 0:
            errors.append(
                {
                    "type": "consistency",
                    "category": "missing_parameters",
                    "severity": "medium",
                    "description": f"Function parameters {params} not mentioned in description",
                    "function_name": func_node.name,
                    "parameters": params,
                }
            )

        # Check return statements
        has_return = any(
            isinstance(node, ast.Return) and node.value is not None for node in ast.walk(func_node)
        )

        if has_return and "return" not in description.lower():
            errors.append(
                {
                    "type": "consistency",
                    "category": "missing_return_info",
                    "severity": "medium",
                    "description": f"Function '{func_node.name}' returns values but description doesn't mention this",
                    "function_name": func_node.name,
                }
            )

        return errors

    def _check_class_description_consistency(
        self, class_node: ast.ClassDef, description: str
    ) -> list[dict[str, Any]]:
        """Check consistency between class and its description."""
        errors: list[dict[str, Any]] = []

        # Check if class name is mentioned
        if class_node.name not in description:
            errors.append(
                {
                    "type": "consistency",
                    "category": "missing_class_name",
                    "severity": "low",
                    "description": f"Class name '{class_node.name}' not mentioned in description",
                    "class_name": class_node.name,
                }
            )

        # Check methods
        methods = [node.name for node in class_node.body if isinstance(node, ast.FunctionDef)]
        public_methods = [method for method in methods if not method.startswith("_")]

        if public_methods:
            mentioned_methods = [method for method in public_methods if method in description]
            if len(mentioned_methods) == 0:
                errors.append(
                    {
                        "type": "consistency",
                        "category": "missing_methods",
                        "severity": "medium",
                        "description": f"Class methods {public_methods} not mentioned in description",
                        "class_name": class_node.name,
                        "methods": public_methods,
                    }
                )

        return errors

    def _detect_description_mismatches(
        self, parsed_ast: ast.AST, description: str
    ) -> list[dict[str, Any]]:
        """Detect mismatches between described and actual functionality."""
        errors = []

        # This is a simplified implementation
        # In practice, this would use more sophisticated NLP techniques

        description_lower = description.lower()

        # Check for mentioned but non-existent elements
        code_text = ast.unparse(parsed_ast).lower()

        # Common mismatches to check
        mentioned_operations = {
            "database": ["database", "db", "sql", "query"],
            "file": ["file", "save", "load", "read", "write"],
            "network": ["http", "request", "api", "url"],
            "async": ["async", "await", "asynchronous"],
        }

        for operation, keywords in mentioned_operations.items():
            mentioned_in_desc = any(keyword in description_lower for keyword in keywords)
            present_in_code = any(keyword in code_text for keyword in keywords)

            if mentioned_in_desc and not present_in_code:
                errors.append(
                    {
                        "type": "consistency",
                        "category": "functionality_mismatch",
                        "severity": "medium",
                        "description": f"Description mentions {operation} operations but code doesn't contain them",
                        "operation_type": operation,
                    }
                )

        return errors

    def _detect_infinite_loops(self, parsed_ast: ast.AST) -> list[dict[str, Any]]:
        """Detect potential infinite loops."""
        errors = []

        for node in ast.walk(parsed_ast):
            if isinstance(node, ast.While):
                # Check for while True without break
                if isinstance(node.test, ast.Constant) and node.test.value is True:
                    # Look for break statements
                    has_break = any(isinstance(n, ast.Break) for n in ast.walk(node))
                    if not has_break:
                        errors.append(
                            {
                                "type": "logic",
                                "category": "infinite_loop",
                                "severity": "high",
                                "description": "Potential infinite loop detected (while True without break)",
                                "line": node.lineno if hasattr(node, "lineno") else 0,
                            }
                        )

        return errors

    def _detect_unused_variables(self, parsed_ast: ast.AST) -> list[dict[str, Any]]:
        """Detect potentially unused variables."""
        errors = []

        # This is a simplified implementation
        # A full implementation would require more sophisticated data flow analysis

        for node in ast.walk(parsed_ast):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assigned_vars = set()
                used_vars = set()

                for child in ast.walk(node):
                    if isinstance(child, ast.Assign):
                        for target in child.targets:
                            if isinstance(target, ast.Name):
                                assigned_vars.add(target.id)
                    elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                        used_vars.add(child.id)

                unused = assigned_vars - used_vars
                for var in unused:
                    if not var.startswith("_"):  # Ignore variables starting with underscore
                        errors.append(
                            {
                                "type": "logic",
                                "category": "unused_variable",
                                "severity": "low",
                                "description": f"Variable '{var}' assigned but never used",
                                "variable_name": var,
                                "function_name": node.name,
                            }
                        )

        return errors

    def _detect_null_pointer_issues(self, parsed_ast: ast.AST) -> list[dict[str, Any]]:
        """Detect potential null pointer or None-related issues."""
        errors: list[dict[str, Any]] = []

        for node in ast.walk(parsed_ast):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Collect variables explicitly assigned to None in this scope
                none_assigned_vars: set[str] = set()
                for child in ast.walk(node):
                    if isinstance(child, ast.Assign):
                        for target in child.targets:
                            if isinstance(target, ast.Name) and isinstance(
                                child.value, ast.Constant
                            ):
                                if child.value.value is None:
                                    none_assigned_vars.add(target.id)

                # Check for attribute access on variables known to be assigned None
                for child in ast.walk(node):
                    if isinstance(child, ast.Attribute) and isinstance(child.value, ast.Name):
                        if child.value.id in none_assigned_vars:
                            errors.append(
                                {
                                    "type": "logic",
                                    "category": "potential_none_access",
                                    "severity": "high",
                                    "description": (
                                        f"Attribute '.{child.attr}' accessed on variable "
                                        f"'{child.value.id}' which was assigned None"
                                    ),
                                    "variable_name": child.value.id,
                                    "attribute": child.attr,
                                    "function_name": node.name,
                                    "line": child.lineno if hasattr(child, "lineno") else 0,
                                }
                            )

        return errors


class ErrorReporter:
    """
    Formats and reports detected errors in various formats.

    Provides structured reporting of consistency and validation errors
    with different output formats and severity filtering.
    """

    def __init__(self):
        """Initialize error reporter."""
        self.report_templates = {
            "summary": "Summary: {total_errors} errors found ({critical} critical, {high} high, {medium} medium, {low} low)",
            "detailed": "Error: {description} (Type: {type}, Severity: {severity})",
            "json": "JSON format for programmatic consumption",
        }

    def generate_summary_report(self, errors: list[dict[str, Any]]) -> dict[str, Any]:
        """
        Generate a summary report of all errors.

        Args:
            errors: List of error dictionaries

        Returns:
            Summary report with counts and statistics
        """
        errors_by_severity: dict[str, int] = {
            "critical": 0,
            "high": 0,
            "medium": 0,
            "low": 0,
        }
        errors_by_type: dict[str, int] = {}
        errors_by_category: dict[str, int] = {}
        most_common_issues: list[tuple[str, int]] = []
        severity_score = 0.0

        if not errors:
            return {
                "total_errors": 0,
                "errors_by_severity": errors_by_severity,
                "errors_by_type": errors_by_type,
                "errors_by_category": errors_by_category,
                "most_common_issues": most_common_issues,
                "severity_score": 1.0,
            }

        # Count by severity
        for error in errors:
            severity = error.get("severity", "medium")
            if severity in errors_by_severity:
                errors_by_severity[severity] += 1

        # Count by type
        for error in errors:
            error_type = error.get("type", "unknown")
            errors_by_type[error_type] = errors_by_type.get(error_type, 0) + 1

        # Count by category
        for error in errors:
            category = error.get("category", "unknown")
            errors_by_category[category] = errors_by_category.get(category, 0) + 1

        # Find most common issues
        category_counts = list(errors_by_category.items())
        category_counts.sort(key=lambda x: x[1], reverse=True)
        most_common_issues = category_counts[:5]

        # Calculate severity score (0.0 = worst, 1.0 = perfect)
        severity_weights = {"critical": 1.0, "high": 0.7, "medium": 0.4, "low": 0.1}
        total_weight = sum(
            errors_by_severity[sev] * weight for sev, weight in severity_weights.items()
        )

        # Normalize to 0-1 scale (assuming max 10 errors of each type as baseline)
        max_possible_weight = sum(severity_weights.values()) * 10
        severity_score = max(0.0, 1.0 - (total_weight / max_possible_weight))

        return {
            "total_errors": len(errors),
            "errors_by_severity": errors_by_severity,
            "errors_by_type": errors_by_type,
            "errors_by_category": errors_by_category,
            "most_common_issues": most_common_issues,
            "severity_score": severity_score,
        }

    def format_error_details(
        self, errors: list[dict[str, Any]], include_context: bool = True
    ) -> list[str]:
        """
        Format error details for human-readable output.

        Args:
            errors: List of error dictionaries
            include_context: Whether to include context information

        Returns:
            List of formatted error strings
        """
        formatted = []

        for error in errors:
            base_info = f"[{error.get('severity', 'unknown').upper()}] {error.get('description', 'No description')}"

            # Add type and category info
            if error.get("type") or error.get("category"):
                type_info = f" (Type: {error.get('type', 'unknown')}"
                if error.get("category"):
                    type_info += f", Category: {error.get('category')}"
                type_info += ")"
                base_info += type_info

            # Add location info if available
            if error.get("line"):
                base_info += f" at line {error['line']}"
                if error.get("column"):
                    base_info += f", column {error['column']}"

            # Add specific entity info
            if error.get("function_name"):
                base_info += f" in function '{error['function_name']}'"
            elif error.get("class_name"):
                base_info += f" in class '{error['class_name']}'"

            # Add context if requested and available
            if include_context and error.get("context"):
                context_str = ", ".join(
                    [f"{k}: {v}" for k, v in error["context"].items() if k != "code"]
                )
                if context_str:
                    base_info += f" (Context: {context_str})"

            formatted.append(base_info)

        return formatted

    def filter_errors_by_severity(
        self, errors: list[dict[str, Any]], min_severity: str = "low"
    ) -> list[dict[str, Any]]:
        """
        Filter errors by minimum severity level.

        Args:
            errors: List of error dictionaries
            min_severity: Minimum severity to include ("critical", "high", "medium", "low")

        Returns:
            Filtered list of errors
        """
        severity_levels = {"critical": 4, "high": 3, "medium": 2, "low": 1}
        min_level = severity_levels.get(min_severity, 1)

        return [
            error
            for error in errors
            if severity_levels.get(error.get("severity", "low"), 1) >= min_level
        ]

    def group_errors_by_type(self, errors: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        """
        Group errors by their type.

        Args:
            errors: List of error dictionaries

        Returns:
            Dict mapping error types to lists of errors
        """
        grouped: dict[str, list[dict[str, Any]]] = {}

        for error in errors:
            error_type = error.get("type", "unknown")
            if error_type not in grouped:
                grouped[error_type] = []
            grouped[error_type].append(error)

        return grouped

    def export_errors_json(
        self, errors: list[dict[str, Any]], include_metadata: bool = True
    ) -> dict[str, Any]:
        """
        Export errors in JSON format for programmatic consumption.

        Args:
            errors: List of error dictionaries
            include_metadata: Whether to include metadata like timestamps

        Returns:
            JSON-serializable dict with error data
        """
        export_data = {
            "errors": errors,
            "summary": self.generate_summary_report(errors),
        }

        if include_metadata:
            export_data["metadata"] = {
                "timestamp": time.time(),
                "total_count": len(errors),
                "export_version": "1.0",
            }

        return export_data
