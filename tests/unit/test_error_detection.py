"""Tests for services.code_graph.error_detection_reporting."""

from __future__ import annotations

import ast

from services.code_graph.error_detection_reporting import ErrorDetector, ErrorReporter

# ---------------------------------------------------------------------------
# ErrorDetector
# ---------------------------------------------------------------------------


class TestErrorDetectorSyntax:
    def setup_method(self):
        self.detector = ErrorDetector()

    def test_empty_code(self):
        errors = self.detector.detect_syntax_errors("")
        assert len(errors) == 1
        assert errors[0]["category"] == "empty_code"
        assert errors[0]["severity"] == "high"

    def test_whitespace_only(self):
        errors = self.detector.detect_syntax_errors("   \n  ")
        assert len(errors) == 1
        assert errors[0]["category"] == "empty_code"

    def test_valid_code(self):
        errors = self.detector.detect_syntax_errors("x = 1")
        assert errors == []

    def test_syntax_error(self):
        errors = self.detector.detect_syntax_errors("def !!!")
        assert len(errors) == 1
        assert errors[0]["category"] == "parse_error"
        assert errors[0]["severity"] == "critical"

    def test_context_passed_through(self):
        ctx = {"file": "test.py"}
        errors = self.detector.detect_syntax_errors("", context=ctx)
        assert errors[0]["context"] == ctx


class TestErrorDetectorStructural:
    def setup_method(self):
        self.detector = ErrorDetector()

    def test_no_definitions(self):
        tree = ast.parse("x = 1")
        errors = self.detector.detect_structural_errors(tree)
        assert any(e["category"] == "no_definitions" for e in errors)

    def test_has_function(self):
        tree = ast.parse("def foo():\n    pass")
        errors = self.detector.detect_structural_errors(tree)
        assert not any(e["category"] == "no_definitions" for e in errors)

    def test_unreachable_code(self):
        code = "def foo():\n    return 1\n    x = 2"
        tree = ast.parse(code)
        errors = self.detector.detect_structural_errors(tree)
        assert any(e["category"] == "unreachable_code" for e in errors)

    def test_no_unreachable_code(self):
        code = "def foo():\n    x = 2\n    return x"
        tree = ast.parse(code)
        errors = self.detector.detect_structural_errors(tree)
        assert not any(e["category"] == "unreachable_code" for e in errors)


class TestErrorDetectorConsistency:
    def setup_method(self):
        self.detector = ErrorDetector()

    def test_empty_description(self):
        errors = self.detector.detect_consistency_errors("x = 1", "")
        assert any(e["category"] == "missing_description" for e in errors)

    def test_function_name_missing(self):
        errors = self.detector.detect_consistency_errors(
            "def process(data):\n    return data", "handles stuff"
        )
        assert any(e["category"] == "missing_function_name" for e in errors)

    def test_function_name_present(self):
        errors = self.detector.detect_consistency_errors(
            "def process(data):\n    return data", "process handles data and returns it"
        )
        assert not any(e["category"] == "missing_function_name" for e in errors)

    def test_missing_return_info(self):
        errors = self.detector.detect_consistency_errors(
            "def calc(x):\n    return x * 2", "calc multiplies x"
        )
        assert any(e["category"] == "missing_return_info" for e in errors)

    def test_class_name_missing(self):
        errors = self.detector.detect_consistency_errors(
            "class Foo:\n    def bar(self):\n        pass",
            "manages things",
        )
        assert any(e["category"] == "missing_class_name" for e in errors)

    def test_class_methods_missing(self):
        errors = self.detector.detect_consistency_errors(
            "class Foo:\n    def bar(self):\n        pass",
            "Foo manages things",
        )
        assert any(e["category"] == "missing_methods" for e in errors)

    def test_functionality_mismatch(self):
        errors = self.detector.detect_consistency_errors(
            "def add(a, b):\n    return a + b",
            "add queries the database and returns results",
        )
        assert any(e["category"] == "functionality_mismatch" for e in errors)

    def test_syntax_error_code(self):
        errors = self.detector.detect_consistency_errors("def !!!", "stuff")
        assert any(e["category"] == "unparseable_code" for e in errors)

    def test_missing_parameters(self):
        errors = self.detector.detect_consistency_errors(
            "def calc(x, y):\n    return x + y", "calc does math"
        )
        assert any(e["category"] == "missing_parameters" for e in errors)


