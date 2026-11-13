"""Main module for checking consistency in code graphs and structures."""

from __future__ import annotations

import logging
import time
from typing import Any

from services.code_graph.consistency_rules_engine import (
    ConsistencyRulesEngine,
    ModuleConsistencyChecker,
)
from services.code_graph.error_detection_reporting import (
    ErrorDetector,
    ErrorReporter,
)
from services.code_graph.graph_validation import GraphValidator
from services.code_graph.recovery_mechanisms import RecoveryMechanisms
from services.code_graph.validation_utilities import (
    ASTUtils,
    GraphUtils,
    HashUtils,
    ValidationScoreCalculator,
    validation_cache,
)

logger = logging.getLogger(__name__)


class CodeConsistencyChecker:
    """
    Main class for checking consistency in code graphs and structures.

    This helps prevent hallucinations by validating that descriptions of code
    functionality match the actual implementation and ensures structural consistency
    across the codebase.

    This is now a facade that coordinates the various specialized modules.
    """

    def __init__(self) -> None:
        """Initialize with modular components."""
        self.rules_engine = ConsistencyRulesEngine()
        self.error_detector = ErrorDetector()
        self.error_reporter = ErrorReporter()
        self.graph_validator = GraphValidator()
        self.recovery_mechanisms = RecoveryMechanisms()
        self.module_checker = ModuleConsistencyChecker()
        self.score_calculator = ValidationScoreCalculator()

        # Configuration
        self.config = {
            "severity_weights": {"high": 0.3, "medium": 0.2, "low": 0.1},
            "context_weights": {
                "public_api": 1.2,  # Higher impact for public API inconsistencies
                "internal": 1.0,
            },
            "enable_caching": True,
            "fast_mode": False,  # For testing scenarios
        }

    def check_function_description(
        self, function_code: str, description: str, context: str = "internal"
    ) -> dict[str, Any]:
        """
        Check if a function description is consistent with actual code.

        Args:
            function_code: The actual code of the function
            description: The AI's description of what the function does
            context: The context of the function ("public_api" or "internal")

        Returns:
            Comprehensive consistency check result including:
            - consistency_score: float (0.0-1.0)
            - is_consistent: bool
            - issues: List of detected issues
            - suggestions: List of improvement suggestions
            - analysis_time_ms: Processing time
        """
        start_time = time.time()

        # Check cache first if enabled
        if self.config["enable_caching"]:
            cache_key = HashUtils.create_cache_key(
                "function_check", function_code, description, context
            )
            cached_result = validation_cache.get(cache_key)
            if cached_result:
                return cached_result

        # Quick validation for empty inputs
        if not function_code.strip():
            return self._create_error_result(
                "Empty function code provided",
                analysis_time=int((time.time() - start_time) * 1000),
                context=context,
            )

        if not description.strip():
            return self._create_error_result(
                "Empty description provided",
                analysis_time=int((time.time() - start_time) * 1000),
                context=context,
            )

        try:
            # Parse the function code
            parsed_ast = ASTUtils.safe_parse(function_code)
            if not parsed_ast:
                return self._create_error_result(
                    "Failed to parse function code",
                    analysis_time=int((time.time() - start_time) * 1000),
                    context=context,
                )

            # Find function node
            functions = ASTUtils.extract_functions(parsed_ast)
            if not functions:
                return self._create_error_result(
                    "No function definition found in code",
                    analysis_time=int((time.time() - start_time) * 1000),
                    context=context,
                )

            function_node = functions[0]  # Use the first function found

            # Detect various types of errors
            all_issues = []

            # 1. Syntax and structural errors
            syntax_errors = self.error_detector.detect_syntax_errors(function_code)
            structural_errors = self.error_detector.detect_structural_errors(parsed_ast)
            all_issues.extend(syntax_errors + structural_errors)

            # 2. Consistency errors using rules engine
            consistency_result = self.rules_engine.check_consistency(
                function_code,
                description,
                {"context": context, "function_node": function_node},
            )
            all_issues.extend(consistency_result["all_issues"])

            # 3. Logic errors
            logic_errors = self.error_detector.detect_logic_errors(parsed_ast)
            all_issues.extend(logic_errors)

            # Generate suggestions for fixing issues
            suggestions = self.recovery_mechanisms.generate_suggestions(
                all_issues, {"function_node": function_node, "description": description}
            )

            # Calculate consistency score
            consistency_score = self.score_calculator.calculate_consistency_score(
                all_issues, context
            )

            # Build result
            result = {
                "consistency_score": consistency_score,
                "is_consistent": consistency_score >= 0.7,
                "issues": all_issues,
                "suggestions": suggestions,
                "analysis_time_ms": int((time.time() - start_time) * 1000),
                "context": context,
                "function_info": ASTUtils.get_function_info(function_node),
                "rules_summary": {
                    "total_rules": consistency_result["total_rules"],
                    "rules_passed": consistency_result["rules_passed"],
                    "issues_by_severity": consistency_result["issues_by_severity"],
                },
            }

            # Cache the result
            if self.config["enable_caching"]:
                validation_cache.set(cache_key, result)

            return result

        except Exception as e:
            logger.error(f"Function consistency check failed: {e}")
            return self._create_error_result(
                f"Analysis failed: {str(e)}",
                analysis_time=int((time.time() - start_time) * 1000),
                context=context,
            )

    def check_class_description(
        self, class_code: str, description: str, context: str = "internal"
    ) -> dict[str, Any]:
        """
        Check if a class description is consistent with actual code.

        Args:
            class_code: The actual code of the class
            description: The AI's description of what the class does
            context: The context of the class ("public_api" or "internal")

        Returns:
            Comprehensive consistency check result for the class
        """
        start_time = time.time()

        # Check cache first
        if self.config["enable_caching"]:
            cache_key = HashUtils.create_cache_key("class_check", class_code, description, context)
            cached_result = validation_cache.get(cache_key)
            if cached_result:
                return cached_result

        # Validate inputs
        if not class_code.strip() or not description.strip():
            return self._create_error_result(
                "Empty class code or description provided",
                analysis_time=int((time.time() - start_time) * 1000),
                context=context,
            )

        try:
            # Parse the class code
            parsed_ast = ASTUtils.safe_parse(class_code)
            if not parsed_ast:
                return self._create_error_result(
                    "Failed to parse class code",
                    analysis_time=int((time.time() - start_time) * 1000),
                    context=context,
                )

            # Find class node
            classes = ASTUtils.extract_classes(parsed_ast)
            if not classes:
                return self._create_error_result(
                    "No class definition found in code",
                    analysis_time=int((time.time() - start_time) * 1000),
                    context=context,
                )

            class_node = classes[0]  # Use the first class found

            # Detect errors
            all_issues = []

            # Syntax and structural errors
            syntax_errors = self.error_detector.detect_syntax_errors(class_code)
            structural_errors = self.error_detector.detect_structural_errors(parsed_ast)
            all_issues.extend(syntax_errors + structural_errors)

            # Class-specific consistency errors
            consistency_errors = self.error_detector.detect_consistency_errors(
                class_code, description
            )
            all_issues.extend(consistency_errors)

            # Generate suggestions
            suggestions = self.recovery_mechanisms.generate_suggestions(
                all_issues, {"class_node": class_node, "description": description}
            )

            # Calculate scores
            consistency_score = self.score_calculator.calculate_consistency_score(
                all_issues, context
            )

            # Extract class information
            class_info = ASTUtils.get_class_info(class_node)

            result = {
                "consistency_score": consistency_score,
                "is_consistent": consistency_score >= 0.7,
                "issues": all_issues,
                "suggestions": suggestions,
                "analysis_time_ms": int((time.time() - start_time) * 1000),
                "context": context,
                "class_info": class_info,
                "class_name": class_info["name"],
                "missing_aspects": [
                    issue["description"]
                    for issue in all_issues
                    if "not mentioned" in issue.get("description", "")
                ],
                "contradictions": [
                    issue["description"]
                    for issue in all_issues
                    if issue.get("severity") == "high"
                    and "not mentioned" not in issue.get("description", "")
                ],
            }

            # Cache result
            if self.config["enable_caching"]:
                validation_cache.set(cache_key, result)

            return result

        except Exception as e:
            logger.error(f"Class consistency check failed: {e}")
            return self._create_error_result(
                f"Analysis failed: {str(e)}",
                analysis_time=int((time.time() - start_time) * 1000),
                context=context,
            )

    def check_module_consistency(
        self, modules: dict[str, str], module_paths: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """
        Check consistency across multiple modules in a codebase.

        Args:
            modules: Dict mapping module names to their code content
            module_paths: Dict mapping module names to their file paths

        Returns:
            Comprehensive module consistency analysis
        """
        start_time = time.time()

        if module_paths is None:
            module_paths = {}

        # Check cache
        if self.config["enable_caching"]:
            cache_key = HashUtils.create_cache_key("module_check", HashUtils.hash_dict(modules))
            cached_result = validation_cache.get(cache_key)
            if cached_result:
                return cached_result

        try:
            # Parse all modules
            parsed_modules = {}
            parsing_errors = []

            for module_name, code in modules.items():
                parsed_ast = ASTUtils.safe_parse(code)
                if parsed_ast:
                    parsed_modules[module_name] = parsed_ast
                else:
                    parsing_errors.append(
                        {
                            "type": "syntax",
                            "severity": "critical",
                            "description": f"Failed to parse module {module_name}",
                            "module": module_name,
                        }
                    )

            all_issues = parsing_errors.copy()

            # Check naming conventions
            naming_issues = self.module_checker.check_naming_conventions(parsed_modules)
            all_issues.extend(naming_issues)

            # Check import patterns
            import_issues = self.module_checker.check_import_patterns(parsed_modules)
            all_issues.extend(import_issues)

            # Validate graph structure if we have module relationships
            if len(parsed_modules) > 1:
                dependency_graph = GraphUtils.create_dependency_graph(modules)
                graph_validation = self.graph_validator.validate_graph_structure(dependency_graph)

                # Convert graph issues to module issues
                for graph_issue in graph_validation["structural_issues"]:
                    all_issues.append(
                        {
                            "type": "module_dependency",
                            "severity": graph_issue["severity"],
                            "description": f"Module dependency issue: {graph_issue['description']}",
                            "modules": graph_issue.get("affected_nodes", []),
                        }
                    )

            # Generate module-level suggestions
            suggestions = self.recovery_mechanisms.generate_suggestions(all_issues)

            # Calculate metrics
            consistency_score = self.score_calculator.calculate_consistency_score(all_issues)

            # Group issues by type and severity
            issues_by_type = {}
            issues_by_severity = {"critical": 0, "high": 0, "medium": 0, "low": 0}

            for issue in all_issues:
                issue_type = issue.get("type", "unknown")
                issues_by_type[issue_type] = issues_by_type.get(issue_type, 0) + 1

                severity = issue.get("severity", "medium")
                if severity in issues_by_severity:
                    issues_by_severity[severity] += 1

            result = {
                "consistency_score": consistency_score,
                "is_consistent": consistency_score >= 0.7,
                "module_count": len(modules),
                "parsed_modules": len(parsed_modules),
                "issues": all_issues,
                "suggestions": suggestions,
                "analysis_time_ms": int((time.time() - start_time) * 1000),
                "issues_by_type": issues_by_type,
                "issues_by_severity": issues_by_severity,
                "naming_consistency": max(0.0, 1.0 - (len(naming_issues) * 0.1)),
                "import_consistency": max(0.0, 1.0 - (len(import_issues) * 0.1)),
                "structural_consistency": max(
                    0.0,
                    1.0
                    - (len([i for i in all_issues if i.get("type") == "module_dependency"]) * 0.2),
                ),
            }

            # Cache result
            if self.config["enable_caching"]:
                validation_cache.set(cache_key, result)

            return result

        except Exception as e:
            logger.error(f"Module consistency check failed: {e}")
            return {
                "consistency_score": 0.0,
                "is_consistent": False,
                "module_count": len(modules),
                "parsed_modules": 0,
                "issues": [
                    {
                        "type": "analysis_error",
                        "severity": "high",
                        "description": f"Module analysis failed: {str(e)}",
                    }
                ],
                "suggestions": ["Check module syntax and try again"],
                "analysis_time_ms": int((time.time() - start_time) * 1000),
                "error": str(e),
            }

    def generate_comprehensive_report(
        self, analysis_results: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """
        Generate a comprehensive report from multiple analysis results.

        Args:
            analysis_results: List of individual analysis results

        Returns:
            Comprehensive report with summary and recommendations
        """
        if not analysis_results:
            return {"error": "No analysis results provided"}

        # Aggregate all issues
        all_issues = []
        for result in analysis_results:
            all_issues.extend(result.get("issues", []))

        # Generate summary using error reporter
        summary = self.error_reporter.generate_summary_report(all_issues)

        # Generate recovery plan
        recovery_plan = self.recovery_mechanisms.generate_recovery_plan(all_issues)

        # Calculate overall metrics
        avg_consistency_score = sum(
            result.get("consistency_score", 0.0) for result in analysis_results
        ) / len(analysis_results)

        overall_quality_score = self.score_calculator.calculate_quality_score(
            {
                "consistency_score": avg_consistency_score,
                "total_issues": len(all_issues),
                "critical_issues": summary["errors_by_severity"]["critical"],
            }
        )

        return {
            "summary": summary,
            "recovery_plan": recovery_plan,
            "overall_metrics": {
                "average_consistency_score": avg_consistency_score,
                "overall_quality_score": overall_quality_score,
                "total_analyses": len(analysis_results),
                "analyses_passed": len(
                    [r for r in analysis_results if r.get("is_consistent", False)]
                ),
            },
            "recommendations": recovery_plan.get("all_suggestions", [])[
                :10
            ],  # Top 10 recommendations
            "priority_actions": recovery_plan.get("action_plan", []),
        }

    def _create_error_result(
        self, error_message: str, analysis_time: int, context: str = "internal"
    ) -> dict[str, Any]:
        """Create a standardized error result."""
        return {
            "consistency_score": 0.0,
            "is_consistent": False,
            "issues": [{"type": "validation", "severity": "high", "description": error_message}],
            "suggestions": ["Resolve the validation error and try again"],
            "analysis_time_ms": analysis_time,
            "context": context,
            "error": error_message,
        }

    def clear_cache(self) -> None:
        """Clear the validation cache."""
        validation_cache.clear()

    def get_cache_stats(self) -> dict[str, Any]:
        """Get cache statistics."""
        return {
            "cache_size": len(validation_cache),
            "max_size": validation_cache.max_size,
            "default_ttl": validation_cache.default_ttl,
        }

    def configure(self, **kwargs) -> None:
        """Update configuration settings."""
        self.config.update(kwargs)

    def get_available_rules(self) -> list[str]:
        """Get list of available consistency rules."""
        return self.rules_engine.get_enabled_rules()

    def add_custom_rule(self, rule) -> None:
        """Add a custom consistency rule."""
        self.rules_engine.add_rule(rule)

    def add_custom_suggestion_strategy(self, strategy) -> None:
        """Add a custom suggestion strategy."""
        self.recovery_mechanisms.add_custom_strategy(strategy)
