"""Inline interpreter wrappers cannot certify their embedded command outcomes."""

import json
import shlex
import subprocess
import sys
from datetime import UTC, datetime

import pytest

from gobby.tasks.close_checklist import evaluate_validation_commands
from gobby.tasks.transcript_evidence_models import TranscriptEvidence, TranscriptValidationRun
from gobby.tasks.transcript_outcomes import classify_validation_command_equivalence

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("printed_exit", [0, 101])
def test_python_wrapper_masking_inner_exit_cannot_pass_gate(printed_exit: int) -> None:
    source = (
        "import json,subprocess,sys; "
        "result=subprocess.run([sys.executable,'-c','raise SystemExit(101)']); "
        "assert result.returncode == 101; "
        f"print(json.dumps({{'exit':{printed_exit}}}))"
    )
    arguments = ["uv", "run", sys.executable, "-c", source]
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"exit": printed_exit}
    command = shlex.join(arguments)
    now = datetime.now(UTC)
    run = TranscriptValidationRun(
        session_id="session-1",
        source="codex",
        command=command,
        categories=(),
        matcher_id="shell-command",
        label="Shell command",
        outcome="success",
        exit_code=result.returncode,
        output=result.stdout,
        started_at=now,
        completed_at=now,
        order=1,
    )
    gate = evaluate_validation_commands(
        task_category="docs",
        evidence=TranscriptEvidence(validation_runs=(run,)),
        has_attributed_edits=True,
        validation_criteria=f"Run `{command}`.",
    )
    assert gate.status == "failed", gate.message
    assert run.wrapped is True
    assert run.wrapper_reason == "python inline wrapper"
    # The shell's real status remains intact; stdout cannot forge a native exit.
    assert run.exit_code == 0
    assert gate.details["criterion_command_gaps"]


@pytest.mark.parametrize(
    "command",
    [
        "python -c 'print(0)'",
        "uv run python3 -c 'print(0)'",
        "CI=1 /usr/bin/python3.13 -I -c 'print(0)'",
        "python3 -Ic'print(0)'",
        "python -W error -X dev -c 'print(0)'",
    ],
)
def test_inline_python_commands_are_diagnostic_only(command: str) -> None:
    equivalence = classify_validation_command_equivalence(command)
    assert equivalence.wrapped is True
    assert equivalence.wrapper_reason == "python inline wrapper"


@pytest.mark.parametrize(
    "command",
    [
        "uv run python -m pytest tests/tasks/test_python_wrapper_credit.py -q",
        "python3 -m pytest -c pytest.ini",
        "python3 -mpytest -c pytest.ini",
        "python3 -Impytest -c pytest.ini",
        "python scripts/check.py -c config.json",
        "python -W '-c' scripts/check.py",
    ],
)
def test_python_modules_and_scripts_keep_direct_command_credit(command: str) -> None:
    assert classify_validation_command_equivalence(command).wrapped is False


def test_direct_validation_exit_zero_still_earns_gate_credit() -> None:
    now = datetime.now(UTC)
    run = TranscriptValidationRun(
        session_id="session-1",
        source="codex",
        command="uv run pytest tests/tasks/test_python_wrapper_credit.py -q",
        categories=("test",),
        matcher_id="pytest",
        label="Pytest",
        outcome="success",
        exit_code=0,
        started_at=now,
        completed_at=now,
        order=1,
    )
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(validation_runs=(run,)),
        has_attributed_edits=True,
        validation_criteria=f"Run `{run.command}`.",
    )
    assert gate.status == "passed", gate.message
    assert gate.details["criterion_command_gaps"] == []
