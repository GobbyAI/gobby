"""Pytest usage errors, scratchpad REDs, and the stop gate's directive (#23759)."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from jinja2.sandbox import SandboxedEnvironment

from gobby.config.validation_detection import classify_validation_segments
from gobby.tasks.transcript_evidence_models import (
    TranscriptValidationRun,
    TranscriptValidationSegment,
)
from gobby.workflows.engine.blocked_tool_recovery import recovery_directive_suffix
from gobby.workflows.found_work_gate import unresolved_validation_failures
from gobby.workflows.sync_rules import get_bundled_rules_path

pytestmark = pytest.mark.unit

_ALIVE = "tests/unit/test_alive.py"
_GONE = "tests/unit/test_gone.py"
_CORRECTED = f"uv run pytest {_ALIVE} -q"
_TYPO = f"uv run pytest {_ALIVE} {_GONE} -q"
# Reviewer 15395's exit-4 red as the transcript recorded it: rtk compacted away
# pytest's "no tests ran" summary and the parser appended record metadata.
_COMPACTED_USAGE_ERROR = (
    "Exit code 4\n"
    f"ERROR: file or directory not found: {_GONE}\n"
    "[full output: rtk recall 6893682e04a1]\n"
    "06fb90a1-d2bd-4638-b766-2d1769a00c12\n"
    "user\n"
    "tool_result\n"
    "toolu_01Vkx1DDxT4GvFQWUvQLcUz8\n"
    "2026-10-07T20:52:36.137Z\n"
    "Error:\n"
    "external\n"
    "cli\n"
    "2.1.292\n"
    "wip/23437-callback-contract"
)
_REAL_FAILURE = f"FAILED {_ALIVE}::test_alive - AssertionError\n1 failed in 0.05s\n"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def project(tmp_path: Path) -> str:
    repo = tmp_path / "worktree"
    (repo / _ALIVE).parent.mkdir(parents=True)
    (repo / _ALIVE).write_text("def test_alive():\n    assert True\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    return str(repo)


def _run(order: int, command: str, *, exit_code: int, output: str = "") -> TranscriptValidationRun:
    now = datetime.now(UTC)
    segments = tuple(
        TranscriptValidationSegment(command=match.normalized_command, categories=match.categories)
        for match in classify_validation_segments(command)
    )
    return TranscriptValidationRun(
        session_id="session-23759",
        source="claude",
        command=command,
        categories=("test",),
        matcher_id="python-tests",
        label="Python tests",
        outcome="success" if exit_code == 0 else "failure",
        started_at=now,
        completed_at=now,
        order=order,
        exit_code=exit_code,
        output=output,
        validation_segments=segments,
    )


def test_compacted_usage_error_red_clears_on_the_corrected_green(project: str) -> None:
    runs = [
        _run(1, _TYPO, exit_code=4, output=_COMPACTED_USAGE_ERROR),
        _run(2, _CORRECTED, exit_code=0),
    ]

    assert unresolved_validation_failures(runs, owner_handoff=False, project_path=project) == ()


@pytest.mark.parametrize(
    ("exit_code", "output"),
    [
        (1, _COMPACTED_USAGE_ERROR),
        (4, _COMPACTED_USAGE_ERROR + "\nERROR: usage: pytest [options]"),
    ],
    ids=["not-a-usage-exit", "another-usage-error"],
)
def test_compacted_red_without_a_pure_missing_path_usage_error_still_blocks(
    project: str, exit_code: int, output: str
) -> None:
    runs = [
        _run(1, _TYPO, exit_code=exit_code, output=output),
        _run(2, _CORRECTED, exit_code=0),
    ]

    assert unresolved_validation_failures(runs, owner_handoff=False, project_path=project) == (
        runs[0],
    )


@pytest.mark.parametrize(
    "locations",
    ["--project {project} --directory {extract}", "--directory {extract} --project {project}"],
    ids=["project-first", "directory-first"],
)
def test_deliberate_red_in_a_scratchpad_extract_does_not_hold_the_stop(
    project: str, tmp_path: Path, locations: str
) -> None:
    extract = tmp_path / "scratchpad" / "base_23762"
    (extract / _ALIVE).parent.mkdir(parents=True)
    options = locations.format(project=project, extract=extract)
    runs = [_run(1, f"uv run {options} pytest {_ALIVE} -q", exit_code=1, output=_REAL_FAILURE)]

    assert unresolved_validation_failures(runs, owner_handoff=False, project_path=project) == ()


@pytest.mark.parametrize("project_option", ["--project {project} ", ""], ids=["project", "bare"])
def test_scratchpad_probe_named_by_its_targets_and_pythonpath_does_not_hold_the_stop(
    project: str, tmp_path: Path, project_option: str
) -> None:
    # Reviewer 15410's extract runs: the cwd stays in the checkout while the
    # targets, PYTHONPATH and --rootdir all point into the scratchpad.
    extract = tmp_path / "scratchpad" / "c23758" / "red"
    command = (
        f"PYTHONPATH={extract}/src uv run --no-sync {project_option.format(project=project)}"
        f"pytest {extract / _ALIVE} -k alive -o pythonpath= -p no:cacheprovider "
        f"--rootdir={extract} --no-cov -q"
    )
    runs = [_run(1, command, exit_code=1, output=_REAL_FAILURE)]

    assert unresolved_validation_failures(runs, owner_handoff=False, project_path=project) == ()


def test_scratchpad_tests_run_against_the_checkouts_own_source_still_block(
    project: str, tmp_path: Path
) -> None:
    probe = tmp_path / "scratchpad" / "probe"
    command = f"PYTHONPATH={project}/src uv run pytest {probe / _ALIVE} -q"
    runs = [_run(1, command, exit_code=1, output=_REAL_FAILURE)]

    assert unresolved_validation_failures(runs, owner_handoff=False, project_path=project) == (
        runs[0],
    )


@pytest.mark.parametrize(
    ("pythonpath", "blocks"),
    [
        ("PYTHONPATH={probe}/src:{project}/src uv run pytest", True),
        ("uv run pytest -o 'pythonpath={probe}/src {project}/src'", True),
        ("PYTHONPATH={probe}/src:{probe}/lib uv run pytest", False),
    ],
    ids=["env-checkout-second", "ini-checkout-second", "env-all-scratchpad"],
)
def test_every_pythonpath_entry_must_lie_in_the_scratchpad(
    project: str, tmp_path: Path, pythonpath: str, blocks: bool
) -> None:
    # A scratchpad entry first must not hide the checkout's source behind it.
    probe = tmp_path / "scratchpad" / "probe"
    command = f"{pythonpath.format(probe=probe, project=project)} {probe / _ALIVE} -q"
    runs = [_run(1, command, exit_code=1, output=_REAL_FAILURE)]

    expected = (runs[0],) if blocks else ()
    assert (
        unresolved_validation_failures(runs, owner_handoff=False, project_path=project) == expected
    )


def test_real_failure_still_blocks_when_the_checkout_itself_lies_in_a_scratchpad(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "scratchpad" / "worktree"
    (checkout / _ALIVE).parent.mkdir(parents=True)
    runs = [_run(1, f"uv run pytest {_ALIVE} -q", exit_code=1, output=_REAL_FAILURE)]

    assert unresolved_validation_failures(
        runs, owner_handoff=False, project_path=str(checkout)
    ) == (runs[0],)


@pytest.mark.parametrize(
    "command",
    [f"uv run pytest {_ALIVE} -q", f"cd {{project}} && uv run pytest {_ALIVE} -q"],
    ids=["project-relative", "cd-worktree"],
)
def test_real_failure_of_the_sessions_own_validation_still_blocks(
    project: str, command: str
) -> None:
    runs = [_run(1, command.format(project=project), exit_code=1, output=_REAL_FAILURE)]

    assert unresolved_validation_failures(runs, owner_handoff=False, project_path=project) == (
        runs[0],
    )


def test_terminal_failure_recovery_directive_keeps_the_whole_command() -> None:
    rule = "block-terminal-validation-failure"
    path = get_bundled_rules_path() / "stop-gates" / "enforce-found-work-ladder.yaml"
    template = yaml.safe_load(path.read_text())["rules"][rule]["effects"][0]["reason"]
    command = (
        "GOBBY_TEST_PROTECT=1 uv run --project /Users/josh/.gobby/worktrees/gobby/lane-6-x "
        "--directory /private/tmp/claude-501/s/scratchpad/cand_23719 pytest "
        "tests/config/test_validation_detection.py -q"
    )
    reason = f"Rule enforced by Gobby: [{rule}]\n" + SandboxedEnvironment().from_string(
        template
    ).render(terminal_validation_failure_commands=[command])

    suffix = recovery_directive_suffix(reason)

    assert command in suffix
    assert suffix.endswith("link_task_to_session(action=discovered).")


def test_recovery_directive_still_ends_sentences_at_terminal_punctuation() -> None:
    reason = (
        "Rule enforced by Gobby: [example]\nNothing to do here. "
        "Run the tests in /tmp/a.b/c.py again. Then stop?"
    )

    assert recovery_directive_suffix(reason) == (
        "\nRecovery directive: Run the tests in /tmp/a.b/c.py again."
    )
