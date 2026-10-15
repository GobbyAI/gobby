"""Interpreter criteria need a registered matcher or a repository-tracked script."""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gobby.tasks.close_checklist import CloseGateResult, evaluate_validation_commands
from gobby.tasks.transcript_evidence_models import TranscriptEvidence, TranscriptValidationRun

pytestmark = pytest.mark.unit


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("check.py", "check.pl", "check.sh"):
        (scripts / name).write_text("# repository validation\n")
    (scripts / "check.ts").write_text("// repository validation\n")
    (scripts / "link.py").symlink_to("check.py")
    subprocess.run(["git", "-C", str(tmp_path), "add", "scripts"], check=True, capture_output=True)
    for name in ("eval", "run", "init"):
        (tmp_path / name).write_text("# tracked data, not a script operand\n")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "eval", "run", "init"], check=True, capture_output=True
    )
    (scripts / "untracked.py").write_text("# not repository validation\n")
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"scripts/check.ts": "exit 0"}}))
    config = tmp_path / ".gobby"
    config.mkdir()
    (config / "project.json").write_text(
        json.dumps(
            {
                "validation_detection": {
                    "custom_matchers": [
                        {
                            "id": "project-check",
                            "label": "Project check",
                            "categories": ["test"],
                            "prefixes": ["python -m project_check"],
                        }
                    ]
                }
            }
        )
    )
    return tmp_path


def _gate(command: str, repository: Path, *, workdir: Path | None = None) -> CloseGateResult:
    now = datetime.now(UTC)
    run = TranscriptValidationRun(
        session_id="author",
        source="codex",
        command=command,
        categories=(),
        matcher_id="shell-command",
        label="Shell command",
        outcome="success",
        exit_code=0,
        started_at=now,
        completed_at=now,
        order=1,
        workdir=str(workdir or repository),
    )
    return evaluate_validation_commands(
        task_category="docs",
        evidence=TranscriptEvidence(command_runs=(run,)),
        has_attributed_edits=True,
        validation_criteria=f"Run `{command}`.",
        close_root=str(repository),
    )


@pytest.mark.parametrize(
    "command",
    [
        "php -r 'exit(0);'",
        "php8.3 -r 'exit(0);'",
        "lua5.4 -e 'os.exit(0)'",
        "luajit -e 'os.exit(0)'",
        "bun -e 'process.exit(0)'",
        "deno eval 'Deno.exit(0)'",
        "deno eval 'Deno.exit(0)' scripts/check.py",
        "deno run scripts/untracked.py",
        "deno2 eval 'Deno.exit(0)'",
        "deno2 run scripts/untracked.py",
        "bun2 run scripts/untracked.py",
        "bun init -y",
        "bun2 init -y",
        "bun run scripts/check.ts",
        "bun2 run scripts/check.ts",
        "osascript -e 'return 0'",
        "su -c 'exit 0'",
        "doas python -c 'print(0)'",
        "lua -e 'os.exit(0)'",
        "Rscript -e 'quit(status=0)'",
        "eval 'python -c print(0)'",
        "find . -exec sh -c 'exit 0' \\;",
        "python -m unknown_check",
        "python scripts/untracked.py",
        "python scripts/link.py",
        "python --help scripts/check.py",
        "python -V scripts/check.py",
        "python -Ih scripts/check.py",
        "cd scripts && python scripts/check.py",
        "uv run --directory scripts python scripts/check.py",
        *[
            f"{shell} {options} -c 'exit 0'"
            for shell in ("dash", "ksh", "mksh", "ash", "yash", "csh", "tcsh", "busybox sh")
            for options in ("", "+x")
        ],
    ],
)
def test_unregistered_interpreter_heads_get_no_credit(command: str, repository: Path) -> None:
    gate = _gate(command, repository)
    assert gate.status == "failed", gate.details
    assert gate.details["criterion_command_gaps"]


@pytest.mark.parametrize(
    "command",
    [
        "python -m pytest tests/test_check.py",
        "uv run pytest tests/test_check.py",
        "python -m project_check",
        "python scripts/check.py",
        "perl5.34 scripts/check.pl",
        "dash scripts/check.sh",
        "uv run python scripts/check.py",
        "deno run scripts/check.py",
        "deno2 run scripts/check.ts",
        "bun ./scripts/check.ts",
        "bun2 ./scripts/check.ts",
        "bun scripts/check.ts",
        "bun run ./scripts/check.ts",
        "bun2 run ./scripts/check.ts",
        "python -I scripts/check.py",
        "python -W ignore scripts/check.py",
        "python -X dev scripts/check.py",
        "python -Wignore scripts/check.py",
        "cd scripts && python check.py",
        "uv run --directory scripts python check.py",
        *[
            f"uv run {options} pytest tests/test_check.py"
            for options in (
                "--link-mode copy",
                "--prerelease allow",
                "--exclude-newer 2026-10-01",
                "--no-binary-package example",
                "--resolution lowest",
                "--link-mode=copy",
                "--prerelease=allow --resolution=lowest",
            )
        ],
    ],
)
def test_registered_or_tracked_script_heads_keep_credit(command: str, repository: Path) -> None:
    gate = _gate(command, repository)
    assert gate.status != "failed", gate.details
    assert gate.details["criterion_commands"][0]["satisfied"] is True


@pytest.mark.parametrize(
    "command",
    [
        "git status --porcelain",
        "git restore --staged README.md",
        "gobby tokens",
        "cargo tree -i example",
        "uv run bandit src/",
        "bandit src/",
    ],
)
def test_non_interpreter_heads_unchanged(command: str, repository: Path) -> None:
    gate = _gate(command, repository)
    assert gate.status != "failed", gate.details
    assert gate.details["criterion_commands"][0]["satisfied"] is True


def test_script_credit_uses_observed_working_directory(repository: Path) -> None:
    gate = _gate("python scripts/check.py", repository, workdir=repository / "scripts")
    assert gate.status == "failed", gate.details
    assert gate.details["criterion_command_gaps"]


def test_tracked_script_in_observed_subdirectory_keeps_credit(repository: Path) -> None:
    gate = _gate("python check.py", repository, workdir=repository / "scripts")
    assert gate.status != "failed", gate.details
    assert gate.details["criterion_commands"][0]["satisfied"] is True


def test_bun_tracked_source_without_package_script_keeps_credit(repository: Path) -> None:
    (repository / "package.json").write_text(json.dumps({"scripts": {"other": "exit 0"}}))
    gate = _gate("bun run scripts/check.ts", repository)
    assert gate.status != "failed", gate.details
    assert gate.details["criterion_commands"][0]["satisfied"] is True


def test_bun_parent_package_script_does_not_borrow_tracked_file_credit(repository: Path) -> None:
    (repository / "package.json").write_text(json.dumps({"scripts": {"check.ts": "exit 0"}}))
    gate = _gate("bun run check.ts", repository, workdir=repository / "scripts")
    assert gate.status == "failed", gate.details
    assert gate.details["criterion_command_gaps"]
