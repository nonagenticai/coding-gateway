"""Tests for services.code_graph.consistency_checker."""

from __future__ import annotations

from services.code_graph.consistency_checker import CodeConsistencyChecker
from services.code_graph.validation_utilities import validation_cache


class TestCodeConsistencyCheckerInit:
    def test_init(self):
        checker = CodeConsistencyChecker()
        assert checker.rules_engine is not None
        assert checker.error_detector is not None
        assert checker.error_reporter is not None
        assert checker.graph_validator is not None
        assert checker.recovery_mechanisms is not None
        assert checker.module_checker is not None
        assert checker.score_calculator is not None

    def test_default_config(self):
        checker = CodeConsistencyChecker()
        assert checker.config["enable_caching"] is True
        assert checker.config["fast_mode"] is False


class TestCheckFunctionDescription:
    def setup_method(self):
        self.checker = CodeConsistencyChecker()
        self.checker.config["enable_caching"] = False  # avoid cross-test pollution

    def test_empty_code(self):
        result = self.checker.check_function_description("", "some description")
        assert result["consistency_score"] == 0.0
        assert result["is_consistent"] is False
        assert "error" in result

    def test_empty_description(self):
        result = self.checker.check_function_description("def foo():\n    pass", "")
        assert result["consistency_score"] == 0.0
        assert result["is_consistent"] is False

    def test_whitespace_code(self):
        result = self.checker.check_function_description("   ", "desc")
        assert result["is_consistent"] is False

    def test_syntax_error(self):
        result = self.checker.check_function_description("def !!!", "desc")
        assert result["is_consistent"] is False
        assert "error" in result

    def test_no_function_def(self):
        result = self.checker.check_function_description("x = 1", "assigns x")
        assert result["is_consistent"] is False
        assert "error" in result

    def test_clean_function(self):
        code = "def greet(name):\n    return f'Hello {name}'"
        desc = "The greet function takes name and returns a greeting"
        result = self.checker.check_function_description(code, desc)
        assert result["consistency_score"] > 0
        assert "issues" in result
        assert "suggestions" in result
        assert "function_info" in result
        assert result["function_info"]["name"] == "greet"

    def test_context_public_api(self):
        code = "def process(data):\n    return data"
        desc = "The process function takes data and returns data"
        result = self.checker.check_function_description(code, desc, context="public_api")
        assert result["context"] == "public_api"

    def test_rules_summary(self):
        code = "def foo(x):\n    return x"
        desc = "The foo function takes x and returns it"
        result = self.checker.check_function_description(code, desc)
        assert "rules_summary" in result
        assert "total_rules" in result["rules_summary"]

    def test_caching_works(self):
        checker = CodeConsistencyChecker()
        checker.config["enable_caching"] = True
        code = "def foo():\n    return 1"
        desc = "The foo function returns 1"
        r1 = checker.check_function_description(code, desc)
        r2 = checker.check_function_description(code, desc)
        assert r1["consistency_score"] == r2["consistency_score"]
        checker.clear_cache()


class TestCheckClassDescription:
    def setup_method(self):
        self.checker = CodeConsistencyChecker()
        self.checker.config["enable_caching"] = False

    def test_empty_inputs(self):
        result = self.checker.check_class_description("", "desc")
        assert result["is_consistent"] is False

    def test_no_class_def(self):
        result = self.checker.check_class_description("x = 1", "some class")
        assert result["is_consistent"] is False

    def test_clean_class(self):
        code = "class Greeter:\n    def greet(self):\n        return 'hi'"
        desc = "The Greeter class has a greet method that returns a greeting"
        result = self.checker.check_class_description(code, desc)
        assert "class_info" in result
        assert result["class_name"] == "Greeter"
        assert "missing_aspects" in result
        assert "contradictions" in result

    def test_syntax_error(self):
        result = self.checker.check_class_description("class !!!", "desc")
        assert result["is_consistent"] is False


class TestCheckModuleConsistency:
    def setup_method(self):
        self.checker = CodeConsistencyChecker()
        self.checker.config["enable_caching"] = False

    def test_empty_modules(self):
        result = self.checker.check_module_consistency({})
        assert result["module_count"] == 0

    def test_single_module(self):
        modules = {"mod": "def foo():\n    pass"}
        result = self.checker.check_module_consistency(modules)
        assert result["module_count"] == 1
        assert result["parsed_modules"] == 1

    def test_unparseable_module(self):
        modules = {"bad": "def !!!"}
        result = self.checker.check_module_consistency(modules)
        assert result["parsed_modules"] == 0
        assert any(i.get("severity") == "critical" for i in result["issues"])

    def test_multiple_modules_with_deps(self):
        modules = {
            "a": "import b\ndef func_a():\n    pass",
            "b": "def func_b():\n    pass",
        }
        result = self.checker.check_module_consistency(modules)
        assert result["module_count"] == 2
        assert "naming_consistency" in result
        assert "import_consistency" in result
        assert "structural_consistency" in result

    def test_issues_by_type_and_severity(self):
        modules = {"a": "x = 1", "b": "y = 2"}
        result = self.checker.check_module_consistency(modules)
        assert "issues_by_type" in result
        assert "issues_by_severity" in result


