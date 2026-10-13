"""Scope shell programs delivered through stdin without executing them."""

from pathlib import Path
from typing import Any

import pytest

from gobby.hooks.normalization import normalize_tool_fields

pytestmark = pytest.mark.unit


def metadata(command: str, root: Path) -> dict[str, Any]:
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": command, "cwd": str(root)},
        "project_path": str(root),
    }
    normalize_tool_fields(data)
    return data


@pytest.mark.parametrize(
    "command",
    [
        "bash <<'EOF'\ntouch a.py\nEOF",
        "sh <<EOF\ntouch a.py\nEOF",
        "bash <<< 'touch a.py'",
        "echo touch a.py | bash",
        "printf '%s\\n' 'touch a.py' | sh",
        "printf 'touch a.py\\n' | bash -s",
        "env bash -eu <<'EOF'\ntouch a.py\nEOF",
        "sh - <<'EOF'\ntouch a.py\nEOF",
        "bash <<-'EOF'\n\ttouch a.py\n\tEOF",
        "bash 0<<'EOF'\ntouch a.py\nEOF",
        "bash 0<<< 'touch a.py'",
        "bash <<< \"bash <<< 'touch a.py'\"",
        "printf '%s %s\\n' touch a.py | bash",
    ],
)
def test_literal_stdin_shell_mutation(tmp_path: Path, command: str) -> None:
    data = metadata(command, tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_write_file_paths"] == ["a.py"]
    assert not data.get("canonical_repo_mutation_scope_unknown")


@pytest.mark.parametrize(
    "command",
    [
        "cat file | bash",
        'echo "$BODY" | sh',
        'bash <<< "$BODY"',
        "bash < script.sh",
        "bash <<EOF\n$BODY\nEOF",
        "printf '%b' '\\164ouch a.py' | bash",
        "echo -e '\\164ouch a.py' | bash",
        "echo touch a.py > script.sh | bash",
        "bash <<'EOF'\ntouch \"$TARGET\"\nEOF",
        "bash 2<<< 'touch a.py'",
        "echo 'touch a.py' 1>&2 | bash",
    ],
)
def test_unknown_stdin_shell_fails_closed(tmp_path: Path, command: str) -> None:
    data = metadata(command, tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize(
    "command",
    ["bash -c 'touch a.py'", "python3 <<'EOF'\nopen('a.py', 'w').close()\nEOF"],
)
def test_existing_inline_and_python_stdin_scope(tmp_path: Path, command: str) -> None:
    data = metadata(command, tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_write_file_paths"] == ["a.py"]


@pytest.mark.parametrize("command", ["bash <<'EOF'\necho touch a.py\nEOF", "echo echo hi | sh"])
def test_literal_stdin_shell_data_stays_read_only(tmp_path: Path, command: str) -> None:
    assert not metadata(command, tmp_path).get("canonical_repo_mutation")


def test_stdin_shell_output_redirect_preserves_both_writes(tmp_path: Path) -> None:
    data = metadata("bash <<< 'touch a.py' > result.txt", tmp_path)
    assert set(data["canonical_write_file_paths"]) == {"a.py", "result.txt"}


def test_heredoc_is_bound_to_its_owning_command(tmp_path: Path) -> None:
    data = metadata("cat <<'DATA'; bash <<'CODE'\ntouch decoy.py\nDATA\ntouch a.py\nCODE", tmp_path)
    assert data["canonical_write_file_paths"] == ["a.py"]


def test_shell_script_operand_does_not_execute_stdin_data(tmp_path: Path) -> None:
    (tmp_path / "read.sh").write_text("echo hi\n")
    assert not metadata("bash read.sh <<'EOF'\ntouch a.py\nEOF", tmp_path).get(
        "canonical_repo_mutation"
    )


@pytest.mark.parametrize(
    "command",
    ["bash -c 'echo hi' <<'EOF'\ntouch a.py\nEOF", "printf '' | bash"],
)
def test_unused_stdin_does_not_invent_mutation(tmp_path: Path, command: str) -> None:
    assert not metadata(command, tmp_path).get("canonical_repo_mutation")


def test_printf_reuses_format_for_each_literal_value(tmp_path: Path) -> None:
    data = metadata("printf '%s\\n' 'touch a.py' 'touch b.py' | bash", tmp_path)
    assert data["canonical_write_file_paths"] == ["a.py", "b.py"]


@pytest.mark.parametrize("command", ["bash -s < {script}", "cat {script} | bash"])
def test_readable_stdin_file_is_still_unknown(tmp_path: Path, command: str) -> None:
    script = tmp_path / "read.sh"
    script.write_text("echo ready\n")
    data = metadata(command.format(script=script), tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize(
    "command", ["bash <<'EOF'\ntouch {target}\nEOF", "echo touch {target} | sh"]
)
def test_literal_stdin_external_target_stays_external(tmp_path: Path, command: str) -> None:
    target = tmp_path.parent / "outside.py"
    data = metadata(command.format(target=target), tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_write_file_paths"] == [str(target)]
    assert data["canonical_repo_mutation"] is False
    assert not data.get("canonical_repo_mutation_scope_unknown")
