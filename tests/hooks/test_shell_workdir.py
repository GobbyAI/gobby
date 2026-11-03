"""Bounded literal shell directory proofs."""

import pytest

from gobby.hooks.shell_execution import literal_shell_workdir

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("cd /task && printf ok", "/task"),
        ("cd /task && printf " + "x " * 600, None),
        ("cd /task && printf '" + "x" * 20_000 + "'", None),
    ],
    ids=["ordinary", "too-many-tokens", "too-many-characters"],
)
def test_shell_directory_proof_refuses_excessive_input(command: str, expected: str | None) -> None:
    assert literal_shell_workdir(command) == expected
