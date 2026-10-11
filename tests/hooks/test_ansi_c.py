"""ANSI-C quote opener decisions across bash and zsh."""

import pytest

from gobby.hooks._ansi_c import opens_ansi_c_quote

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "text,index,expected",
    [
        ("$'x'", 0, True),
        ("\\$$'\\''", 2, True),
        ("$$$'\\''", 2, True),
        ("$$'x'", 1, False),
        ("$$'a.txt'", 1, False),
        ("\\\\$$'x'", 3, False),
        ("\\$'x'", 1, False),
        ("$x", 0, False),
    ],
)
def test_opens_ansi_c_quote(text: str, index: int, expected: bool) -> None:
    assert opens_ansi_c_quote(text, index) is expected


@pytest.mark.parametrize("text", ["$$'\\''", "$$'a\\nb'", "$$'\\"])
def test_opens_ansi_c_quote_rejects_ambiguous_closing_quotes(text: str) -> None:
    with pytest.raises(ValueError, match="bash and zsh disagree on a quote after"):
        opens_ansi_c_quote(text, 1)
