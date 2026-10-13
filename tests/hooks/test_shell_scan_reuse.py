"""Repeated shell inspection preserves independent parser results."""

import pytest

from gobby.hooks._normalization_shell import scan_shell_command

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("command", "values"),
    [
        (
            "p0() { echo 0; }; gem push x",
            ["p0()", "{", "echo", "0", ";", "}", ";", "gem", "push", "x"],
        ),
        ("echo one;; echo two", ["echo", "one", ";;", "echo", "two"]),
        ("name=value foo()suffix", ["name=value", "foo()suffix"]),
    ],
)
def test_literal_words_and_function_headers_keep_spans(command: str, values: list[str]) -> None:
    scan = scan_shell_command(command)
    assert [token.value for token in scan.tokens] == values
    assert [command[start:end] for start, end in scan.spans] == values
    assert not any(token.quoted for token in scan.tokens)
    assert scan.heredocs == []


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
