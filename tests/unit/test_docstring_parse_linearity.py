"""Regression tests for the py/polynomial-redos highs (alerts 8-10).

``_header_word`` / ``_section_entry_name`` replaced the three anchored regexes
that CodeQL flagged in ``routers/validation.py``. They are single left-to-right
scans, so a hostile docstring (megabytes of spaces after ``Args``/``Raises``,
or a star/space run) must parse in linear time.
"""

from __future__ import annotations

import time

from routers.validation import (
    _described_parameters,
    _header_word,
    _section_entry_name,
)

# 200k characters per line x 5 lines = ~1 MB. A quadratic scan at this size is
# minutes; a linear scan is milliseconds. The 1.0 s bound therefore proves the
# absence of polynomial backtracking, not merely "fast on this machine".
_ADVERSARIAL_UNITS = (
    " " * 200_000,
    "Args" + " " * 200_000,
    "Raises" + " " * 200_000,
    "*" * 200_000,
    "x" * 200_000,
)


def test_adversarial_description_parses_within_a_linear_time_budget():
    description = "\n".join(_ADVERSARIAL_UNITS)
    start = time.perf_counter()
    names = _described_parameters(description)
    elapsed = time.perf_counter() - start
    assert names == []
    assert elapsed < 1.0, f"adversarial parse took {elapsed:.3f}s (expected linear, < 1s)"


def test_header_word_is_linear_on_a_single_long_line():
    line = "Raises" + " " * 500_000
    start = time.perf_counter()
    word = _header_word(line)
    elapsed = time.perf_counter() - start
    assert word == "Raises"
    assert elapsed < 0.5, f"_header_word took {elapsed:.3f}s"


def test_section_entry_scan_is_linear_on_a_single_long_line():
    line = "*" * 2 + "name" + "(" + "x" * 500_000 + "): description"
    start = time.perf_counter()
    name = _section_entry_name(line)
    elapsed = time.perf_counter() - start
    assert name == "name"
    assert elapsed < 0.5, f"_section_entry_name took {elapsed:.3f}s"


def test_header_parsing_keeps_the_original_regex_semantics():
    assert _header_word("Args:") == "Args"
    assert _header_word("  Parameters  :  ") == "Parameters"
    assert _header_word("\tParams\t") == "Params"
    assert _header_word("Returns") == "Returns"
    assert _header_word("See Also:") == "See Also"
    # Not headers: trailing prose, two colons, empty, ordinary words.
    assert _header_word("Args: the parameters") == "Args: the parameters"
    assert _header_word("Args::") == "Args:"
    assert _header_word("") == ""
    assert _header_word("args") == "args"


def test_section_entry_parsing_keeps_the_original_regex_semantics():
    assert _section_entry_name("    alpha (int): first") == "alpha"
    assert _section_entry_name("    *beta (str): second") == "beta"
    assert _section_entry_name("    **gamma: third") == "gamma"
    assert _section_entry_name("\t**_delta: fourth") == "_delta"
    # Not entries: three stars, a space between the star and the name, digit
    # first, parenthesised name without colon, an unclosed parenthesis, and a
    # bare value line (all match the previous regex's behaviour exactly).
    assert _section_entry_name("*** epsilon: nope") is None
    assert _section_entry_name("    * beta: nope") is None
    assert _section_entry_name("    1thing: nope") is None
    assert _section_entry_name("    name (int) no colon") is None
    assert _section_entry_name("    name (unclosed: nope") is None
    assert _section_entry_name("    just prose") is None


def test_numpydoc_section_still_yields_parameter_names():
    description = """Does something useful.

    Args:
        alpha (int): first
        *beta (str): second
        **gamma: third
            Default: 5

    Returns:
        value: ignored

    :param delta: cue form
    """
    assert _described_parameters(description) == ["delta", "alpha", "beta", "gamma"]
