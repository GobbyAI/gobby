"""Authoring-time command-shaped backtick criteria."""

from __future__ import annotations

import pytest

from gobby.tasks.criterion_commands import (
    CRITERION_COMMAND_CONTRACT,
    authored_criterion_commands,
    criterion_command_authoring_payload,
    malformed_criterion_command_findings,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "span",
    [
        "uv run pytest tests/foo.py",
        "gobby test-types audit tests/",
        "cargo test -p gobby-code",
    ],
)
def test_well_formed_command_spans_are_listed_and_explained(span: str) -> None:
    criteria = f"Done when `{span}` passes."
    assert authored_criterion_commands(criteria) == [span]
    payload = criterion_command_authoring_payload(criteria)
    assert payload["criterion_commands"] == [span]
    assert payload["criterion_command_contract"] == CRITERION_COMMAND_CONTRACT


@pytest.mark.parametrize(
    ("span", "needle"),
    [
        ("uv run pytest <path>", "placeholders"),
        ("uv run pytest tests/foo.py.", "trailing punctuation"),
        ("uv run pytest tests/foo.py | tee log", "pipelines"),
        ("uv run pytest tests/foo.py > out.txt", "redirections"),
        ("if true; then uv run pytest tests/foo.py; fi", "conditional"),
        ("uv", "incomplete package command"),
    ],
)
def test_malformed_command_spans_are_rejected(span: str, needle: str) -> None:
    findings = malformed_criterion_command_findings(f"Done when `{span}`.")
    assert findings
    assert needle in findings[0]
    assert authored_criterion_commands(f"Done when `{span}`.") == []


def test_prose_and_excluded_spans_are_not_commands() -> None:
    criteria = (
        "See the `activity` panel. "
        "The command `uv run pytest tests/foo.py` is not required. "
        "Named test: test: `tests/foo.py::test_bar`."
    )
    assert authored_criterion_commands(criteria) == []
    assert malformed_criterion_command_findings(criteria) == ()
