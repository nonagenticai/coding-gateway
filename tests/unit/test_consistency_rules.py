"""Tests for services.code_graph.consistency_rules — each rule pass/fail, malformed data, empty graph."""

from __future__ import annotations

from services.code_graph.consistency_rules import (
    ConsistencyRule,
    ExceptionConsistencyRule,
    LogicOperationConsistencyRule,
    NamingConsistencyRule,
    ParameterConsistencyRule,
    ReturnConsistencyRule,
)

# ---------------------------------------------------------------------------
# ParameterConsistencyRule
# ---------------------------------------------------------------------------


class TestParameterConsistencyRule:
    def setup_method(self):
        self.rule = ParameterConsistencyRule()

    def test_rule_name(self):
        assert self.rule.get_rule_name() == "parameter_consistency"

    def test_severity(self):
        assert self.rule.get_severity() == "medium"

    def test_pass_when_params_mentioned(self):
        code = "def greet(name, age):\n    return f'{name} is {age}'"
        desc = "Takes name and age, returns greeting"
        issues = self.rule.check(code, desc, {})
        assert issues == []

    def test_fail_when_param_not_mentioned(self):
        code = "def greet(name, age):\n    return f'{name}'"
        desc = "Takes name, returns greeting"
        issues = self.rule.check(code, desc, {})
        assert len(issues) == 1
        assert issues[0]["parameter_name"] == "age"
        assert issues[0]["type"] == "parameter"

    def test_no_function_returns_empty(self):
        code = "x = 1"
        issues = self.rule.check(code, "assigns a value", {})
        assert issues == []

    def test_no_params_returns_empty(self):
        code = "def hello():\n    pass"
        issues = self.rule.check(code, "says hello", {})
        assert issues == []

    def test_self_excluded(self):
        code = "def greet(self, name):\n    return name"
        desc = "Takes name and returns it"
        issues = self.rule.check(code, desc, {})
        assert issues == []

    def test_syntax_error_code(self):
        code = "def greet(name:"
        issues = self.rule.check(code, "desc", {})
        assert len(issues) == 1
        assert issues[0]["type"] == "syntax"
        assert issues[0]["severity"] == "high"


# ---------------------------------------------------------------------------
# ReturnConsistencyRule
# ---------------------------------------------------------------------------


class TestReturnConsistencyRule:
    def setup_method(self):
        self.rule = ReturnConsistencyRule()

    def test_rule_name(self):
        assert self.rule.get_rule_name() == "return_consistency"

    def test_severity(self):
        assert self.rule.get_severity() == "medium"

    def test_pass_return_mentioned(self):
        code = "def add(a, b):\n    return a + b"
        desc = "Adds two numbers and returns the sum"
        issues = self.rule.check(code, desc, {})
        # No issue about "returns values" since "return" is in desc
        assert not any("returns values" in i.get("description", "") for i in issues)

    def test_fail_return_not_mentioned(self):
        code = "def add(a, b):\n    return a + b"
        desc = "Adds two numbers"
        issues = self.rule.check(code, desc, {})
        assert any("returns values" in i["description"] for i in issues)

    def test_return_type_not_mentioned(self):
        code = "def add(a, b) -> int:\n    return a + b"
        desc = "Adds two numbers and returns the sum"
        issues = self.rule.check(code, desc, {})
        assert any("Return type" in i["description"] for i in issues)

    def test_return_type_mentioned(self):
        code = "def add(a, b) -> int:\n    return a + b"
        desc = "Adds two numbers and returns an int"
        issues = self.rule.check(code, desc, {})
        assert not any("Return type" in i.get("description", "") for i in issues)

    def test_no_function(self):
        code = "x = 1"
        issues = self.rule.check(code, "assigns x", {})
        assert issues == []

    def test_no_return_value(self):
        code = "def noop():\n    pass"
        issues = self.rule.check(code, "does nothing", {})
        assert issues == []


# ---------------------------------------------------------------------------
# ExceptionConsistencyRule
# ---------------------------------------------------------------------------


