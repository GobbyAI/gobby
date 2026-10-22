"""Shared command scope and close-result model contracts."""

from __future__ import annotations

import pytest

from gobby.config.validation_detection import is_validation_command
from gobby.tasks.close_checklist import (
    CloseChecklist,
    CloseGateResult,
)
from gobby.tasks.command_equivalence import (
    command_covers,
    pytest_targets,
    scope_difference,
    vitest_related_targets,
)

_MARKDOWN_BODY_RELATED = "cd web && npx vitest related src/components/shared/MarkdownBody.tsx --run"


@pytest.mark.parametrize(
    ("required", "executed"),
    [
        ("uv run pytest tests/a.py -q", "CI=1 rtk uv run pytest 'tests/a.py' -q > /tmp/out 2>&1"),
        ("uv run pytest tests/a.py -q", "cd '/repo path' && uv run pytest tests/a.py -q >> out"),
        ("uv run pytest tests/a.py -q", "uv run --directory '/repo path' pytest tests/a.py -q"),
        ("uv run pytest tests/a.py -q", "uv run --directory=/repo pytest tests/a.py -q"),
        ("uv run pytest tests/a.py -q", "uv run pytest tests/a.py tests/b.py -q"),
        ("uv run pytest tests/a.py::test_one -q", "uv run pytest tests/ -q"),
        ("uv run ruff check src/a.py", "uv run ruff check src/ tests/"),
        ("uv run ruff check src", "uv run ruff check src tests"),
        ("pytest tests/a.py -q", "pytest . -q"),
        ("cargo fmt --all -- --check", "cargo fmt --all --check"),
        ("cargo test -p gobby-core", "cargo test --package=gobby-core"),
        ("cargo clippy -p gobby-core -- -D warnings", "cargo clippy -pgobby-core -- -Dwarnings"),
        ("pytest tests/a.py -k 'a or b'", 'pytest "tests/a.py" -k "a or b" 2>errors'),
        ("pytest tests/a.py -k '|'", 'pytest tests/a.py -k "|"'),
        ("pytest tests/*.py", "pytest tests/*.py"),
        ("uv run pytest tests/a.py tests/b.py", "uv run pytest tests/a.py tests/b.py -q"),
        ("uv run pytest tests/a.py -q", "uv run pytest tests/a.py"),
        ("pytest tests/a.py", "pytest --tb=short --no-header --color yes -rA tests/a.py"),
        ("pytest tests/a.py", "pytest tests/a.py -x --maxfail 2 --durations=5 -vv"),
        ("pytest -ra tests/a.py", "pytest -ra tests/"),
        ("pytest tests/a.py -k foo -p no:randomly", "pytest -p no:randomly tests/a.py -k foo"),
        ("uv run pytest -k close_gate", "uv run pytest -q -k close_gate"),
        (
            "cargo nextest run -p gobby-client",
            "cargo nextest run -p gobby-client --status-level fail",
        ),
        (
            "cargo nextest run -p gobby-client",
            "cargo nextest run -p gobby-client --failure-output immediate",
        ),
        (
            "cargo nextest run -p gobby-client",
            "cargo nextest run -p gobby-client --no-fail-fast",
        ),
        (
            "cargo nextest run -p gobby-client --status-level fail",
            "cargo nextest run -p gobby-client",
        ),
        (
            "cargo nextest run -p gobby-client --failure-output immediate",
            "cargo nextest run -p gobby-client",
        ),
        (
            "cargo nextest run -p gobby-client --no-fail-fast",
            "cargo nextest run -p gobby-client",
        ),
        (
            "cargo clippy -- -D warnings",
            "cargo clippy --all-targets -- -D warnings",
        ),
        ("uv run ruff check src/", "uv run ruff check src/ --output-format=concise"),
        ("uv run ruff check src/ --output-format concise", "uv run ruff check src/"),
        ("uv run mypy src/", "uv run mypy src/ --no-error-summary"),
        ("uv run mypy src/ --no-error-summary", "uv run mypy src/"),
    ],
)
def test_equivalent_command_covers_required_scope(required: str, executed: str) -> None:
    assert command_covers(executed, required)


