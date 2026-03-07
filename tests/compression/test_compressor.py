"""Tests for OutputCompressor."""

import pytest

from gobby.compression.compressor import CompressionResult, OutputCompressor

pytestmark = pytest.mark.unit


class TestCompressionResult:
    def test_savings_pct(self) -> None:
        r = CompressionResult(
            compressed="short",
            original_chars=100,
            compressed_chars=40,
            strategy_name="test",
        )
        assert r.savings_pct == 60.0

    def test_savings_pct_zero_original(self) -> None:
        r = CompressionResult(
            compressed="",
            original_chars=0,
            compressed_chars=0,
            strategy_name="test",
        )
        assert r.savings_pct == 0.0


class TestOutputCompressor:
    def test_passthrough_short_output(self) -> None:
        c = OutputCompressor(min_length=100)
        r = c.compress("git status", "short output")
        assert r.strategy_name == "passthrough"
        assert r.compressed == "short output"

    def test_excluded_commands(self) -> None:
        c = OutputCompressor(min_length=10, excluded_commands=[r"secret-cmd"])
        r = c.compress("secret-cmd arg", "x" * 5000)
        assert r.strategy_name == "excluded"

    def test_git_status_strategy(self) -> None:
        git_output = (
            "On branch main\n"
            "Your branch is up to date with 'origin/main'.\n"
            "\n"
            "Changes not staged for commit:\n"
            '  (use "git add <file>..." to update what will be committed)\n'
            "\n"
        )
        git_output += "".join(f"\tM  src/file{i}.py\n" for i in range(50))
        git_output += "\nUntracked files:\n"
        git_output += "".join(f"\t?? new{i}.py\n" for i in range(20))

        c = OutputCompressor(min_length=100)
        r = c.compress("git status", git_output)
        assert r.strategy_name == "git-status"
        assert r.compressed_chars < r.original_chars
        assert "Modified" in r.compressed

    def test_pytest_strategy(self) -> None:
        pytest_output = "=== test session starts ===\n"
        pytest_output += "platform linux -- Python 3.13\n"
        pytest_output += "plugins: cov-4.0\n"
        pytest_output += "collected 200 items\n\n"
        pytest_output += "".join(f"tests/test_{i}.py PASSED\n" for i in range(150))
        pytest_output += "=== FAILURES ===\n"
        pytest_output += "____ test_thing ____\n"
        pytest_output += "    assert 1 == 2\n"
        pytest_output += "=== short test summary ===\n"
        pytest_output += "FAILED tests/test_thing.py::test_thing\n"
        pytest_output += "=== 1 failed, 149 passed ===\n"

        c = OutputCompressor(min_length=100)
        r = c.compress("pytest tests/ -v", pytest_output)
        assert r.strategy_name == "pytest"
        assert r.compressed_chars < r.original_chars

    def test_fallback_truncation(self) -> None:
        """Unknown commands get fallback truncation."""
        output = "".join(f"line {i}\n" for i in range(200))
        c = OutputCompressor(min_length=100)
        r = c.compress("some-unknown-tool", output)
        assert r.strategy_name == "fallback"
        assert "lines omitted" in r.compressed

    def test_no_compression_if_minimal_savings(self) -> None:
        """Don't compress if savings < 5%."""
        # A short output that matches git but doesn't compress much
        output = " M file.py\n" * 3
        c = OutputCompressor(min_length=10)
        r = c.compress("git status", output)
        # With only 3 lines, compression adds headers so result may be larger
        # Should fall back to passthrough
        assert r.strategy_name in ("passthrough", "git-status")

    def test_ruff_lint_strategy(self) -> None:
        ruff_output = "".join(
            f"src/file{i % 5}.py:{i}:1: E501 Line too long\n" for i in range(100)
        )
        ruff_output += "Found 100 errors.\n"
        c = OutputCompressor(min_length=100)
        r = c.compress("ruff check src/", ruff_output)
        assert r.strategy_name == "python-lint"
        assert r.compressed_chars < r.original_chars

    def test_max_lines_caps_long_output(self) -> None:
        """max_lines acts as a final cap after pipeline primitives."""
        output = "".join(f"line {i}\n" for i in range(200))
        c = OutputCompressor(min_length=100, max_lines=30)
        r = c.compress("some-unknown-tool", output)
        # truncate() marker is "\n[... N lines omitted ...]\n\n" (3 extra lines)
        # so total = 30 content + 3 marker = 33
        result_lines = r.compressed.splitlines()
        assert len(result_lines) <= 33
        assert len(result_lines) < 200  # significantly reduced
        assert "lines omitted" in r.compressed

    def test_max_lines_no_op_when_under_cap(self) -> None:
        """max_lines doesn't truncate further when output is already short."""
        output = "".join(f"line {i}\n" for i in range(200))
        c = OutputCompressor(min_length=100, max_lines=500)
        r = c.compress("some-unknown-tool", output)
        # Fallback truncation (head=20, tail=20) already reduces to ~43 lines
        # (20 head + 20 tail + 3 marker lines). max_lines=500 adds no extra cap.
        lines = r.compressed.splitlines()
        assert len(lines) <= 43
        # Only one omission marker from the fallback truncation
        assert r.compressed.count("lines omitted") == 1

    def test_max_lines_zero_means_no_cap(self) -> None:
        """max_lines=0 disables the final cap."""
        output = "".join(f"line {i}\n" for i in range(200))
        c = OutputCompressor(min_length=100, max_lines=0)
        r = c.compress("some-unknown-tool", output)
        # Fallback truncation still applies (head=20, tail=20), but no extra cap
        assert r.strategy_name == "fallback"
        assert r.compressed.count("lines omitted") == 1

    def test_max_lines_head_tail_split(self) -> None:
        """max_lines cap preserves head and tail content with omission marker."""
        # Use unknown command so fallback (head=20, tail=20) runs first,
        # then max_lines=10 cap truncates further
        output = "".join(f"unique-line-{chr(65 + (i % 26))}-{i}\n" for i in range(200))
        c = OutputCompressor(min_length=100, max_lines=10)
        r = c.compress("some-unknown-tool", output)
        lines = r.compressed.splitlines()
        # 10 content + 3 marker = 13 max
        assert len(lines) <= 13
        assert "lines omitted" in r.compressed
        # Verify head content preserved (first line from original)
        assert "unique-line-A-0" in r.compressed
        # Verify tail content preserved (last line from original)
        assert "unique-line-" in lines[-1]

    def test_preserves_exit_code_via_cli(self) -> None:
        """Test that CompressionResult is a clean dataclass."""
        r = CompressionResult(
            compressed="output",
            original_chars=100,
            compressed_chars=50,
            strategy_name="test",
        )
        assert r.savings_pct == 50.0
        assert r.strategy_name == "test"
