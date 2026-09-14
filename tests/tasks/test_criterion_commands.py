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
    ],
)
def test_malformed_command_spans_are_rejected(span: str, needle: str) -> None:
    findings = malformed_criterion_command_findings(f"Done when `{span}`.")
    assert findings
    assert needle in findings[0]
    assert authored_criterion_commands(f"Done when `{span}`.") == []


def test_bare_tool_name_span_is_not_command_shaped() -> None:
    criteria = (
        "The `gobby` CLI refuses a linked worktree. "
        "`git` is authoritative for code. "
        "the `go` keyword "
        "Use `make` targets"
    )
    assert malformed_criterion_command_findings(criteria) == ()
    assert authored_criterion_commands(criteria) == []


def test_authored_commands_still_extracted_and_malformed_still_rejected() -> None:
    well_formed = "Run `uv run gobby restart --wait` and `cargo test -p gobby-terminal`."
    assert authored_criterion_commands(well_formed) == [
        "uv run gobby restart --wait",
        "cargo test -p gobby-terminal",
    ]
    assert malformed_criterion_command_findings(well_formed) == ()

    malformed = (
        "Avoid `uv run pytest tests/foo.py | tee log`, "
        "`uv run pytest <path>`, "
        "`uv run pytest tests/foo.py > out.txt`, and "
        "`uv run pytest tests/foo.py.`."
    )
    findings = malformed_criterion_command_findings(malformed)
    joined = " ".join(findings)
    assert "pipelines" in joined
    assert "placeholders" in joined
    assert "redirections" in joined
    assert "trailing punctuation" in joined
    assert authored_criterion_commands(malformed) == []


def test_prose_and_excluded_spans_are_not_commands() -> None:
    criteria = (
        "See the `activity` panel. "
        "The command `uv run pytest tests/foo.py` is not required. "
        "Named test: test: `tests/foo.py::test_bar`."
    )
    assert authored_criterion_commands(criteria) == []
    assert malformed_criterion_command_findings(criteria) == ()


@pytest.mark.parametrize(
    "span",
    [
        "pytest",
        "uv run pytest",
        "GOBBY_TEST_PROTECT=1 uv run pytest -q",
        "python -m pytest tests/",
        "uv run pytest -m slow",
        "cargo test",
        "cargo +nightly test --release",
        "uv run python -m pytest",
        "uv run ruff check src && uv run pytest",
        "npx vitest run",
        "jest --coverage",
        "uv run pytest tests/a.py && cargo test",
        "uv run cargo test",
        "go test",
        "go test -v",
        "go test ./...",
        "go test -v ./...",
    ],
)
def test_targetless_test_runner_spans_are_rejected(span: str) -> None:
    criteria = f"Done when `{span}` passes."
    findings = malformed_criterion_command_findings(criteria)
    assert findings
    assert "full suite" in findings[0]
    assert authored_criterion_commands(criteria) == []


@pytest.mark.parametrize(
    "span",
    [
        "uv run pytest tests/tasks/test_close_checklist.py -q",
        "uv run pytest tests/tasks/ -q",
        "uv run pytest tests/a.py::test_one",
        "uv run pytest -k close_gate",
        "uv run pytest -kclose_gate",
        "uv run pytest -q tests/tasks/test_close_checklist.py",
        "uv run pytest tests/tasks",
        "cargo test -p gobby-core",
        "cargo test grant_errors",
        "npx vitest run src/app.test.ts",
        "npx vitest run src/app.test.ts:12",
        "cd web && npx vitest run src/app.test.ts",
        "npx vitest run -t 'renders sidebar'",
        "go test ./pkg/...",
    ],
)
def test_targeted_test_runner_spans_are_accepted(span: str) -> None:
    criteria = f"Done when `{span}` passes."
    assert malformed_criterion_command_findings(criteria) == ()
    assert authored_criterion_commands(criteria) == [span]
