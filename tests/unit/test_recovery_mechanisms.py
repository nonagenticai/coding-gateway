"""Tests for services.code_graph.recovery_mechanisms."""

from __future__ import annotations

import ast

from services.code_graph.recovery_mechanisms import (
    DefaultSuggestionStrategy,
    ExceptionSuggestionStrategy,
    LogicSuggestionStrategy,
    NamingSuggestionStrategy,
    ParameterSuggestionStrategy,
    RecoveryMechanisms,
    ReturnSuggestionStrategy,
    StructuralSuggestionStrategy,
    SuggestionStrategy,
)

# ---------------------------------------------------------------------------
# ParameterSuggestionStrategy
# ---------------------------------------------------------------------------


class TestParameterSuggestionStrategy:
    def setup_method(self):
        self.s = ParameterSuggestionStrategy()

    def test_strategy_name(self):
        assert self.s.get_strategy_name() == "parameter_suggestion"

    def test_applicable_types(self):
        assert "parameter" in self.s.get_applicable_types()

    def test_single_missing_param(self):
        issues = [
            {
                "type": "parameter",
                "description": "Parameter 'name' is not mentioned in the description",
                "parameter_name": "name",
            }
        ]
        suggestions = self.s.generate(issues, {})
        assert any("name" in s for s in suggestions)

    def test_multiple_missing_params(self):
        issues = [
            {
                "type": "parameter",
                "description": "Parameter 'x' is not mentioned",
                "parameter_name": "x",
            },
            {
                "type": "parameter",
                "description": "Parameter 'y' is not mentioned",
                "parameter_name": "y",
            },
        ]
        suggestions = self.s.generate(issues, {})
        assert any("'x'" in s and "'y'" in s for s in suggestions)

    def test_with_function_node_typed(self):
        tree = ast.parse("def foo(x: int):\n    pass")
        func = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)][0]
        issues = [
            {
                "type": "parameter",
                "description": "Parameter 'x' is not mentioned",
                "parameter_name": "x",
            },
        ]
        suggestions = self.s.generate(issues, {"function_node": func})
        assert any("type" in s.lower() for s in suggestions)

    def test_no_matching_issues(self):
        issues = [{"type": "return", "description": "not relevant"}]
        suggestions = self.s.generate(issues, {})
        assert suggestions == []


# ---------------------------------------------------------------------------
# ReturnSuggestionStrategy
# ---------------------------------------------------------------------------


class TestReturnSuggestionStrategy:
    def setup_method(self):
        self.s = ReturnSuggestionStrategy()

    def test_strategy_name(self):
        assert self.s.get_strategy_name() == "return_suggestion"

    def test_missing_return_info(self):
        issues = [
            {"type": "return", "description": "Function returns values but this is not mentioned"},
        ]
        suggestions = self.s.generate(issues, {})
        assert any("return" in s.lower() for s in suggestions)

    def test_missing_return_type(self):
        issues = [
            {
                "type": "return",
                "description": "Return type 'int' is not mentioned",
                "return_type": "int",
            },
        ]
        suggestions = self.s.generate(issues, {})
        assert any("int" in s for s in suggestions)

    def test_multiple_return_types(self):
        issues = [
            {"type": "return", "description": "Return type 'int' missing", "return_type": "int"},
            {"type": "return", "description": "Return type 'str' missing", "return_type": "str"},
        ]
        suggestions = self.s.generate(issues, {})
        assert any("return type" in s.lower() or "int" in s for s in suggestions)


# ---------------------------------------------------------------------------
# ExceptionSuggestionStrategy
# ---------------------------------------------------------------------------


class TestExceptionSuggestionStrategy:
    def setup_method(self):
        self.s = ExceptionSuggestionStrategy()

    def test_strategy_name(self):
        assert self.s.get_strategy_name() == "exception_suggestion"

    def test_missing_exception_handling(self):
        issues = [
            {
                "type": "exception",
                "description": "Function handles exceptions but this is not mentioned",
            },
        ]
        suggestions = self.s.generate(issues, {})
        assert any("exception" in s.lower() for s in suggestions)

    def test_specific_exception(self):
        issues = [
            {
                "type": "exception",
                "description": "Function handles 'ValueError' exceptions but not mentioned",
                "exception_type": "ValueError",
            }
        ]
        suggestions = self.s.generate(issues, {})
        assert any("ValueError" in s for s in suggestions)


