"""Regression coverage for validation launcher option boundaries."""

import pytest

from gobby.tasks.command_equivalence import parse_validation_shell, run_location
from gobby.tasks.validation_launchers import first_executable


@pytest.mark.parametrize(
    "options",
    [
        "--directory /repo -m",
        "-m --directory /repo",
        "--module --directory=/repo",
        "-m --directory /repo --",
    ],
)
def test_uv_run_module_with_directory_does_not_return_directory(options: str) -> None:
    directories: list[str | None] = []
    resolved = first_executable(
        parse_validation_shell(f"uv run {options} pytest tests/example.py"),
        directories=directories,
    )

    assert resolved == ("pytest", ("tests/example.py",))
    assert directories == ["/repo"]


def test_uv_run_directory_preserves_plain_pytest_location() -> None:
    assert run_location("uv run --directory /repo pytest tests/example.py") == "/repo"
