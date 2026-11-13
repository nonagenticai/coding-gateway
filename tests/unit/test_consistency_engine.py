"""Tests for services.code_graph.consistency_rules_engine — engine and module checker."""

from __future__ import annotations

import ast

from services.code_graph.consistency_rules import ConsistencyRule
from services.code_graph.consistency_rules_engine import (
    ConsistencyRulesEngine,
    ModuleConsistencyChecker,
)

# ---------------------------------------------------------------------------
# ConsistencyRulesEngine
# ---------------------------------------------------------------------------


class TestConsistencyRulesEngine:
    def setup_method(self):
        self.engine = ConsistencyRulesEngine()

    # -- basic attributes --
    def test_default_rules_loaded(self):
        names = self.engine.get_enabled_rules()
        assert "parameter_consistency" in names
        assert "return_consistency" in names
        assert "exception_consistency" in names
        assert "logic_operation_consistency" in names
        assert "naming_consistency" in names
        assert len(names) == 5

    # -- check_consistency: all rules pass --
    def test_all_pass_clean_code(self):
        code = "def greet(name):\n    return f'Hello {name}'"
        desc = "The greet function takes name and returns a greeting"
        result = self.engine.check_consistency(code, desc)
        assert result["consistency_score"] == 1.0
        assert result["is_consistent"] is True
        assert result["total_issues"] == 0
        assert result["rules_passed"] == result["total_rules"]

    # -- aggregates violations --
    def test_aggregates_violations(self):
        code = "def compute(x, y, z):\n    return x + y + z"
        desc = "Adds numbers"
        result = self.engine.check_consistency(code, desc)
        assert result["total_issues"] > 0
        assert result["is_consistent"] is False or result["consistency_score"] < 1.0
        assert "all_issues" in result
        assert isinstance(result["issues_by_severity"], dict)

    # -- empty code --
    def test_empty_code(self):
        result = self.engine.check_consistency("", "anything")
        # No function node → no issues from most rules
        assert result["total_issues"] == 0

    # -- scoring --
    def test_score_no_issues(self):
        score = self.engine._calculate_consistency_score([])
        assert score == 1.0

    def test_score_with_high_issue(self):
        issues = [{"severity": "high"}]
        score = self.engine._calculate_consistency_score(issues)
        assert score < 1.0

    def test_score_clamped_to_zero(self):
        issues = [{"severity": "high"}] * 50
        score = self.engine._calculate_consistency_score(issues)
        assert score == 0.0

    # -- severity grouping --
    def test_group_issues_by_severity(self):
        issues = [
            {"severity": "high"},
            {"severity": "low"},
            {"severity": "low"},
            {"severity": "medium"},
        ]
        grouped = self.engine._group_issues_by_severity(issues)
        assert grouped == {"high": 1, "medium": 1, "low": 2}

    def test_group_unknown_severity_ignored(self):
        issues = [{"severity": "extreme"}]
        grouped = self.engine._group_issues_by_severity(issues)
        assert grouped == {"high": 0, "medium": 0, "low": 0}

    # -- add / remove rules --
    def test_add_rule(self):
        class DummyRule(ConsistencyRule):
            def check(self, code, description, context):
                return []

            def get_rule_name(self):
                return "dummy"

            def get_severity(self):
                return "low"

        self.engine.add_rule(DummyRule())
        assert "dummy" in self.engine.get_enabled_rules()

    def test_remove_rule(self):
        removed = self.engine.remove_rule("parameter_consistency")
        assert removed is True
        assert "parameter_consistency" not in self.engine.get_enabled_rules()

    def test_remove_nonexistent_rule(self):
        removed = self.engine.remove_rule("nonexistent")
        assert removed is False

    # -- enable / disable --
    def test_enable_existing_rule(self):
        assert self.engine.enable_rule("parameter_consistency") is True

    def test_enable_nonexistent_rule(self):
        assert self.engine.enable_rule("nonexistent") is False

    def test_disable_rule(self):
        assert self.engine.disable_rule("parameter_consistency") is True
        assert "parameter_consistency" not in self.engine.get_enabled_rules()

    # -- get_rule_info --
    def test_get_rule_info_exists(self):
        info = self.engine.get_rule_info("parameter_consistency")
        assert info["name"] == "parameter_consistency"
        assert "severity" in info
        assert "class" in info

    def test_get_rule_info_missing(self):
        info = self.engine.get_rule_info("nonexistent")
        assert info == {}

    # -- rule execution error handling --
    def test_rule_execution_error(self):
        class BrokenRule(ConsistencyRule):
            def check(self, code, description, context):
                raise RuntimeError("boom")

            def get_rule_name(self):
                return "broken"

            def get_severity(self):
                return "high"

        self.engine.rules = [BrokenRule()]
        result = self.engine.check_consistency("def x():\n    pass", "desc")
        assert result["rule_results"]["broken"]["passed"] is False
        assert "error" in result["rule_results"]["broken"]


# ---------------------------------------------------------------------------
# ModuleConsistencyChecker
# ---------------------------------------------------------------------------


class TestModuleConsistencyChecker:
    def setup_method(self):
        self.checker = ModuleConsistencyChecker()

    def test_naming_conventions_all_snake_case(self):
        tree = ast.parse("def hello_world():\n    pass\ndef foo_bar():\n    pass")
        issues = self.checker.check_naming_conventions({"mod": tree})
        # All snake_case → no mixed issue
        assert not any("Inconsistent function naming within" in i["description"] for i in issues)

    def test_naming_conventions_mixed_in_module(self):
        code = "def hello_world():\n    pass\ndef helloWorld():\n    pass"
        tree = ast.parse(code)
        issues = self.checker.check_naming_conventions({"mod": tree})
        assert any("Inconsistent function naming within" in i["description"] for i in issues)

    def test_naming_conventions_across_modules(self):
        snake_tree = ast.parse("def hello_world():\n    pass")
        camel_tree = ast.parse("def helloWorld():\n    pass")
        issues = self.checker.check_naming_conventions({"a": snake_tree, "b": camel_tree})
        assert any("across modules" in i["description"] for i in issues)

    def test_class_naming_non_pascal(self):
        tree = ast.parse("class my_class:\n    pass")
        issues = self.checker.check_naming_conventions({"mod": tree})
        assert any("Non-PascalCase" in i["description"] for i in issues)

    def test_import_patterns_consistent(self):
        tree = ast.parse("import os\nimport sys")
        issues = self.checker.check_import_patterns({"mod": tree})
        assert issues == []

    def test_import_patterns_mixed(self):
        code = "import os\nfrom . import sibling"
        tree = ast.parse(code)
        issues = self.checker.check_import_patterns({"mod": tree})
        assert any("Mixed import styles" in i["description"] for i in issues)

    def test_empty_modules(self):
        issues = self.checker.check_naming_conventions({})
        assert issues == []
        issues = self.checker.check_import_patterns({})
        assert issues == []