# ---------------------------------------------------------------------------
# LogicSuggestionStrategy
# ---------------------------------------------------------------------------


class TestLogicSuggestionStrategy:
    def setup_method(self):
        self.s = LogicSuggestionStrategy()

    def test_strategy_name(self):
        assert self.s.get_strategy_name() == "logic_suggestion"

    def test_database_operation(self):
        issues = [
            {
                "type": "logic",
                "description": "Function performs database operations",
                "operation_type": "database",
            },
        ]
        suggestions = self.s.generate(issues, {})
        assert any("database" in s.lower() for s in suggestions)

    def test_async_operation(self):
        issues = [
            {
                "type": "logic",
                "description": "Function performs async operations",
                "operation_type": "async",
            },
        ]
        suggestions = self.s.generate(issues, {})
        assert any("asynchronous" in s.lower() for s in suggestions)

    def test_unknown_operation_extracted_from_desc(self):
        issues = [
            {
                "type": "logic",
                "description": "Function performs file operations",
                "operation_type": "unknown_thing",
            },
        ]
        suggestions = self.s.generate(issues, {})
        assert any("file" in s.lower() for s in suggestions)


# ---------------------------------------------------------------------------
# StructuralSuggestionStrategy
# ---------------------------------------------------------------------------


class TestStructuralSuggestionStrategy:
    def setup_method(self):
        self.s = StructuralSuggestionStrategy()

    def test_strategy_name(self):
        assert self.s.get_strategy_name() == "structural_suggestion"

    def test_empty_function(self):
        issues = [{"category": "empty_function"}]
        suggestions = self.s.generate(issues, {})
        assert any("empty function" in s.lower() for s in suggestions)

    def test_unreachable_code(self):
        issues = [{"category": "unreachable_code"}]
        suggestions = self.s.generate(issues, {})
        assert any("unreachable" in s.lower() for s in suggestions)

    def test_no_matching_categories(self):
        issues = [{"category": "something_else"}]
        suggestions = self.s.generate(issues, {})
        assert suggestions == []


# ---------------------------------------------------------------------------
# NamingSuggestionStrategy
# ---------------------------------------------------------------------------


class TestNamingSuggestionStrategy:
    def setup_method(self):
        self.s = NamingSuggestionStrategy()

    def test_strategy_name(self):
        assert self.s.get_strategy_name() == "naming_suggestion"

    def test_single_function(self):
        issues = [
            {"type": "naming", "entity_type": "function", "entity_name": "process"},
        ]
        suggestions = self.s.generate(issues, {})
        assert any("process" in s for s in suggestions)

    def test_multiple_classes(self):
        issues = [
            {"type": "naming", "entity_type": "class", "entity_name": "Foo"},
            {"type": "naming", "entity_type": "class", "entity_name": "Bar"},
        ]
        suggestions = self.s.generate(issues, {})
        assert any("Foo" in s and "Bar" in s for s in suggestions)


# ---------------------------------------------------------------------------
# DefaultSuggestionStrategy
# ---------------------------------------------------------------------------


class TestDefaultSuggestionStrategy:
    def setup_method(self):
        self.s = DefaultSuggestionStrategy()

    def test_strategy_name(self):
        assert self.s.get_strategy_name() == "default_suggestion"

    def test_no_issues(self):
        suggestions = self.s.generate([], {})
        assert any("well-aligned" in s.lower() for s in suggestions)

    def test_critical_issues(self):
        issues = [{"severity": "critical"}]
        suggestions = self.s.generate(issues, {})
        assert any("critical" in s.lower() for s in suggestions)

    def test_high_issues(self):
        issues = [{"severity": "high"}]
        suggestions = self.s.generate(issues, {})
        assert any("high" in s.lower() for s in suggestions)

    def test_medium_issues(self):
        issues = [{"severity": "medium"}]
        suggestions = self.s.generate(issues, {})
        assert len(suggestions) > 0


