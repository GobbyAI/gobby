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
        "cat file |& bash",
        "cat file |& sh",
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


@pytest.mark.parametrize("shell", ["bash", "sh", "zsh", "dash", "ksh"])
@pytest.mark.parametrize(
    "template",
    [
        "{shell} <<'EOF'\ntouch a.py\nEOF",
        "{shell} <<< 'touch a.py'",
        "echo touch a.py | {shell}",
    ],
)
def test_supported_shell_stdin_scope(tmp_path: Path, shell: str, template: str) -> None:
    data = metadata(template.format(shell=shell), tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_write_file_paths"] == ["a.py"]
    assert not data.get("canonical_repo_mutation_scope_unknown")


@pytest.mark.parametrize("shell", ["bash", "sh", "zsh", "dash", "ksh"])
@pytest.mark.parametrize("pipe", ["|&", "|&\n"])
def test_stderr_pipe_shell_fails_closed(tmp_path: Path, shell: str, pipe: str) -> None:
    data = metadata(f"echo touch a.py {pipe} {shell}", tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    # Stderr can add program text, so stdout alone cannot prove the body.
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize("options", ["-euo pipefail", "-o pipefail", "-O nullglob"])
def test_bash_stdin_option_operands(tmp_path: Path, options: str) -> None:
    data = metadata(f"bash {options} <<'EOF'\ntouch a.py\nEOF", tmp_path)
    assert data["canonical_write_file_paths"] == ["a.py"]
    assert not data.get("canonical_repo_mutation_scope_unknown")


@pytest.mark.parametrize("wrapper", ["xargs", "env xargs", "xargs -n 2", "xargs -I {}"])
def test_xargs_arguments_are_not_shell_stdin(tmp_path: Path, wrapper: str) -> None:
    data = metadata(f"echo touch a.py | {wrapper} bash", tmp_path)
    assert "a.py" not in data.get("canonical_write_file_paths", [])


@pytest.mark.parametrize("shell", ["zsh", "dash", "ksh"])
def test_other_shell_dynamic_stdin_fails_closed(tmp_path: Path, shell: str) -> None:
    data = metadata(f'{shell} <<< "$BODY"', tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize(
    "options",
    ["--rcfile custom.rc", "-O lastpipe", "-o", "-eou pipefail", "-o unknown"],
)
def test_unproven_stdin_options_stay_unknown(tmp_path: Path, options: str) -> None:
    data = metadata(f"bash {options} <<'EOF'\ntouch a.py\nEOF", tmp_path)
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize("shell", ["zsh", "dash", "ksh"])
def test_other_shell_script_operand_preserves_stdin_as_data(tmp_path: Path, shell: str) -> None:
    (tmp_path / "read.sh").write_text("echo hi\n")
    data = metadata(f"{shell} read.sh <<'EOF'\ntouch a.py\nEOF", tmp_path)
    assert not data.get("canonical_repo_mutation")


def test_xargs_inline_shell_body_still_classifies(tmp_path: Path) -> None:
    data = metadata("echo ignored | xargs bash -c 'touch a.py'", tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation_scope_unknown"] is True


def test_xargs_producer_does_not_prove_literal_stdout(tmp_path: Path) -> None:
    data = metadata("xargs echo 'touch a.py' | bash", tmp_path)
    assert data["canonical_repo_mutation_scope_unknown"] is True


def test_quoted_stderr_pipe_is_data(tmp_path: Path) -> None:
    assert not metadata("echo '|& bash'", tmp_path).get("canonical_repo_mutation")


@pytest.mark.parametrize(
    "command",
    [
        "printf '%s\\n' '-c' 'touch a.py' | xargs -d '\\n' bash",
        "printf '%s\\n' '-c \"touch a.py\"' | xargs bash",
        "printf '%s\\n' '-c \"touch Dockerfile\"' | xargs bash",
        "echo touch a.py | xargs bash",
        "echo ignored | xargs -I {} bash -c '{}'",
    ],
)
def test_xargs_runtime_shell_argv_fails_closed(tmp_path: Path, command: str) -> None:
    data = metadata(command, tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation_scope_unknown"] is True
    assert "a.py" not in data.get("canonical_write_file_paths", [])


def test_xargs_shell_unknown_program_retains_output_scope(tmp_path: Path) -> None:
    data = metadata("echo touch a.py | xargs bash > result.txt", tmp_path)
    assert data["canonical_write_file_paths"] == ["result.txt"]
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize("option", ["-O", "+O"])
@pytest.mark.parametrize("script", ["evil.sh", "extglob"])
@pytest.mark.parametrize("stdin", ["", " <<'EOF'\ntouch a.py\nEOF"])
def test_zsh_correct_all_keeps_script_operand(
    tmp_path: Path, option: str, script: str, stdin: str
) -> None:
    (tmp_path / script).write_text("touch b.py\n")
    data = metadata(f"zsh {option} {script}{stdin}", tmp_path)
    assert data["canonical_write_file_paths"] == ["b.py"]
    assert not data.get("canonical_repo_mutation_scope_unknown")
