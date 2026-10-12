"""Differential check of the shell scanner's unquoted-operator probe (#23359)."""

import re

import pytest

from gobby.hooks._shell_syntax import _scan_unquoted_shell_operator

pytestmark = pytest.mark.unit

_FD_DUP_SCAN_RE = re.compile(r"[<>]&(?:\d+|-)(?=$|[\s;&|<>])")


def _reference_operator(command: str, index: int) -> tuple[str, int] | None:
    """Punctuation-only probe; the word-aware lexer attaches fd numbers."""
    char = command[index]
    if char == "\n":
        return "\n", 1
    fd_dup = _FD_DUP_SCAN_RE.match(command, index)
    if fd_dup:
        operator = fd_dup.group(0)
        return operator, len(operator)
    for operator in (
        "<<<",
        "<<-",
        "&>>",
        "&&",
        "||",
        "<<",
        ">>",
        "&>",
        ";",
        "|",
        "&",
        "<",
        ">&",
        ">",
    ):
        if command.startswith(operator, index):
            return operator, len(operator)
    return None


CORPUS = [
    "git commit -m 'a && b | c' && git push",
    'echo "x > y; z" > out.txt 2>&1',
    r"printf 'it\'s' \; echo a\|b",
    "cat <<'EOF' | grep x\n$HOME > /tmp/a; b && c\nEOF\nls",
    'tr a-z A-Z <<< "$(date) && more" 1>>log 2>/dev/null',
    "cat <<-EOF\n\tbody <x>\n\tEOF",
    "(( a < b && c > 1 )) || [[ $x == y && -n $z ]]; $((1<<3))",
    "cmd >&2 2>&- 0<&3 >&2file &>all &>>more >| clobber",
    "src2>&1 12>>x 7>y 3< in ${a:-b} `echo |x`",
    "echo ²>sup ٣>arabic ١٢>>z",
    "a & b &\nc ; d\n\n|| e",
    "",
]


@pytest.mark.parametrize("command", CORPUS)
def test_operator_probe_matches_reference_at_every_index(command: str) -> None:
    probed = [_scan_unquoted_shell_operator(command, index) for index in range(len(command))]
    expected = [_reference_operator(command, index) for index in range(len(command))]

    assert probed == expected


def test_operator_probe_corpus_reaches_every_operator_shape() -> None:
    """The corpus exercises each operator family, so agreement is not vacuous."""
    found = {
        match[0]
        for command in CORPUS
        for index in range(len(command))
        if (match := _reference_operator(command, index)) is not None
    }

    assert {
        "<<<",
        "<<-",
        "&>>",
        "&&",
        "||",
        "<<",
        ">>",
        "&>",
        ";",
        "|",
        "&",
        "<",
        ">&",
        ">",
    } <= found
    assert {"\n", ">&1", ">&2", ">&-", "<&3"} <= found