# ---------------------------------------------------------------------------
# RecoveryMechanisms
# ---------------------------------------------------------------------------


class TestRecoveryMechanisms:
    def setup_method(self):
        self.rm = RecoveryMechanisms()

    def test_get_available_strategies(self):
        names = self.rm.get_available_strategies()
        assert "parameter_suggestion" in names
        assert "return_suggestion" in names
        assert "default_suggestion" in names

    def test_generate_suggestions_empty(self):
        # Empty list but valid format → no issues → default strategy kicks in
        suggestions = self.rm.generate_suggestions([])
        assert isinstance(suggestions, list)

    def test_generate_suggestions_with_issues(self):
        issues = [
            {
                "type": "parameter",
                "description": "Parameter 'x' is not mentioned in the description",
                "severity": "medium",
                "parameter_name": "x",
            }
        ]
        suggestions = self.rm.generate_suggestions(issues)
        assert len(suggestions) > 0

    def test_generate_suggestions_invalid_input(self):
        # Issues not a list
        suggestions = self.rm.generate_suggestions("not a list")
        assert isinstance(suggestions, list)

    def test_generate_suggestions_missing_fields(self):
        # Issues with missing required fields
        suggestions = self.rm.generate_suggestions([{"no_type": True}])
        assert isinstance(suggestions, list)

    def test_generate_recovery_plan(self):
        issues = [
            {"type": "syntax", "description": "Syntax error", "severity": "critical"},
            {
                "type": "parameter",
                "description": "Parameter 'x' is not mentioned",
                "severity": "medium",
            },
            {"type": "naming", "description": "Function not mentioned", "severity": "low"},
        ]
        plan = self.rm.generate_recovery_plan(issues)
        assert plan["total_issues"] == 3
        assert "action_plan" in plan
        assert "all_suggestions" in plan
        assert "issue_breakdown" in plan

    def test_recovery_plan_empty(self):
        plan = self.rm.generate_recovery_plan([])
        assert plan["total_issues"] == 0
        assert plan["estimated_effort"] == "minimal"

    def test_estimate_effort_critical(self):
        issues = [{"severity": "critical", "type": "x", "description": "x"}]
        effort = self.rm._estimate_effort(issues)
        assert effort == "high"

    def test_estimate_effort_many_high(self):
        issues = [{"severity": "high"}] * 4
        effort = self.rm._estimate_effort(issues)
        assert effort == "medium"

    def test_estimate_effort_minimal(self):
        issues = [{"severity": "low"}] * 2
        effort = self.rm._estimate_effort(issues)
        assert effort == "minimal"

    def test_add_custom_strategy(self):
        class Custom(SuggestionStrategy):
            def generate(self, issues, context):
                return ["custom suggestion"]

            def get_strategy_name(self):
                return "custom"

            def get_applicable_types(self):
                return ["custom_type"]

        self.rm.add_custom_strategy(Custom())
        assert "custom" in self.rm.get_available_strategies()

    def test_remove_strategy(self):
        removed = self.rm.remove_strategy("parameter_suggestion")
        assert removed is True
        assert "parameter_suggestion" not in self.rm.get_available_strategies()

    def test_remove_nonexistent_strategy(self):
        assert self.rm.remove_strategy("nonexistent") is False

    def test_prioritize_suggestions_deduplication(self):
        suggestions = ["fix A", "fix A", "fix B"]
        result = self.rm._prioritize_suggestions(suggestions, [])
        assert result.count("fix A") == 1

    def test_prioritize_critical_first(self):
        suggestions = ["mention the function", "fix syntax error first"]
        result = self.rm._prioritize_suggestions(suggestions, [])
        assert result[0] == "fix syntax error first"

    def test_abstract_cannot_instantiate(self):
        import pytest

        with pytest.raises(TypeError):
            SuggestionStrategy()
