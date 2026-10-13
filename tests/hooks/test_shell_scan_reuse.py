"""Repeated shell inspection preserves independent parser results."""

from pathlib import Path

import pytest

from gobby.hooks._normalization_shell import scan_shell_command
from gobby.hooks.normalization import normalize_tool_fields
from gobby.workflows.engine.command_matching import executable_command_subjects

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("command", "values"),
    [
        (
            "p0() { echo 0; }; gem push x",
            ["p0()", "{", "echo", "0", ";", "}", ";", "gem", "push", "x"],
        ),
        ("echo one;; echo two", ["echo", "one", ";", ";", "echo", "two"]),
        ("name=value foo()suffix", ["name=value", "foo()suffix"]),
    ],
)
def test_literal_words_and_function_headers_keep_spans(command: str, values: list[str]) -> None:
    scan = scan_shell_command(command)
    assert [token.value for token in scan.tokens] == values
    assert [command[start:end] for start, end in scan.spans] == values
    assert not any(token.quoted for token in scan.tokens)
    assert scan.heredocs == []


def test_double_semicolon_preserves_separate_subjects() -> None:
    assert executable_command_subjects("echo one;; echo two") == ["echo one", "echo two"]


def test_repeated_scan_does_not_share_mutable_lists() -> None:
    command = "cat <<'EOF'\n" + "literal $(touch hidden)\n" * 10 + "EOF\ntouch actual.py"
    first = scan_shell_command(command)
    expected_tokens = list(first.tokens)
    expected_spans = list(first.spans)
    expected_heredocs = list(first.heredocs)
    assert expected_tokens and expected_spans and expected_heredocs

    first.tokens.clear()
    first.spans.clear()
    first.heredocs.clear()

    second = scan_shell_command(command)
    assert second.tokens == expected_tokens
    assert second.spans == expected_spans
    assert second.heredocs == expected_heredocs


def test_repeated_scan_keeps_shell_dialects_separate() -> None:
    command = "echo " + "word " * 30 + r"$'\cJ'"
    assert scan_shell_command(command, dialect="bash").tokens[-1].value == "\n"
    assert scan_shell_command(command, dialect="zsh").tokens[-1].value == "cJ"
    assert scan_shell_command(command, dialect="bash").tokens[-1].value == "\n"


@pytest.mark.parametrize(
    ("command", "kind"),
    [
        ("touch +(owned|foreign)", "write"),
        ("echo +(one|two)", "execute"),
        ("touch pre+(owned|foreign)", "write"),
    ],
)
def test_plus_extglob_preserves_word_and_scope(tmp_path: Path, command: str, kind: str) -> None:
    head, pattern = command.split(" ", 1)
    scan = scan_shell_command(command)
    assert [(token.value, token.quoted) for token in scan.tokens] == [
        (head, False),
        (pattern, False),
    ]
    assert scan.spans == [(0, len(head)), (len(head) + 1, len(command))]
    assert executable_command_subjects(command) == [command]

    data: dict[str, object] = {
        "tool_name": "Bash",
        "tool_input": {"command": command, "cwd": str(tmp_path)},
        "project_path": str(tmp_path),
    }
    normalize_tool_fields(data)
    assert data["canonical_tool_kind"] == kind
    if kind == "write":
        assert data["canonical_repo_mutation"] is True
        assert data["canonical_repo_mutation_scope_unknown"] is True
    else:
        assert "canonical_repo_mutation" not in data


@pytest.mark.parametrize(
    "command",
    [
        "p(){ touch a.py;}; p",
        "p()(touch a.py); p",
        'p(){ echo "literal"; touch a.py;}; p',
        "p()('touch' a.py); p",
        "p(){ case x in x) touch a.py;; esac;}; p",
        "p(){ cat <<EOF\nliteral\nEOF\ntouch a.py;}; p",
        "p\\\n() { touch a.py; }; p",
    ],
)
def test_function_scan_reuse_preserves_invoked_writes(tmp_path: Path, command: str) -> None:
    data: dict[str, object] = {
        "tool_name": "Bash",
        "tool_input": {"command": command, "cwd": str(tmp_path)},
        "project_path": str(tmp_path),
    }
    normalize_tool_fields(data)
    assert data["canonical_repo_mutation"] is True
