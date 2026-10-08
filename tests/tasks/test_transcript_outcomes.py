"""Tests for validation-command equivalence classification."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gobby.tasks.command_equivalence import command_covers
from gobby.tasks.transcript_outcomes import (
    classify_validation_command_equivalence,
    extract_outcome,
    extract_output,
    is_unexecuted_tool_result,
    wrapped_validation_command,
)


def test_nice_delimited_criterion_command_is_credited() -> None:
    command = "CARGO_BUILD_JOBS=4 nice -n 15 -- cargo clippy -p gobby-code"
    core = classify_validation_command_equivalence(command).core_command
    assert core == "cargo clippy -p gobby-code"
    assert command_covers(command, "cargo clippy -p gobby-code")


def test_nice_delimited_after_cd_criterion_command_is_credited() -> None:
    command = "cd /x && CARGO_BUILD_JOBS=4 nice -n 15 -- cargo clippy -p gobby-code"
    core = classify_validation_command_equivalence(command).core_command
    assert core == "cargo clippy -p gobby-code"
    assert command_covers(command, "cargo clippy -p gobby-code")


def test_nice_without_delimiter_after_export_criterion_command_is_credited() -> None:
    command = "export CARGO_BUILD_JOBS=4 && nice -n 15 cargo clippy -p gobby-code"
    core = classify_validation_command_equivalence(command).core_command
    assert core == "cargo clippy -p gobby-code"
    assert command_covers(command, "cargo clippy -p gobby-code")


def test_nice_inner_command_mismatch_is_not_credited() -> None:
    command = "CARGO_BUILD_JOBS=4 nice -n 15 cargo check -p gobby-code"
    assert not command_covers(command, "cargo clippy -p gobby-code")


def test_nice_numeric_option_and_absolute_path_criterion_command_is_credited() -> None:
    command = "/usr/bin/nice -5 -- cargo clippy -p gobby-code"
    core = classify_validation_command_equivalence(command).core_command
    assert core == "cargo clippy -p gobby-code"
    assert command_covers(command, "cargo clippy -p gobby-code")


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


_CLAUDE_USER_REJECTED = (
    "The user doesn't want to proceed with this tool use. The tool use was rejected "
    "(eg. if it was a file edit, the new_string was NOT written to the file). "
    "STOP what you are doing and wait for the user to tell you how to proceed."
)
_HOOK_BLOCKED = "Rule enforced by Gobby: [require-task-close]\nTask #16260 is still open."
_PERMISSION_DENIED = "Permission to use Bash has been denied."


@pytest.mark.parametrize(
    "result",
    [
        pytest.param({"is_error": True, "content": _CLAUDE_USER_REJECTED}, id="rejected"),
        pytest.param({"is_error": True, "content": _HOOK_BLOCKED}, id="hook-blocked"),
        pytest.param({"is_error": True, "content": _PERMISSION_DENIED}, id="permission-denied"),
        pytest.param(
            {"output": f"Hook denied: {_HOOK_BLOCKED}", "status": "failed"},
            id="hook-denied-prefix",
        ),
        pytest.param(
            {"toolDenialKind": "user-rejected", "content": "denied"},
            id="denial-kind",
        ),
        pytest.param(
            {"attachment": {"type": "hook_blocking_error"}},
            id="hook-blocking-attachment",
        ),
    ],
)
def test_unexecuted_tool_result_is_detected(result: object) -> None:
    assert is_unexecuted_tool_result(result) is True


@pytest.mark.parametrize(
    "result",
    [
        pytest.param(
            {"is_error": True, "content": "ruff check found 3 errors"},
            id="executed-error-output",
        ),
        pytest.param(
            {"exit_code": 1, "is_error": True, "content": _CLAUDE_USER_REJECTED},
            id="exit-code-wins",
        ),
        pytest.param(
            {"is_error": True, "stdout": "permission denied"},
            id="os-permission-denied",
        ),
        pytest.param({"exit_code": 0, "stdout": "passed"}, id="success"),
    ],
)
def test_executed_tool_result_is_not_unexecuted(result: object) -> None:
    assert is_unexecuted_tool_result(result) is False


def _claude_edit_result(*, is_error: bool, content: str) -> dict[str, object]:
    """A parsed Claude Edit result whose transport copy holds the edited file."""
    return {
        "tool_result": {"content": content, "is_error": is_error},
        "raw_json": {
            "toolUseResult": {
                "filePath": "/repo/tests/test_denials.py",
                "originalFile": f"REJECTED = {_CLAUDE_USER_REJECTED!r}\n",
            }
        },
    }


def test_executed_claude_result_quoting_denial_text_is_not_unexecuted() -> None:
    """An edit ran when Claude did not flag it, whatever its file says (#23409)."""
    executed = _claude_edit_result(
        is_error=False, content="The file /repo/tests/test_denials.py has been updated."
    )
    rejected = _claude_edit_result(is_error=True, content=_CLAUDE_USER_REJECTED)

    assert is_unexecuted_tool_result(executed, source="claude") is False
    assert is_unexecuted_tool_result(rejected, source="claude") is True


def test_unflagged_droid_hook_denial_is_unexecuted() -> None:
    """Droid's parser coerces a missing flag to False, which proves nothing (#23410)."""
    result = {"tool_result": {"content": _HOOK_BLOCKED, "is_error": False}}

    assert is_unexecuted_tool_result(result) is True


_NEXTEST_PACKAGE_ERROR = (
    "error: package ID specification `gterminal` did not match any packages\n\n"
    "help: a package with a similar name exists: `termina`\n"
    "error: command `/Users/josh/.rustup/toolchains/stable-aarch64-apple-darwin/bin/cargo test "
    "--no-run --message-format json-render-diagnostics --package gterminal --test build_env` "
    "exited with code 101"
)


def _claude_bash_result(content: str, *, is_error: bool) -> dict[str, object]:
    """A parsed Claude Bash result: a failed status lives only in the content text."""
    return {
        "tool_result": {
            "type": "tool_result",
            "tool_use_id": "toolu-bash",
            "content": content,
            "is_error": is_error,
        }
    }


@pytest.mark.parametrize(
    ("content", "is_error", "expected"),
    [
        pytest.param(
            f"Exit code 101\n{_NEXTEST_PACKAGE_ERROR}",
            True,
            ("failure", 101, None),
            id="cargo-package-error",
        ),
        pytest.param(
            "Exit code 2\nERROR: file or directory not found: tests/gone.py",
            True,
            ("failure", 2, None),
            id="pytest-usage-error",
        ),
        pytest.param(_PERMISSION_DENIED, True, ("failure", None, None), id="error-without-header"),
        pytest.param(
            "error: boom\nExit code 1", True, ("failure", None, None), id="header-not-first-line"
        ),
        pytest.param(
            "Exit code 1\nprinted by the command",
            False,
            ("success", None, None),
            id="unflagged-output",
        ),
    ],
)
def test_claude_bash_error_header_supplies_the_exit_code(
    content: str, is_error: bool, expected: tuple[str, int | None, str | None]
) -> None:
    """Claude records a failed Bash status only as its error content's first line (#23529)."""
    result = _claude_bash_result(content, is_error=is_error)

    assert extract_outcome(result, extract_output(result)[0]) == expected


def _claude_record_result(
    content: str,
    *,
    is_error: bool,
    transport: object,
    git_branch: str = "wip/23437-callback-contract",
    slug: str = "rustling-foraging-allen",
) -> dict[str, object]:
    """Close evidence's view of a Claude tool result: the parsed block plus its whole record."""
    block = {
        "type": "tool_result",
        "content": content,
        "is_error": is_error,
        "tool_use_id": "toolu_01Vkx1DDxT4GvFQWUvQLcUz8",
    }
    return {
        "tool_result": {"content": content, "is_error": is_error},
        "raw_json": {
            "parentUuid": "06fb90a1-d2bd-4638-b766-2d1769a00c12",
            "isSidechain": False,
            "promptId": "7da5ad50-6e28-4a11-ac50-754c7b407488",
            "type": "user",
            "message": {"role": "user", "content": [block]},
            "uuid": "340a274e-da85-4143-9f1f-a8103fd6f50c",
            "timestamp": "2026-10-07T20:52:36.137Z",
            "toolUseResult": transport,
            "sourceToolAssistantUUID": "06fb90a1-d2bd-4638-b766-2d1769a00c12",
            "session_id": "1c8cea2d-b28a-4fa5-8df7-2ad8bd5f028a",
            "userType": "external",
            "entrypoint": "cli",
            "cwd": "/Users/josh/.gobby/worktrees/gobby/lane-6-everything-else",
            "sessionId": "1c8cea2d-b28a-4fa5-8df7-2ad8bd5f028a",
            "version": "2.1.292",
            "gitBranch": git_branch,
            "slug": slug,
        },
    }


def test_claude_error_string_result_output_is_only_the_tool_result_text() -> None:
    """A failed Bash record's metadata never joins the run output (#23787)."""
    content = (
        "Exit code 4\n"
        "ERROR: file or directory not found: tests/config/test_validation_matchers.py\n"
        "[full output: rtk recall 6893682e04a1]"
    )
    result = _claude_record_result(content, is_error=True, transport=f"Error: {content}")

    assert extract_output(result) == (content, False)


@pytest.mark.parametrize(
    ("git_branch", "slug"),
    [("wip/1 failed", "rustling-foraging-allen"), ("main", "ERROR tests/test_a.py::test_b")],
)
def test_claude_record_metadata_cannot_flip_a_passing_run(git_branch: str, slug: str) -> None:
    """Branch and slug text is not runner output, so it cannot report failures (#23787)."""
    content = "============================= 3 passed in 0.10s =============================="
    result = _claude_record_result(
        content,
        is_error=False,
        transport={"stdout": content, "stderr": "", "interrupted": False, "isImage": False},
        git_branch=git_branch,
        slug=slug,
    )
    output, _truncated = extract_output(result)

    assert output == content
    assert extract_outcome(result, output, aggregate_status_is_trustworthy=False) == (
        "success",
        None,
        None,
    )


def test_runner_reported_failure_keeps_the_claude_error_exit_code() -> None:
    """A runner-reported red still carries the header status to exit-code consumers (#23529)."""
    output = "FAILED tests/test_x.py::test_y - AssertionError\n1 failed in 0.10s"
    result = _claude_bash_result(f"Exit code 1\n{output}", is_error=True)

    assert extract_outcome(result, output, aggregate_status_is_trustworthy=False) == (
        "failure",
        1,
        None,
    )


@pytest.mark.parametrize(
    ("command", "expected_reason"),
    [
        ("uv run mypy src/ 2>&1 | tail -3", "pipeline"),
        ("uv run ruff check src/; echo done", "trailing echo"),
        ("npx vitest run src/a.test.tsx && printf ok", "trailing printf"),
        ("cargo clippy -p gobby-core --all-targets || true", "fallback"),
        ("cargo nextest run -p gobby-core &", "backgrounding"),
        ("rtk uv run pytest tests/x.py -q | tail -5", "pipeline"),
        (
            "uv run gobby test-types audit tests/ --baseline .gobby/test-types-baseline.json"
            " --fail-on-new 2>&1 | tail -3",
            "pipeline",
        ),
        # The close gate's matcher config decides what counts, so executables the
        # gate credits are covered without a second inventory here.
        ("cargo check -p gobby-core 2>&1 | tail -3", "pipeline"),
        ("npm test | tail -20", "pipeline"),
        # The gate recognizes the run inside the subshell and still voids it.
        ("(cd web && npx vitest run src/a.test.tsx) | cat", "unsupported shell structure"),
    ],
)
def test_wrapped_validation_command_names_the_credit_voiding_wrapper(
    command: str, expected_reason: str
) -> None:
    assert wrapped_validation_command(command) == expected_reason


@pytest.mark.parametrize(
    "command",
    [
        "uv run mypy src/",
        "DATABASE_URL=x GOBBY_TEST_PROTECT=1 uv run pytest tests/x.py -q",
        "cd /repo && uv run mypy src/",
        "uv run ruff check src/ && uv run mypy src/",
        "python -m pytest tests/x.py",
        "cargo +nightly test -p gobby-core",
        # No recognized validation executable: the runner name is prose, not a
        # command, and the tail of the pipeline is unrelated tooling.
        "git commit -m 'run pytest | tail'",
        "git log --oneline | head -5",
        "uv run gobby tasks list | head -5",
        "uv run ruff --version | cat",
        # Non-executing forms earn no credit, so wrapping them loses nothing.
        "uv run pytest --collect-only -q tests/ | wc -l",
        "uv run mypy --version | head -1",
        # Unparseable for the validation shell subset, so nothing is claimed.
        "OUT=$(uv run mypy src/) | cat",
    ],
)
def test_wrapped_validation_command_reports_nothing_for_credited_or_unrecognized_calls(
    command: str,
) -> None:
    assert wrapped_validation_command(command) is None


@pytest.mark.parametrize("command", [None, 42, "", "   "])
def test_wrapped_validation_command_ignores_non_command_input(command: object) -> None:
    assert wrapped_validation_command(command) is None


def test_wrapped_validation_command_honors_project_custom_matchers(tmp_path: Path) -> None:
    """A matcher the project adds in .gobby/project.json is judged like a built-in one."""
    command = "uv run gobby test-quality audit tests/x.py --fail-on-new | tail -3"
    (tmp_path / ".gobby").mkdir()
    (tmp_path / ".gobby" / "project.json").write_text(
        json.dumps(
            {
                "validation_detection": {
                    "custom_matchers": [
                        {
                            "id": "gobby-test-quality-audit",
                            "label": "Gobby test-quality audit",
                            "languages": [],
                            "categories": ["test"],
                            "prefixes": ["gobby test-quality audit"],
                            "required_args_all": ["--fail-on-new"],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    assert wrapped_validation_command(command) is None
    assert wrapped_validation_command(command, str(tmp_path)) == "pipeline"
    # The matcher's required argument is missing, so the gate would not credit it.
    assert (
        wrapped_validation_command(
            "uv run gobby test-quality audit tests/x.py | tail -3", str(tmp_path)
        )
        is None
    )
