"""Inline interpreter wrappers cannot certify their embedded command outcomes."""

import json
import os
import shlex
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gobby.tasks.close_checklist import evaluate_validation_commands
from gobby.tasks.transcript_evidence_models import TranscriptEvidence, TranscriptValidationRun
from gobby.tasks.transcript_outcomes import classify_validation_command_equivalence

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("printed_exit", [0, 101])
@pytest.mark.parametrize(
    ("uv_options", "options", "free_threaded_name"),
    [
        ((), (), False),
        ((), ("-IW", "error"), False),
        ((), ("-IX", "dev"), False),
        ((), ("--check-hash-based-pycs", "always"), False),
        ((), ("-IWerror",), False),
        ((), ("-IXdev",), False),
        (("--no-sync",), (), False),
        (("--project", "."), (), False),
        (("--",), (), False),
        ((), (), True),
    ],
)
def test_python_wrapper_masking_inner_exit_cannot_pass_gate(
    printed_exit: int,
    uv_options: tuple[str, ...],
    options: tuple[str, ...],
    free_threaded_name: bool,
    tmp_path: Path,
) -> None:
    source = (
        "import json,subprocess,sys; "
        "result=subprocess.run([sys.executable,'-c','raise SystemExit(101)']); "
        "assert result.returncode == 101; "
        f"print(json.dumps({{'exit':{printed_exit}}}))"
    )
    interpreter = sys.executable
    if free_threaded_name:
        # Exercise the executable-name boundary without requiring a second Python install.
        alias = tmp_path / "python3.14t"
        alias.symlink_to(sys.executable)
        interpreter = str(alias)
    arguments = ["uv", "run", *uv_options, interpreter, *options, "-c", source]
    _assert_wrapper_rejected(arguments, printed_exit)


@pytest.mark.parametrize("printed_exit", [0, 101])
def test_uv_node_wrapper_masking_inner_exit_cannot_pass_gate(printed_exit: int) -> None:
    source = (
        "const r=require('node:child_process').spawnSync(process.execPath,"
        "['-e','process.exit(101)']); "
        "if(r.status!==101)process.exit(1); "
        f"console.log(JSON.stringify({{exit:{printed_exit}}}));"
    )
    _assert_wrapper_rejected(["uv", "run", "--no-sync", "node", "-e", source], printed_exit)


def _assert_wrapper_rejected(arguments: list[str], printed_exit: int) -> None:
    result = subprocess.run(
        arguments,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "UV_NO_SYNC": "1"},
    )
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
    assert run.wrapper_reason in {"python inline wrapper", "node wrapper"}
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
        "python -IW error scripts/check.py -c config.json",
        "python -IX dev -m pytest -c pytest.ini",
        "python --check-hash-based-pycs always scripts/check.py -c config.json",
        "python -IW '-c' scripts/check.py",
        "python -IX '-c' -m pytest",
        "python -IW-c scripts/check.py",
        "python -IX-c -m pytest",
        "uv run --no-sync pytest -k python -c pytest.ini",
        "uv run --project . python -m pytest -c pytest.ini",
        "uv run --project=. --with pytest python scripts/check.py -c config.json",
        "uv run -- python scripts/check.py -c config.json",
        "uv --directory . run --no-sync pytest -k python -c pytest.ini",
        "python3.14t -m pytest -c pytest.ini",
        "python3.14t scripts/check.py -c config.json",
        "uv pip check",
        "uv lock --check",
        "uv -q pip check",
        "uv --directory . -q lock --check",
        "uv run -m pytest -c pytest.ini",
        "uv run --module pytest -k python",
        "uv run --reinstall pytest -q",
        "uv build --wheel",
        "uv export --locked",
        "uv format --check",
        "uvx pytest -q",
        "timeout 60 pytest -q",
        "time -p python -m pytest",
        "exec python -m pytest",
        "command -p python -m pytest",
        "xargs -n 1 pytest",
        "pypy3 -m pytest",
        "uvx python@3.14 -m pytest",
        "gtimeout 60 python -m pytest",
        "stdbuf -oL pytest -q",
        "sudo -n -u root python -m pytest",
        "caffeinate -i pytest -q",
        "perl scripts/check.pl",
        "ruby scripts/check.rb",
    ],
)
def test_python_modules_and_scripts_keep_direct_command_credit(command: str) -> None:
    assert classify_validation_command_equivalence(command).wrapped is False


@pytest.mark.parametrize(
    "command",
    [
        "python --future-option module -c 'print(0)'",
        "python -Z unknown -c 'print(0)'",
        "python -W",
        "python -m",
        "python --",
        "python",
        "uv run --future-option unknown python -c 'print(0)'",
        "uv run --project",
        "uv tool run python -c 'print(0)'",
        "uv tool --from example run node -e 'console.log(0)'",
        "uv unknown-launcher python -c 'print(0)'",
        "env -S 'python -c print(0)'",
        "uv run --no-sync rtk env -S 'python -c print(0)'",
        "bash --future-option script.sh",
        "uvx python -c 'print(0)'",
        "uvx --from example pypy3 -c 'print(0)'",
        "timeout 60 python -c 'print(0)'",
        "timeout --signal TERM 60 python -c 'print(0)'",
        "time -p python -c 'print(0)'",
        "exec -a alias python -c 'print(0)'",
        "command -p python -c 'print(0)'",
        "xargs -n 1 python -c 'print(0)'",
        "pypy3 -c 'print(0)'",
        "uv run --no-sync timeout 60 pypy3 -c 'print(0)'",
        "uvx --future-option python -c 'print(0)'",
        "timeout --future-option 60 python -c 'print(0)'",
        "time --future-option python -c 'print(0)'",
        "exec --future-option python -c 'print(0)'",
        "command --future-option python -c 'print(0)'",
        "xargs --future-option python -c 'print(0)'",
        "xargs -I '{}' '{}' -c 'print(0)'",
        "xargs --replace=TOKEN TOKEN -c 'print(0)'",
        "uv run uv run python -c 'print(0)'",
        "uvx python@3.14 -c 'print(0)'",
        "uvx 'python@>=3.13' -c 'print(0)'",
        "uvx cpython -c 'print(0)'",
        "uvx cpython3.14 -c 'print(0)'",
        "uvx pypy@3.10 -c 'print(0)'",
        "uvx graalpy@3.11 -c 'print(0)'",
        "uvx pyodide@3.12 -c 'print(0)'",
        "uvx python@3.14t -c 'print(0)'",
        "graalpy -c 'print(0)'",
        "gtimeout 60 python -c 'print(0)'",
        "stdbuf -oL python -c 'print(0)'",
        "sudo -n -u root python -c 'print(0)'",
        "caffeinate -i python -c 'print(0)'",
        "perl -e 'exit 0'",
        "ruby -e 'exit 0'",
        "node -e 'process.exit(0)'",
        "sudo -s 'python -c print(0)'",
        "gtimeout --future-option 60 python -c 'print(0)'",
        "stdbuf --future-option python -c 'print(0)'",
        "sudo --future-option python -c 'print(0)'",
        "caffeinate --future-option python -c 'print(0)'",
    ],
)
def test_ambiguous_interpreter_and_launcher_forms_get_no_credit(command: str) -> None:
    assert classify_validation_command_equivalence(command).wrapped is True


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
