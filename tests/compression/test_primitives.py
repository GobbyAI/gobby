"""Tests for compression primitives."""

import pytest

from gobby.compression.primitives import dedup, filter_lines, group_lines, truncate

pytestmark = pytest.mark.unit


class TestFilterLines:
    def test_removes_matching_lines(self) -> None:
        lines = ["hello\n", "  \n", "world\n", "\n", "foo\n"]
        result = filter_lines(lines, patterns=[r"^\s*$"])
        assert result == ["hello\n", "world\n", "foo\n"]

    def test_multiple_patterns(self) -> None:
        lines = ["On branch main\n", "M file.py\n", "Your branch is up\n", "?? new.py\n"]
        result = filter_lines(lines, patterns=[r"^On branch", r"^Your branch"])
        assert result == ["M file.py\n", "?? new.py\n"]

    def test_no_match_preserves_all(self) -> None:
        lines = ["a\n", "b\n", "c\n"]
        result = filter_lines(lines, patterns=[r"^zzz"])
        assert result == lines

    def test_empty_input(self) -> None:
        assert filter_lines([], patterns=[r".*"]) == []


class TestTruncate:
    def test_short_input_unchanged(self) -> None:
        lines = ["a\n", "b\n", "c\n"]
        result = truncate(lines, head=5, tail=5)
        assert result == lines

    def test_truncates_middle(self) -> None:
        lines = [f"line{i}\n" for i in range(50)]
        result = truncate(lines, head=5, tail=5)
        assert len(result) == 11  # 5 head + 1 marker + 5 tail
        assert "line0\n" in result[0]
        assert "line49\n" in result[-1]
        assert "40 lines omitted" in result[5]

    def test_exact_boundary(self) -> None:
        lines = [f"line{i}\n" for i in range(10)]
        result = truncate(lines, head=5, tail=5)
        assert result == lines  # exactly head+tail, no truncation


class TestDedup:
    def test_collapses_identical_lines(self) -> None:
        lines = ["error: foo\n"] * 5
        result = dedup(lines)
        assert len(result) == 2  # one line + count
        assert "repeated 5 times" in result[1]

    def test_collapses_near_identical(self) -> None:
        lines = [
            "Processing file 1 of 100\n",
            "Processing file 2 of 100\n",
            "Processing file 3 of 100\n",
            "Done\n",
        ]
        result = dedup(lines)
        assert len(result) <= 3  # collapsed + count + Done
        assert any("repeated" in line for line in result)

    def test_no_duplicates_unchanged(self) -> None:
        lines = ["a\n", "b\n", "c\n"]
        result = dedup(lines)
        assert result == lines

    def test_empty_input(self) -> None:
        assert dedup([]) == []


class TestGroupLines:
    def test_git_status_groups_by_status(self) -> None:
        lines = [
            " M src/foo.py\n",
            " M src/bar.py\n",
            "?? new_file.py\n",
        ]
        result = group_lines(lines, mode="git_status")
        text = "".join(result)
        assert "Modified" in text
        assert "Untracked" in text

    def test_lint_by_rule_groups(self) -> None:
        lines = [
            "src/foo.py:10:5: E501 line too long\n",
            "src/bar.py:20:1: E501 line too long\n",
            "src/baz.py:5:1: F401 unused import\n",
        ]
        result = group_lines(lines, mode="lint_by_rule")
        text = "".join(result)
        assert "E501" in text
        assert "F401" in text
        assert "2 occurrences" in text

    def test_unknown_mode_passthrough(self) -> None:
        lines = ["a\n", "b\n"]
        result = group_lines(lines, mode="nonexistent_mode")
        assert result == lines

    def test_test_failures_extracts_failures(self) -> None:
        lines = [
            "test_foo ... ok\n",
            "test_bar ... ok\n",
            "FAILED test_baz\n",
            "  AssertionError: 1 != 2\n",
            "1 passed, 1 failed\n",
        ]
        result = group_lines(lines, mode="test_failures")
        text = "".join(result)
        assert "FAILED" in text

    def test_pytest_failures_extracts_section(self) -> None:
        lines = [
            "collected 5 items\n",
            "tests/test_a.py .....PASSED\n",
            "tests/test_b.py .F.\n",
            "======= FAILURES =======\n",
            "_______ test_thing _______\n",
            "    assert 1 == 2\n",
            "======= short test summary =======\n",
            "FAILED tests/test_b.py::test_thing\n",
            "======= 1 failed, 4 passed =======\n",
        ]
        result = group_lines(lines, mode="pytest_failures")
        text = "".join(result)
        assert "FAILURES" in text
        assert "assert 1 == 2" in text
        assert "1 failed" in text

    def test_by_file_groups_grep_output(self) -> None:
        lines = [
            "src/foo.py:10:match1\n",
            "src/foo.py:20:match2\n",
            "src/bar.py:5:match3\n",
        ]
        result = group_lines(lines, mode="by_file")
        text = "".join(result)
        assert "src/foo.py (2 matches)" in text
        assert "src/bar.py (1 match)" in text

    def test_errors_warnings_separates(self) -> None:
        lines = [
            "error: undefined variable\n",
            "warning: unused import\n",
            "error: type mismatch\n",
            "Build complete.\n",
        ]
        result = group_lines(lines, mode="errors_warnings")
        text = "".join(result)
        assert "Errors (2)" in text
        assert "Warnings (1)" in text