class TestGenerateComprehensiveReport:
    def setup_method(self):
        self.checker = CodeConsistencyChecker()

    def test_empty_results(self):
        result = self.checker.generate_comprehensive_report([])
        assert "error" in result

    def test_with_results(self):
        analysis = [
            {
                "consistency_score": 0.9,
                "is_consistent": True,
                "issues": [
                    {"type": "naming", "severity": "low", "description": "x", "category": "y"}
                ],
            },
            {
                "consistency_score": 0.5,
                "is_consistent": False,
                "issues": [
                    {
                        "type": "syntax",
                        "severity": "critical",
                        "description": "error",
                        "category": "z",
                    }
                ],
            },
        ]
        report = self.checker.generate_comprehensive_report(analysis)
        assert "summary" in report
        assert "recovery_plan" in report
        assert "overall_metrics" in report
        assert report["overall_metrics"]["total_analyses"] == 2
        assert report["overall_metrics"]["analyses_passed"] == 1


class TestUtilityMethods:
    def setup_method(self):
        self.checker = CodeConsistencyChecker()

    def test_clear_cache(self):
        validation_cache.set("test_key", "value")
        self.checker.clear_cache()
        assert validation_cache.get("test_key") is None

    def test_get_cache_stats(self):
        stats = self.checker.get_cache_stats()
        assert "cache_size" in stats
        assert "max_size" in stats

    def test_configure(self):
        self.checker.configure(fast_mode=True, enable_caching=False)
        assert self.checker.config["fast_mode"] is True
        assert self.checker.config["enable_caching"] is False

    def test_get_available_rules(self):
        rules = self.checker.get_available_rules()
        assert "parameter_consistency" in rules

    def test_add_custom_rule(self):
        from services.code_graph.consistency_rules import ConsistencyRule

        class Dummy(ConsistencyRule):
            def check(self, code, desc, ctx):
                return []

            def get_rule_name(self):
                return "custom"

            def get_severity(self):
                return "low"

        self.checker.add_custom_rule(Dummy())
        assert "custom" in self.checker.get_available_rules()

    def test_add_custom_suggestion_strategy(self):
        from services.code_graph.recovery_mechanisms import SuggestionStrategy

        class Dummy(SuggestionStrategy):
            def generate(self, issues, ctx):
                return []

            def get_strategy_name(self):
                return "custom_strat"

            def get_applicable_types(self):
                return []

        self.checker.add_custom_suggestion_strategy(Dummy())
        assert "custom_strat" in self.checker.recovery_mechanisms.get_available_strategies()

    def test_create_error_result(self):
        result = self.checker._create_error_result("oops", 42, "internal")
        assert result["consistency_score"] == 0.0
        assert result["error"] == "oops"
        assert result["analysis_time_ms"] == 42


class TestModuleConsistencyScoreClamping:
    """Regression test: consistency scores must be clamped to [0.0, 1.0]."""

    def test_many_naming_issues_score_does_not_go_negative(self):
        checker = CodeConsistencyChecker()
        # Create modules with 15+ naming convention violations to trigger the bug
        # where 1.0 - (15 * 0.1) = -0.5 before fix
        modules = {}
        paths = {}
        for i in range(15):
            mod = f"mod_{i}"
            # Mix camelCase and snake_case to trigger naming inconsistencies
            if i % 2 == 0:
                code = f"def camelCase{i}(): pass\ndef snake_case_{i}(): pass"
            else:
                code = f"class lowercase{i}: pass"
            modules[mod] = code
            paths[mod] = f"/project/{mod}.py"

        result = checker.check_module_consistency(modules, paths)
        # Scores must never go negative
        if "naming_consistency" in result:
            assert result["naming_consistency"] >= 0.0, (
                f"naming_consistency went negative: {result['naming_consistency']}"
            )
        if "import_consistency" in result:
            assert result["import_consistency"] >= 0.0
        if "structural_consistency" in result:
            assert result["structural_consistency"] >= 0.0