class TestErrorDetectorLogic:
    def setup_method(self):
        self.detector = ErrorDetector()

    def test_infinite_loop(self):
        code = "def run():\n    while True:\n        pass"
        tree = ast.parse(code)
        errors = self.detector.detect_logic_errors(tree)
        assert any(e["category"] == "infinite_loop" for e in errors)

    def test_while_true_with_break(self):
        code = "def run():\n    while True:\n        break"
        tree = ast.parse(code)
        errors = self.detector.detect_logic_errors(tree)
        assert not any(e["category"] == "infinite_loop" for e in errors)

    def test_unused_variable(self):
        code = "def foo():\n    unused = 42\n    return 1"
        tree = ast.parse(code)
        errors = self.detector.detect_logic_errors(tree)
        assert any(e["category"] == "unused_variable" for e in errors)

    def test_underscore_var_not_flagged(self):
        code = "def foo():\n    _unused = 42\n    return 1"
        tree = ast.parse(code)
        errors = self.detector.detect_logic_errors(tree)
        assert not any(
            e["category"] == "unused_variable" and e.get("variable_name") == "_unused"
            for e in errors
        )

    def test_no_logic_errors(self):
        code = "def foo(x):\n    return x + 1"
        tree = ast.parse(code)
        errors = self.detector.detect_logic_errors(tree)
        # May be empty or only trivial
        assert not any(e["category"] == "infinite_loop" for e in errors)


# ---------------------------------------------------------------------------
# ErrorReporter
# ---------------------------------------------------------------------------


class TestErrorReporter:
    def setup_method(self):
        self.reporter = ErrorReporter()

    def test_summary_empty(self):
        summary = self.reporter.generate_summary_report([])
        assert summary["total_errors"] == 0
        assert summary["severity_score"] == 1.0

    def test_summary_with_errors(self):
        errors = [
            {"severity": "critical", "type": "syntax", "category": "parse_error"},
            {"severity": "low", "type": "logic", "category": "unused_variable"},
        ]
        summary = self.reporter.generate_summary_report(errors)
        assert summary["total_errors"] == 2
        assert summary["errors_by_severity"]["critical"] == 1
        assert summary["errors_by_severity"]["low"] == 1
        assert summary["severity_score"] < 1.0

    def test_summary_most_common(self):
        errors = [
            {"severity": "low", "type": "logic", "category": "unused_variable"},
            {"severity": "low", "type": "logic", "category": "unused_variable"},
            {"severity": "medium", "type": "structural", "category": "empty_function"},
        ]
        summary = self.reporter.generate_summary_report(errors)
        assert summary["most_common_issues"][0][0] == "unused_variable"

    def test_format_error_details(self):
        errors = [
            {
                "severity": "high",
                "description": "Something wrong",
                "type": "syntax",
                "category": "parse_error",
                "line": 5,
                "column": 10,
                "function_name": "foo",
                "context": {"file": "test.py"},
            }
        ]
        formatted = self.reporter.format_error_details(errors)
        assert len(formatted) == 1
        assert "[HIGH]" in formatted[0]
        assert "line 5" in formatted[0]
        assert "foo" in formatted[0]

    def test_format_error_no_context(self):
        errors = [{"severity": "low", "description": "minor issue"}]
        formatted = self.reporter.format_error_details(errors, include_context=False)
        assert len(formatted) == 1
        assert "Context" not in formatted[0]

    def test_format_error_class_name(self):
        errors = [{"severity": "low", "description": "issue", "class_name": "MyClass"}]
        formatted = self.reporter.format_error_details(errors)
        assert "MyClass" in formatted[0]

    def test_filter_by_severity(self):
        errors = [
            {"severity": "critical"},
            {"severity": "high"},
            {"severity": "medium"},
            {"severity": "low"},
        ]
        filtered = self.reporter.filter_errors_by_severity(errors, "high")
        assert len(filtered) == 2  # critical + high

    def test_filter_by_severity_low(self):
        errors = [{"severity": "low"}, {"severity": "high"}]
        filtered = self.reporter.filter_errors_by_severity(errors, "low")
        assert len(filtered) == 2

    def test_group_errors_by_type(self):
        errors = [
            {"type": "syntax"},
            {"type": "logic"},
            {"type": "syntax"},
        ]
        grouped = self.reporter.group_errors_by_type(errors)
        assert len(grouped["syntax"]) == 2
        assert len(grouped["logic"]) == 1

    def test_export_errors_json(self):
        errors = [{"severity": "low", "type": "logic", "category": "x"}]
        export = self.reporter.export_errors_json(errors)
        assert "errors" in export
        assert "summary" in export
        assert "metadata" in export
        assert export["metadata"]["total_count"] == 1

    def test_export_errors_no_metadata(self):
        export = self.reporter.export_errors_json([], include_metadata=False)
        assert "metadata" not in export


class TestLogicAnalysisFailureSeverity:
    """Regression: logic analysis failure should be medium severity, not low."""

    def test_logic_analysis_error_is_medium_severity(self):
        detector = ErrorDetector()
        # Feed code that will trigger an exception during logic analysis
        # by making the internal analysis fail
        from unittest.mock import patch

        with patch.object(detector, "_detect_unreachable_code", side_effect=RuntimeError("boom")):
            errors = detector.detect_logic_errors("def f():\n    return 1\n")
        # The error from the exception should be severity "medium"
        analysis_errors = [e for e in errors if e["category"] == "analysis_error"]
        assert len(analysis_errors) == 1
        assert analysis_errors[0]["severity"] == "medium"
