"""Tests for validation-command equivalence classification."""

from __future__ import annotations

import pytest

from gobby.tasks.transcript_outcomes import classify_validation_command_equivalence


@pytest.mark.parametrize(
    ("command", "expected_core"),
    [
        ("rtk uv run pytest tests/x.py -q", "uv run pytest tests/x.py -q"),
        ("uv run rtk pytest tests/x.py -q", "uv run pytest tests/x.py -q"),
        ("rtk uv run ruff check src/", "uv run ruff check src/"),
        (
            "cd /repo && GOBBY_TEST_PROTECT=1 rtk uv run pytest tests/x.py -q",
            "uv run pytest tests/x.py -q",
        ),
    ],
)
def test_rtk_wrapper_is_stripped_from_core_command(command: str, expected_core: str) -> None:
    equivalence = classify_validation_command_equivalence(command)

    assert equivalence.wrapped is False
    assert equivalence.core_command == expected_core


def test_rtk_as_argument_is_not_stripped() -> None:
    equivalence = classify_validation_command_equivalence("uv run pytest tests/test_rtk.py -q")

    assert equivalence.wrapped is False
    assert equivalence.core_command == "uv run pytest tests/test_rtk.py -q"


def test_rtk_wrapped_subshell_is_still_a_wrapper() -> None:
    equivalence = classify_validation_command_equivalence('rtk bash -c "uv run pytest tests/x.py"')

    assert equivalence.wrapped is True
    assert equivalence.wrapper_reason == "subshell wrapper"


def test_top_level_and_chain_preserves_a_provable_aggregate_outcome() -> None:
    command = "uv run ruff check src/gobby && uv run pytest tests/tasks -q"

    equivalence = classify_validation_command_equivalence(command)

    assert equivalence.wrapped is False
    assert equivalence.core_command == command


@pytest.mark.parametrize(
    ("command", "expected_reason"),
    [
        ("pytest tests/x.py | tail -1", "pipeline"),
        ("pytest tests/x.py || true", "fallback"),
        ("pytest tests/x.py & wait", "backgrounding"),
        ("pytest tests/x.py; echo done", "trailing echo"),
        ("pytest tests/x.py && STATUS=ok /bin/echo done", "trailing echo"),
        ("pytest tests/x.py && printf 'done\\n'", "trailing printf"),
        ("pytest tests/x.py; true", "command sequence"),
        ("pytest tests/x.py\ntrue", "command sequence"),
        ("pytest tests/x.py &&", "unsupported shell structure"),
    ],
)
def test_unprovable_top_level_structures_have_specific_reasons(
    command: str,
    expected_reason: str,
) -> None:
    equivalence = classify_validation_command_equivalence(command)

    assert equivalence.wrapped is True
    assert equivalence.core_command is None
    assert equivalence.wrapper_reason == expected_reason