@pytest.mark.parametrize(
    "executed",
    [
        "uv run pytest tests/a.py -q",
        "uv run pytest tests/ -q -k selected",
        "uv run pytest tests/ -q --ignore tests/b.py",
        "uv run pytest tests/ -q | tail -1",
        "uv run pytest tests/ -q || true",
        "uv run pytest tests/ -q & wait",
        "bash -c 'uv run pytest tests/ -q'",
        "uv run pytest tests/ -q >",
        "uv run pytest tests/ -q < input",
        "uv run pytest tests/ -q > $(echo out)",
        "uv run pytest tests/ -q && bash -c 'true'",
        "uv run pytest tests/ -q --collect-only",
        "uv run pytest tests/ -q -m slow",
        "uv run pytest tests/ -q -p no:cacheprovider",
        "uv run pytest -q",
        "uv run --with pytest-randomly pytest tests/ -q",
    ],
)
def test_narrowed_or_obscured_command_does_not_cover_required_scope(executed: str) -> None:
    assert not command_covers(executed, "uv run pytest tests/ -q")


@pytest.mark.parametrize(
    ("required", "executed"),
    [
        (
            "cargo nextest run -p gobby-client",
            "cargo nextest run -p gobby-client -E 'test(test_name)'",
        ),
        (
            "cargo nextest run -p gobby-client",
            "cargo nextest run -p gobby-client --filter-expr 'test(test_name)'",
        ),
        (
            "cargo nextest run -p gobby-client",
            "cargo nextest run -p gobby-client --all-targets",
        ),
        (
            "cargo nextest run -p gobby-client",
            "cargo nextest run -p gobby-client -- --status-level fail",
        ),
        (
            "cargo clippy -- -D warnings",
            "cargo clippy --failure-output immediate -- -D warnings",
        ),
        (
            "cargo clippy --all-targets -- -D warnings",
            "cargo clippy -- -D warnings",
        ),
        ("uv run ruff check src/", "uv run ruff check src/ --fix"),
        ("uv run mypy src/", "uv run mypy src/ --ignore-missing-imports"),
    ],
)
def test_runner_specific_allowances_do_not_cover_unsafe_forms(required: str, executed: str) -> None:
    assert not command_covers(executed, required)


@pytest.mark.parametrize(
    ("executed", "required", "difference"),
    [
        ("uv run pytest tests/a.py -p x -p x", "uv run pytest tests/a.py -p x", "adds `-p x`"),
        (
            "npx vitest run src/b.test.ts src/a.test.ts",
            "npx vitest run src/a.test.ts src/b.test.ts",
            "orders arguments differently",
        ),
    ],
)
def test_scope_difference_names_repeats_and_reordering(
    executed: str, required: str, difference: str
) -> None:
    assert scope_difference(executed, required) == difference


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (_MARKDOWN_BODY_RELATED, ("web/src/components/shared/MarkdownBody.tsx",)),
        ("cd web && npx --no-install vitest related src/a.tsx --run", ("web/src/a.tsx",)),
        ("cd web && npm exec -- vitest related src/a.tsx --run", ("web/src/a.tsx",)),
        ("cd web && pnpm exec vitest related src/a.tsx --run", ("web/src/a.tsx",)),
        ("cd web && ./node_modules/.bin/vitest related src/a.tsx --run", ("web/src/a.tsx",)),
        ("cd web && /other/project/node_modules/.bin/vitest related src/a.tsx --run", None),
        ("cd web && evilnode_modules/.bin/vitest related src/a.tsx --run", None),
        (
            "cd web && node_modules/.bin/vitest related src/a.tsx src/b.ts --run",
            ("web/src/a.tsx", "web/src/b.ts"),
        ),
        (
            "cd /repo/web && npx vitest related src/a.tsx --run --reporter=dot",
            ("web/src/a.tsx",),
        ),
        ("cd /repo && cd web && npx vitest related --run src/a.tsx", ("web/src/a.tsx",)),
        ("export CI=1 && cd web && npx vitest related src/a.tsx --run", ("web/src/a.tsx",)),
        ("cd web && CI=1 rtk npx vitest related src/a.tsx --run", ("web/src/a.tsx",)),
        ("cd web && npx vitest related", None),
        ("cd web && npx vitest related --run", None),
        ("cd web && npx vitest related src/a.tsx", None),
        ("cd web && npx vitest related 'src/**/*.tsx' --run", None),
        ("cd web && npx vitest related src/a.tsx --run --passWithNoTests", ("web/src/a.tsx",)),
        ("cd web && npx vitest related src/a.tsx --run -t sidebar", None),
        ("cd web && npx vitest run src/a.test.tsx", None),
        ("cd web && npx jest related src/a.tsx --run", None),
        ("npx vitest related src/a.tsx --run", None),
        ("cd src && npx vitest related a.tsx --run", None),
        ("cd /other/web && npx vitest related src/a.tsx --run", None),
        ("cd web && npx vitest related ../src/a.tsx --run", None),
    ],
)
def test_vitest_related_targets_resolve_against_web(
    command: str, expected: tuple[str, ...] | None
) -> None:
    assert vitest_related_targets(command, close_root="/repo") == expected


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        # Valueless pytest and pytest-cov flags leave the next path as a target.
        *[
            (flag, ("tests/x.py",))
            for flag in (
                "--no-cov",
                "--no-cov-on-fail",
                "--cov-append",
                "--cov-branch",
                "--cov-reset",
                "-x",
                "--exitfirst",
                "--lf",
                "--last-failed",
                "--ff",
                "--failed-first",
                "--nf",
                "--new-first",
                "--sw",
                "--stepwise",
                "--sw-skip",
                "--stepwise-skip",
                "--no-header",
                "--no-summary",
                "--cache-clear",
                "--showlocals",
                "--full-trace",
                "--strict-markers",
                "--strict-config",
                "--disable-warnings",
                "--runxfail",
            )
        ],
        # Options that take a value still consume it.
        ("--cov src", ("tests/x.py",)),
        ("--cov=src --no-cov-on-fail", ("tests/x.py",)),
        ("-k expr", ("tests/x.py",)),
        ("-m marker", ("tests/x.py",)),
        ("-p no:cacheprovider", ("tests/x.py",)),
    ],
)
def test_pytest_options_before_a_path_keep_it_a_target(
    options: str, expected: tuple[str, ...]
) -> None:
    assert pytest_targets(f"uv run pytest {options} tests/x.py -q") == expected


