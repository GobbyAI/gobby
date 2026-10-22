"""RTK command/output forms must preserve close-gate credit and failure attribution."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from gobby.config.validation_detection import default_validation_detection_config
from gobby.storage.session_models import Session
from gobby.tasks.command_equivalence import command_covers, pytest_targets
from gobby.tasks.transcript_evidence import _derive_transcript_path_evidence
from gobby.tasks.transcript_evidence_models import TranscriptEvidence, TranscriptValidationRun
from gobby.tasks.transcript_outcomes import classify_validation_command_equivalence

pytestmark = pytest.mark.unit
START = datetime(2026, 10, 16, tzinfo=UTC)
TEST = "tests/test_feature.py"
SOURCE = "src/feature.py"
RECALL = "d1965a04eb04"
FORMS = ("bare", "hook", "explicit", "proxy", "bypass")
FIXTURES = Path(__file__).parents[1] / "fixtures" / "rtk"
CAPTURE_101: dict[str, Any] = json.loads((FIXTURES / "contracts-1.0.1.json").read_text())


def _wrapped(command: str, form: str) -> str:
    if form == "bare":
        return command
    if form == "hook":
        if command.startswith("uv run "):
            return command.replace("uv run ", "uv run rtk ", 1)
        return f"rtk {command}"
    if form == "explicit":
        return f"rtk {command}"
    return f"rtk {form} {command}"


def _pair(
    at: int, name: str, arguments: dict[str, Any], output: str, code: int = 0
) -> list[dict[str, Any]]:
    call_id = f"call-{at}"
    return [
        {
            "type": "assistant",
            "timestamp": (START + timedelta(seconds=at)).isoformat(),
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": call_id, "name": name, "input": arguments}],
            },
        },
        {
            "type": "user",
            "timestamp": (START + timedelta(seconds=at + 1)).isoformat(),
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": call_id,
                        "content": output,
                        "is_error": code != 0,
                    }
                ],
            },
        },
    ]


def _derive(tmp_path: Path, records: list[dict[str, Any]]) -> TranscriptEvidence:
    transcript = tmp_path / "claude.jsonl"
    transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    session = Session(
        id="rtk-parity",
        external_id="fixture",
        machine_id="isolated",
        source="claude",
        project_id="project",
        title=None,
        status="active",
        transcript_path=str(transcript),
        summary_path=None,
        summary_markdown=None,
        git_branch="test",
        parent_session_id=None,
        created_at=START,
        updated_at=START,
    )
    evidence, _ = _derive_transcript_path_evidence(
        session,
        str(transcript),
        START,
        default_validation_detection_config(),
        [str(transcript)],
        resume=None,
    )
    return evidence


@pytest.mark.parametrize("launcher", ["uv run --python 3.13 rtk", "uv run --python=3.13 rtk"])
@pytest.mark.parametrize("runner,target", [("pytest", TEST), ("mypy", SOURCE)])
def test_uv_interpreter_rtk_preserves_explicit_path_scope(
    launcher: str, runner: str, target: str
) -> None:
    command = f"{launcher} {runner} {target}"
    assert command_covers(command, f"uv run {runner} {target}")
    if runner == "pytest":
        assert pytest_targets(command) == (target,)


@pytest.mark.parametrize(
    "count,summary", [(1, "Pytest: 1 passed in 0.01s"), (10, "Pytest: 192 passed in 16.20s")]
)
def test_validation_uv_interpreter_rtk_compact_pass_preserves_targets(
    tmp_path: Path, count: int, summary: str
) -> None:
    paths = tuple(f"tests/test_feature_{index}.py" for index in range(count))
    targets = " ".join(paths)
    command = f"uv run --python 3.13 rtk pytest {targets} -q --no-cov"
    audit = (
        f"uv run gobby test-types audit {targets} "
        "--baseline .gobby/test-types-baseline.json --fail-on-new"
    )
    evidence = _derive(
        tmp_path,
        _pair(0, "Bash", {"command": command}, summary)
        + _pair(2, "Bash", {"command": audit}, "0 new type findings"),
    )
    assert evidence.validation_runs[0].outcome == "success"
    assert evidence.validation_runs[0].output == summary
    assert pytest_targets(evidence.validation_runs[0].command) == paths


@pytest.mark.parametrize(
    "command",
    [
        f"uv run --python 3.13 --with plugin rtk pytest {TEST}",
        f"uv run --python 3.13 rtk pytest {TEST} -k selected",
        f"uv run --python 3.13 rtk pytest {TEST} && true",
    ],
)
def test_uv_interpreter_rtk_does_not_widen_selected_scope(command: str) -> None:
    assert not command_covers(command, f"uv run pytest {TEST}")


@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize(
    "code,summary",
    [
        (0, "Pytest: 1 passed"),
        (0, "Pytest: 14 passed, 0 failed, 1 skipped"),
        (0, "Pytest: 87 passed, 0 failed, 1 skipped in 81.29s"),
        (0, "Pytest: 14 passed, 1 skipped, 2 deselected, 1 xfailed, 1 xpassed, 2 warnings"),
        (1, "Pytest: 0 passed, 1 failed"),
        (1, "Pytest: 14 passed, 1 failed, 1 skipped in 1.0s"),
        (0, CAPTURE_101["pytest_pass"]["stdout"]),
        (1, CAPTURE_101["pytest"]["stdout"]),
    ],
)
def test_validation_pytest_audit_outcome_parity(
    tmp_path: Path, form: str, code: int, summary: str
) -> None:
    pytest_command = f"uv run pytest {TEST} -q"
    audit = (
        f"uv run gobby test-types audit {TEST} "
        "--baseline .gobby/test-types-baseline.json --fail-on-new"
    )
    records = _pair(
        0,
        "Bash",
        {"command": _wrapped(pytest_command, form)},
        summary,
        code,
    )
    records += _pair(2, "Bash", {"command": _wrapped(audit, form)}, "0 new type findings")
    evidence = _derive(tmp_path, records)
    assert evidence.validation_runs[0].outcome == ("success" if code == 0 else "failure")
    assert evidence.validation_runs[1].outcome == "success"
    assert "type_check" in evidence.validation_runs[1].categories


@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize("code", [0, 1])
def test_validation_vitest_related_parity(tmp_path: Path, form: str, code: int) -> None:
    command = "bunx vitest related src/feature.ts --run"
    records = _pair(
        0,
        "Bash",
        {"command": f"cd web && {_wrapped(command, form)}"},
        "Tests  1 passed (1)" if code == 0 else "Tests  1 failed (1)",
        code,
    )
    evidence = _derive(tmp_path, records)
    assert evidence.validation_runs[0].outcome == ("success" if code == 0 else "failure")
    assert evidence.validation_runs[0].exit_code is None


@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize(
    "command,category",
    [
        ("uv run ruff check src/feature.py", "lint"),
        ("uv run ruff format --check src/feature.py", "format"),
        ("uv run mypy src/feature.py", "type_check"),
        ("cargo test -p example", "test"),
    ],
)
@pytest.mark.parametrize("code", [0, 1])
def test_validation_categories_preserve_transport_outcome(
    tmp_path: Path, form: str, command: str, category: str, code: int
) -> None:
    records = _pair(0, "Bash", {"command": _wrapped(command, form)}, "completed", code)
    evidence = _derive(tmp_path, records)
    run = evidence.validation_runs[0]
    assert category in run.categories
    assert run.outcome == ("success" if code == 0 else "failure")
    assert run.core_command == command


@pytest.mark.parametrize("form", ["proxy", "bypass"])
@pytest.mark.parametrize("delimiter", ["", "-- "])
def test_direct_argv_wrappers_preserve_uv_options(form: str, delimiter: str) -> None:
    command = f"rtk {form} {delimiter}uv run --python 3.13 pytest {TEST} -q"
    result = classify_validation_command_equivalence(command)
    assert not result.wrapped
    assert result.core_command == f"uv run --python 3.13 pytest {TEST} -q"


@pytest.mark.parametrize(
    "command",
    [
        f"rtk proxy --help uv run pytest {TEST}",
        f"rtk bypass --unknown uv run pytest {TEST}",
        f"rtk proxy echo uv run pytest {TEST}",
        f"rtk proxy uv run pytest {TEST} | cat",
        f"rtk bypass uv run pytest {TEST} || true",
    ],
)
def test_rtk_does_not_hide_nonexecution_or_exit_obscuring_wrappers(
    tmp_path: Path, command: str
) -> None:
    evidence = _derive(tmp_path, _pair(0, "Bash", {"command": command}, "1 passed"))
    assert not any(run.outcome == "success" and not run.wrapped for run in evidence.validation_runs)


BACKGROUND_PYTEST = f"uv run rtk pytest {TEST} -q"
READER_REWRITES: dict[str, Any] = json.loads((FIXTURES / "reader-rewrites.json").read_text())


def _reader_wrapped(reader: str, path: str, form: str, release: str) -> str:
    capture = READER_REWRITES[release][reader]
    assert capture["exit_code"] == 3
    assert capture["stderr"] == ""
    if form == "hook":
        return str(capture["stdout"]).format(path=path)
    return _wrapped(str(capture["command"]).format(path=path), form)


@pytest.mark.parametrize("form", ["bare", "hook", "explicit", "proxy", "bypass"])
@pytest.mark.parametrize("reader", ["cat", "tail", "grep"])
@pytest.mark.parametrize("release", ["0.51.0", "1.0.1"])
@pytest.mark.parametrize("code", [0, 1])
def test_background_read_launcher_output_parity(
    tmp_path: Path, form: str, reader: str, release: str, code: int
) -> None:
    read = _reader_wrapped(reader, "{path}", form, release)
    output = "Pytest: 1 passed" if code == 0 else "Pytest: 0 passed, 1 failed"
    run = _background_run(tmp_path, read, output, code=code)
    assert run.outcome == ("success" if code == 0 else "failure")
    assert run.output == output
    assert run.output_recovered_from == str(tmp_path / "tasks" / "job.output")


@pytest.mark.parametrize("tail_form", ["bare", "explicit", "proxy", "bypass"])
@pytest.mark.parametrize("grep_form", ["bare", "explicit", "proxy", "bypass"])
def test_background_monitor_launcher_parity(tmp_path: Path, tail_form: str, grep_form: str) -> None:
    read = (
        _wrapped("tail -n +1 -f {path}", tail_form)
        + " | "
        + _wrapped('grep -E --line-buffered "passed|failed"', grep_form)
    )
    run = _background_run(tmp_path, read, "Pytest: 1 passed", monitor=True)
    assert run.outcome == "success"
    assert (run.output or "").endswith("Pytest: 1 passed")
    assert run.output_recovered_from == str(tmp_path / "tasks" / "job.output")


@pytest.mark.parametrize("release", ["0.51.0", "1.0.1"])
@pytest.mark.parametrize("code", [0, 1])
def test_background_monitor_captured_hook_parity(tmp_path: Path, release: str, code: int) -> None:
    read = _reader_wrapped("monitor", "{path}", "hook", release)
    output = "Pytest: 1 passed" if code == 0 else "Pytest: 0 passed, 1 failed"
    run = _background_run(tmp_path, read, output, code=code, monitor=True)
    assert run.outcome == ("success" if code == 0 else "failure")
    assert (run.output or "").endswith(output)
    assert run.output_recovered_from == str(tmp_path / "tasks" / "job.output")


def _user_text(at: int, content: str) -> dict[str, Any]:
    return {
        "type": "user",
        "timestamp": (START + timedelta(seconds=at)).isoformat(),
        "message": {"role": "user", "content": content},
    }


def _background_run(
    tmp_path: Path, read: str, output: str, *, code: int = 0, monitor: bool = False
) -> TranscriptValidationRun:
    """Parse a hook-rewritten background pytest, its exit notice, then ``read``.

    With ``monitor``, ``read`` is a Monitor command and ``output`` arrives as its event.
    """
    output_path = tmp_path / "tasks" / "job.output"
    status, summary = (
        ("completed", "completed (exit code 0)")
        if code == 0
        else ("failed", f"failed with exit code {code}")
    )
    notice = _user_text(
        3,
        "<task-notification><task-id>job</task-id><tool-use-id>call-0</tool-use-id>"
        f"<output-file>{output_path}</output-file><status>{status}</status>"
        f'<summary>Background command "feature tests" {summary}</summary>'
        "</task-notification>",
    )
    if monitor:
        reader = _pair(
            5,
            "Monitor",
            {"command": read.format(path=output_path)},
            "Monitor started (task monitor, expires in 30m unless the source ends first).",
        ) + [
            _user_text(
                7,
                "<task-notification><task-id>monitor</task-id>"
                f'<summary>Monitor event: "tests"</summary><event>{output}</event>'
                "</task-notification>",
            )
        ]
    else:
        reader = _pair(5, "Bash", {"command": read.format(path=output_path)}, output)
    audit = (
        f"uv run gobby test-types audit {TEST} "
        "--baseline .gobby/test-types-baseline.json --fail-on-new"
    )
    evidence = _derive(
        tmp_path,
        _pair(
            0,
            "Bash",
            {
                "command": BACKGROUND_PYTEST,
                "run_in_background": True,
                "description": "feature tests",
            },
            "Command running in background with ID: job. Output is being written to: "
            f"{output_path}. You will be notified when it completes.",
        )
        + [notice]
        + reader
        + _pair(8, "Bash", {"command": audit}, "0 new type findings"),
    )
    return evidence.validation_runs[0]


@pytest.mark.parametrize(
    "read", ["tail -3 {path}", "tail -c 400 {path}", "tail -n3 {path}", "tail {path}"]
)
def test_validation_credits_background_pass_read_with_plain_tail(tmp_path: Path, read: str) -> None:
    run = _background_run(tmp_path, read, "Pytest: 1 passed in 0.01s\n\n[exited with code 0]")
    assert run.outcome == "success"
    assert run.output == "Pytest: 1 passed in 0.01s\n\n[exited with code 0]"
    assert run.output_recovered_from == str(tmp_path / "tasks" / "job.output")


def test_validation_refuses_failing_background_run_read_with_tail(tmp_path: Path) -> None:
    run = _background_run(
        tmp_path,
        "tail -3 {path}",
        f"FAILED {TEST}::test_feature - assert 1 == 2\n1 failed in 0.02s\n\n[exited with code 1]",
        code=1,
    )
    assert run.outcome == "failure"
    assert "FAILED tests/test_feature.py" in (run.output or "")
    assert run.output_recovered_from == str(tmp_path / "tasks" / "job.output")


@pytest.mark.parametrize(
    "read",
    [
        "cat {path} | tail -3",
        # Bash tails a missing file, then prints a forged summary under exit 0.
        "tail -3 {path}#missing; printf 'Pytest: 1 passed in 0.01s'",
        # The substitution prints a forged summary to stderr before grep reads the file.
        """grep -E "$(printf 'Pytest: 1 passed in 0.01s' >&2)rootdir" {path}""",
    ],
)
def test_validation_ignores_unbound_background_output(tmp_path: Path, read: str) -> None:
    run = _background_run(tmp_path, read, "Pytest: 1 passed in 0.01s\n\n[exited with code 0]")
    assert run.outcome == "success"
    assert run.output_recovered_from is None
    assert "Pytest: 1 passed" not in (run.output or "")


def test_validation_credits_a_bound_monitor_follow_of_background_output(tmp_path: Path) -> None:
    run = _background_run(
        tmp_path,
        'tail -n +1 -f {path} | grep -E --line-buffered "passed|failed"',
        "Pytest: 1 passed in 0.01s",
        monitor=True,
    )
    assert run.outcome == "success"
    assert (run.output or "").endswith("Pytest: 1 passed in 0.01s")
    assert run.output_recovered_from == str(tmp_path / "tasks" / "job.output")


@pytest.mark.parametrize(
    "read",
    [
        # A shell program that waits in a loop and re-reads through a variable is ambiguous.
        'f={path}; until grep -q "exited with code" "$f"; do sleep 2; done; '
        'grep -E "passed|failed" "$f" | head -20',
        # Bash runs a pattern-less grep, then H prints the synthetic passing event.
        "tail -n +1 -f {path} | grep -E --line-buffered\nH",
        # The substitution in the pattern prints the forged event.
        "tail -n +1 -f {path} | grep -E --line-buffered "
        """"$(printf 'Pytest: 1 passed in 0.01s' >&2)passed\"""",
    ],
    ids=["shell-program", "newline-command", "substitution"],
)
def test_validation_ignores_unbound_monitor_output(tmp_path: Path, read: str) -> None:
    run = _background_run(tmp_path, read, "Pytest: 1 passed in 0.01s", monitor=True)
    assert run.outcome == "success"
    assert run.output_recovered_from is None
    assert run.background_monitor_id is None
    assert "Pytest: 1 passed" not in (run.output or "")