class TestExceptionConsistencyRule:
    def setup_method(self):
        self.rule = ExceptionConsistencyRule()

    def test_rule_name(self):
        assert self.rule.get_rule_name() == "exception_consistency"

    def test_severity(self):
        assert self.rule.get_severity() == "low"

    def test_pass_exception_handling_mentioned(self):
        code = "def load():\n    try:\n        open('f')\n    except IOError:\n        pass"
        desc = "Loads a file, handles IOError exception"
        issues = self.rule.check(code, desc, {})
        # The word "exception" is in desc, so no generic issue
        assert not any("handles exceptions" in i.get("description", "") for i in issues)

    def test_fail_exception_handling_not_mentioned(self):
        code = "def load():\n    try:\n        open('f')\n    except IOError:\n        pass"
        desc = "Loads a file"
        issues = self.rule.check(code, desc, {})
        assert any("handles exceptions" in i["description"] for i in issues)

    def test_specific_exception_not_mentioned(self):
        code = "def load():\n    try:\n        open('f')\n    except ValueError:\n        pass"
        desc = "Loads a file, handles errors"
        issues = self.rule.check(code, desc, {})
        assert any("ValueError" in i.get("description", "") for i in issues)

    def test_no_try_except(self):
        code = "def add(a, b):\n    return a + b"
        desc = "Adds numbers"
        issues = self.rule.check(code, desc, {})
        assert issues == []


# ---------------------------------------------------------------------------
# LogicOperationConsistencyRule
# ---------------------------------------------------------------------------


class TestLogicOperationConsistencyRule:
    def setup_method(self):
        self.rule = LogicOperationConsistencyRule()

    def test_rule_name(self):
        assert self.rule.get_rule_name() == "logic_operation_consistency"

    def test_severity(self):
        assert self.rule.get_severity() == "medium"

    def test_database_op_detected(self):
        code = "def save(data):\n    connection.execute(query)"
        desc = "Saves data"
        issues = self.rule.check(code, desc, {})
        assert any(i.get("operation_type") == "database" for i in issues)

    def test_database_op_mentioned(self):
        code = "def save(data):\n    connection.execute(query)"
        desc = "Saves data to the database"
        issues = self.rule.check(code, desc, {})
        assert not any(i.get("operation_type") == "database" for i in issues)

    def test_file_op_detected(self):
        code = "def process():\n    f = open('data.json')\n    return f.read()"
        desc = "Processes data from somewhere"
        issues = self.rule.check(code, desc, {})
        assert any(i.get("operation_type") == "file" for i in issues)

    def test_no_operations_no_issues(self):
        code = "def add(a, b):\n    return a + b"
        desc = "Adds numbers"
        issues = self.rule.check(code, desc, {})
        assert issues == []

    def test_recursion_detected(self):
        code = (
            "def factorial(n):\n    if n <= 1:\n        return 1\n    return n * factorial(n - 1)"
        )
        desc = "Calculates factorial"
        issues = self.rule.check(code, desc, {})
        assert any(i.get("operation_type") == "recursion" for i in issues)

    def test_recursion_mentioned(self):
        code = (
            "def factorial(n):\n    if n <= 1:\n        return 1\n    return n * factorial(n - 1)"
        )
        desc = "Recursively calculates factorial"
        issues = self.rule.check(code, desc, {})
        assert not any(i.get("operation_type") == "recursion" for i in issues)


# ---------------------------------------------------------------------------
# NamingConsistencyRule
# ---------------------------------------------------------------------------


class TestNamingConsistencyRule:
    def setup_method(self):
        self.rule = NamingConsistencyRule()

    def test_rule_name(self):
        assert self.rule.get_rule_name() == "naming_consistency"

    def test_severity(self):
        assert self.rule.get_severity() == "low"

    def test_pass_name_mentioned(self):
        code = "def greet():\n    pass"
        desc = "The greet function says hello"
        issues = self.rule.check(code, desc, {})
        assert issues == []

    def test_fail_function_name_not_mentioned(self):
        code = "def greet():\n    pass"
        desc = "Says hello"
        issues = self.rule.check(code, desc, {})
        assert len(issues) == 1
        assert issues[0]["entity_name"] == "greet"
        assert issues[0]["entity_type"] == "function"

    def test_class_name_not_mentioned(self):
        code = "class UserManager:\n    pass"
        desc = "Manages users"
        issues = self.rule.check(code, desc, {})
        assert len(issues) == 1
        assert issues[0]["entity_name"] == "UserManager"
        assert issues[0]["entity_type"] == "class"

    def test_class_name_mentioned(self):
        code = "class UserManager:\n    pass"
        desc = "UserManager manages users"
        issues = self.rule.check(code, desc, {})
        assert issues == []

    def test_syntax_error_returns_empty(self):
        code = "def !!!invalid"
        issues = self.rule.check(code, "desc", {})
        assert issues == []


# ---------------------------------------------------------------------------
# Abstract base class
# ---------------------------------------------------------------------------


class TestConsistencyRuleABC:
    def test_cannot_instantiate_abstract(self):
        import pytest

        with pytest.raises(TypeError):
            ConsistencyRule()