def test_uv_directory_with_no_cov_keeps_its_path_scope() -> None:
    command = "uv run --directory /repo pytest --no-cov tests/x.py tests/y.py -q"

    assert pytest_targets(command) == ("tests/x.py", "tests/y.py")


@pytest.mark.parametrize("flag", ["--co", "--collect-only", "--setup-only", "--setup-plan"])
def test_pytest_runs_that_execute_no_tests_are_not_validation(flag: str) -> None:
    command = f"uv run pytest {flag} tests/tasks/test_close_checklist.py -q"

    assert is_validation_command(command) is False


def test_all_failures_reports_every_blocker_and_keeps_the_first_for_the_reason() -> None:
    """One evaluation names every failed gate; the first stays the headline reason."""
    checklist = CloseChecklist(
        (
            CloseGateResult(1, "children_closed", "passed", "children closed"),
            CloseGateResult(2, "linked_commits", "failed", "commit missing"),
            CloseGateResult(3, "uncommitted_task_edits", "failed", "src/a.py is dirty"),
            CloseGateResult(4, "integration_ancestry", "skipped", "No commit to inspect."),
        )
    )

    assert checklist.ready is False
    assert [gate.name for gate in checklist.all_failures] == [
        "linked_commits",
        "uncommitted_task_edits",
    ]
    assert checklist.first_failure is not None
    assert checklist.first_failure.name == "linked_commits"


def test_all_failures_is_empty_when_only_skips_remain() -> None:
    """A skipped gate is unevaluated, so it never becomes a blocker to fix."""
    checklist = CloseChecklist(
        (
            CloseGateResult(1, "children_closed", "passed", "children closed"),
            CloseGateResult(4, "integration_ancestry", "skipped", "No commit to inspect."),
        )
    )

    assert checklist.all_failures == ()
    assert checklist.first_failure is None
    assert checklist.ready is True


def test_checklist_summary_drops_the_detail_payloads() -> None:
    """Concise responses carry gate statuses without diagnostic payloads."""
    checklist = CloseChecklist(
        (
            CloseGateResult(
                3,
                "uncommitted_task_edits",
                "failed",
                "Task files are dirty.",
                details={"dirty_paths": ["src/a.py"]},
            ),
        )
    )

    assert checklist.summary() == [
        {
            "item": 3,
            "name": "uncommitted_task_edits",
            "status": "failed",
            "message": "Task files are dirty.",
        }
    ]
