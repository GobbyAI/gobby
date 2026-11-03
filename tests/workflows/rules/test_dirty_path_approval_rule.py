"""Approving unowned dirty paths is the operator's decision; no seat may run the approval."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from gobby.workflows.approval_brace_guard import approval_command_word_expansion

pytestmark = pytest.mark.unit


# Every launcher, wrapper and global-option spelling that reaches the CLI.
# Ordinary arguments, other gobby subcommands and literal globs stay allowed.
# Protected brace spellings are covered separately below.


# A leading environment assignment is still a literal python heredoc: its body is data.


def test_long_python_and_brace_commands_finish_outside_the_daemon() -> None:
    # Keep the bounded scanner isolated from pytest's own timeout thread.
    program = """
import time
from gobby.workflows.approval_brace_guard import approval_command_word_expansion
commands = [
    "uv run python -c '" + "value = str(123); " * 1500 + "'",
    "uv run python -c '" + "x" * 30000 + "'",
    "uv run python -c '" + "{a,b}" * 6000 + "'",
    "echo " + "{" * 15000,
    "echo " + "{a,b}" * 15000,
]
started = time.perf_counter()
for command in commands:
    assert not approval_command_word_expansion(command), command[:80]
assert approval_command_word_expansion(commands[0] + '; gobby tasks {list,}')
assert time.perf_counter() - started < 10, 'scanner exceeded its budget'
print('bounded scanner passes')
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=Path(__file__).resolve().parents[3],
        text=True,
        capture_output=True,
        # The engine package import alone can take over 10 seconds on a loaded
        # checkout; the matching budget is asserted inside the probe, so this
        # timeout only backstops a hang.
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "bounded scanner passes"


def test_long_python_data_with_many_quotes_and_braces_stays_literal() -> None:
    # Additional proof requested after the initial correction: no behavior
    # change, and the retained RED of the protected-suffix test stays original.
    program = """
import time
from gobby.workflows.approval_brace_guard import approval_command_word_expansion
command = "uv run python -c '" + 'value = {"key": "value"}; ' * 2000 + "'"
started = time.perf_counter()
assert not approval_command_word_expansion(command)
elapsed = time.perf_counter() - started
assert elapsed < 2, elapsed
print('quoted Python data stays literal')
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=Path(__file__).resolve().parents[3],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "quoted Python data stays literal"


@pytest.mark.parametrize(
    "command",
    [
        """P=$(cat <<'EOF'
print({"probe": "gobby"})
EOF
)
cd /Users/josh/Projects/gobby && git status --short src/gobby/hooks;
diff <(cd /Users/josh/Projects/gobby && uv run --no-sync python -c "$P" 2>&1) <(cd /Users/josh/.gobby/worktrees/gobby/task-24600-remote-workspace-expiry && uv run --no-sync python -c "$P" 2>&1)""",
        'diff <(cd /repo/gobby && python -c "$P") <(cd /other && python -c "$P")',
        'diff /repo/gobby "$P"',
        'uv run --project /repo/gobby python -c "$P"',
        'uv run --directory /repo/gobby python -c "$P"',
        'python -c "$P" /repo/gobby "$Q"',
        "cd /repo/gobby && git status --short",
    ],
)
def test_command_slot_guard_allows_non_command_gobby_paths(command: str) -> None:
    assert not approval_command_word_expansion(command)


@pytest.mark.parametrize(
    "command",
    [
        "gobby sessions {approve-dirty-paths,list} s7",
        "uv run gobby sessions {approve-dirty-paths,list} s7",
        "gobby sessions approve-$(printf dirty)-paths s7",
        "uv run gobby sessions approve-$(printf dirty)-paths s7",
        'gobby sessions "$SUB" s7',
        'uv run --project /repo/gobby gobby sessions "$SUB" s7',
        'sudo -u root gobby sessions "$SUB" s7',
        'python -m gobby.cli sessions "$SUB" s7',
        'uv run -m gobby.cli sessions "$SUB" s7',
    ],
)
def test_command_slot_guard_blocks_real_expanding_gobby_commands(command: str) -> None:
    assert approval_command_word_expansion(command)


def test_command_word_expansion_pattern_is_bounded_on_long_uv_run_probe() -> None:
    # Bound the token scanner on a repeated launcher prefix.
    probe = "uv run --no-sync python -c '" + "uv run --directory /repo " * 95 + "'"
    assert len(probe) == 2404
    program = """
import sys
import time
from gobby.workflows.approval_brace_guard import approval_command_word_expansion
started = time.perf_counter()
assert not approval_command_word_expansion(sys.argv[1])
assert approval_command_word_expansion('gobby sessions {list,} ' + sys.argv[1])
assert time.perf_counter() - started < 1.0
print('bounded word expansion passes')
"""
    result = subprocess.run(
        [sys.executable, "-c", program, probe],
        cwd=Path(__file__).resolve().parents[3],
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "bounded word expansion passes"
