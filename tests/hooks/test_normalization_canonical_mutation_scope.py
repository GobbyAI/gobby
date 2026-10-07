from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from gobby.hooks._normalization_bindings import _BASH_LOOP_BINDING_UNSTABLE_PARAMETERS
from gobby.hooks._normalization_canonical import (
    _classify_shell_segment_without_redirection,
    _set_canonical_tool_metadata,
)
from gobby.hooks._normalization_metadata import _merge_shell_segment_metadata
from gobby.hooks._normalization_operands import _resolve_long_option
from gobby.hooks._normalization_segments import _ShellSegmentMetadata

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "command",
    [
        "bash probe.sh",
        "sh -- probe.sh",
        "zsh -f probe.sh",
        "env MODE=test bash -e probe.sh",
        "env -i bash probe.sh",
        "env -u HOME bash probe.sh",
        "command -p bash probe.sh",
        "bash /tmp/scratchpad/probe.sh",
        "source /tmp/scratchpad/probe.sh",
        ". /tmp/scratchpad/probe.sh",
        "bash -c 'source /tmp/scratchpad/probe.sh'",
        "bash < /tmp/scratchpad/probe.sh",
        "bash -s < /tmp/scratchpad/probe.sh",
        "bash <<'EOF'\ntouch /project/x.py\nEOF",
        "cat /tmp/scratchpad/probe.sh | bash",
        "echo 'touch /project/x.py' | sh",
        "bash -s",
        "bash -so pipefail",
        "bash -",
    ],
)
def test_script_file_execution_requires_only_a_claim(tmp_path: Path, command: str) -> None:
    data = _shell_write_metadata(command, tmp_path)

    assert data.get("canonical_script_execution") is True
    assert data["canonical_tool_kind"] in {"execute", "read"}
    assert not data.get("canonical_repo_mutation")
    assert not data.get("canonical_write_file_paths")
    assert not data.get("canonical_repo_mutation_scope_unknown")


@pytest.mark.parametrize(
    "options", ["-euo pipefail", "-eo pipefail", "-euO extglob", "+euo pipefail"]
)
def test_clustered_shell_option_value_preserves_inline_writes(tmp_path: Path, options: str) -> None:
    target = tmp_path / "changed.py"
    data = _shell_write_metadata(f"bash {options} -c 'touch {target}'", tmp_path)

    assert data.get("canonical_repo_mutation") is True
    assert data["canonical_write_file_paths"] == [str(tmp_path / "changed.py")]
    assert not data.get("canonical_script_execution")


@pytest.mark.parametrize(
    ("prefix", "gated"),
    [
        (b"#!/bin/sh\ntouch x", True),
        # A shell runs executable text without `#!` as a script once execve fails.
        (b"touch x\n", True),
        (b"", True),
        # Magic alone is not native: a first line without a NUL still runs as a script.
        (b"\x7fELF\ntouch x\n", True),
        (b"\xcf\xfa\xed\xfe\ntouch x\n", True),
        (b"\x7fELF\x02\x01\x01\x00", False),
        (b"\xcf\xfa\xed\xfe\x0c\x00\x00\x01", False),
        (b"\xca\xfe\xba\xbe\x00\x00\x00\x02", False),
    ],
)
@pytest.mark.parametrize("relative", [False, True])
def test_direct_path_execution_gates_all_but_native_executables(
    tmp_path: Path, prefix: bytes, gated: bool, relative: bool
) -> None:
    executable = tmp_path / "scratchpad" / "bash"
    executable.parent.mkdir()
    executable.write_bytes(prefix)
    command = "./scratchpad/bash" if relative else str(executable)
    data = _shell_write_metadata(command, tmp_path)

    assert bool(data.get("canonical_script_execution")) is gated
    assert not data.get("canonical_repo_mutation")


def test_missing_direct_execution_path_requires_a_claim(tmp_path: Path) -> None:
    data = _shell_write_metadata(str(tmp_path / "missing-script"), tmp_path)

    assert data.get("canonical_script_execution") is True
    assert not data.get("canonical_repo_mutation")


def test_unreadable_direct_execution_path_requires_a_claim(tmp_path: Path) -> None:
    executable = tmp_path / "unreadable"
    executable.write_bytes(b"#!/bin/sh")
    with patch.object(Path, "open", side_effect=PermissionError("unreadable executable")):
        data = _shell_write_metadata(str(executable), tmp_path)

    assert data.get("canonical_script_execution") is True
    assert not data.get("canonical_repo_mutation")


def _invocation_metadata(
    command: str, *, event_cwd: Path | None = None, tool_cwd: Path | None = None
) -> dict[str, Any]:
    tool_input: dict[str, Any] = {"command": command}
    if tool_cwd is not None:
        tool_input["cwd"] = str(tool_cwd)
    data: dict[str, Any] = {"tool_name": "Bash", "tool_input": tool_input}
    if event_cwd is not None:
        data["cwd"] = str(event_cwd)
    _set_canonical_tool_metadata(data)
    return data


def _signature_fixture(directory: Path) -> None:
    directory.mkdir(parents=True)
    (directory / "tool").write_bytes(b"\xcf\xfa\xed\xfe\x0c\x00\x00\x01")
    (directory / "script").write_bytes(b"#!/bin/sh\n")


@pytest.mark.parametrize(
    ("command", "cwd_source", "gated"),
    [
        ("./bin/tool --version", "event", False),
        ("bin/tool", "tool_input", False),
        ("bash -c './bin/tool'", "event", False),
        ("./bin/tool && bin/tool | ./bin/tool", "event", False),
        ("nohup ./bin/tool", "event", False),
        ("./bin/script", "event", True),
        ("./bin/tool", None, True),
    ],
)
def test_relative_command_paths_resolve_against_the_tool_cwd(
    tmp_path: Path, command: str, cwd_source: str | None, gated: bool
) -> None:
    _signature_fixture(tmp_path / "bin")
    data = _invocation_metadata(
        command,
        event_cwd=tmp_path if cwd_source == "event" else None,
        tool_cwd=tmp_path if cwd_source == "tool_input" else None,
    )

    assert bool(data.get("canonical_script_execution")) is gated


@pytest.mark.parametrize(
    "command",
    [
        "cd bin && ./tool",
        "cd sub || exit 1; ./bin/tool",
        "false || cd sub; ./bin/tool",
        'cd "$D" && ./bin/tool',
        "cd; ./bin/tool",
        "pushd sub && ./bin/tool",
        "eval 'cd sub'; ./bin/tool",
        "bash -c 'cd sub || exit; ./bin/tool'",
        "cd sub && bash -c './bin/tool'",
        # Only a static path word provably runs another program; any other command
        # word may be a builtin, function or alias that changes directory.
        "chdir sub; ./bin/tool",
        "builtin chdir sub; ./bin/tool",
        "c=cd; $c sub; ./bin/tool",
        "f() { cd sub; }; f; ./bin/tool",
        "./bin/tool () cd sub; ./bin/tool; ./bin/tool",
        "{cd,./sub}; ./bin/tool",
        "./bin/tool || true; ./bin/tool",
        # Wrappers that start the program in another directory.
        "env -C sub ./bin/tool",
        "env --chdir=sub ./bin/tool",
        "sudo -D sub ./bin/tool",
        "env -C sub bash -c './bin/tool'",
    ],
)
def test_relative_command_paths_after_a_directory_change_fail_closed(
    tmp_path: Path, command: str
) -> None:
    _signature_fixture(tmp_path / "bin")
    data = _invocation_metadata(command, event_cwd=tmp_path)

    assert data.get("canonical_script_execution") is True


def test_absolute_command_paths_after_a_directory_change_keep_their_signature(
    tmp_path: Path,
) -> None:
    _signature_fixture(tmp_path / "bin")
    data = _invocation_metadata(f"cd sub || exit 1; {tmp_path}/bin/tool", event_cwd=tmp_path)

    assert not data.get("canonical_script_execution")


@pytest.mark.parametrize(("name", "gated"), [("tool", False), ("script", True)])
def test_home_command_paths_expand_before_the_signature_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, gated: bool
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _signature_fixture(tmp_path / "bin")
    data = _invocation_metadata(f"~/bin/{name}", event_cwd=tmp_path / "elsewhere")

    assert bool(data.get("canonical_script_execution")) is gated


@pytest.mark.parametrize("shell", ["sh", "bash", "zsh"])
def test_script_indirection_writes_are_classified(tmp_path: Path, shell: str) -> None:
    target = tmp_path / "owned.py"
    data = _shell_write_metadata(f"{shell} -c 'touch {target}'", tmp_path)

    assert data["canonical_repo_mutation"] is True
    assert data["canonical_write_file_paths"] == [str(target)]
    assert not data.get("canonical_script_execution")


@pytest.mark.parametrize("shell", ["sh", "bash"])
def test_decoded_scratch_script_writes_remain_exempt(tmp_path: Path, shell: str) -> None:
    target = tmp_path / "scratchpad" / "probe.py"
    data = _shell_write_metadata(f"{shell} -c 'touch {target}'", tmp_path / "repo")

    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_file_paths"] == [str(target)]
    assert data["canonical_repo_mutation"] is False
    assert not data.get("canonical_script_execution")


def test_nested_shell_string_uses_parent_cwd_without_changing_it(tmp_path: Path) -> None:
    data = _shell_write_metadata(
        f"cd {tmp_path} && bash -c \"sh -c 'cd child && touch inner.py'\" && touch outer.py",
        tmp_path,
    )

    assert data["canonical_file_paths"] == [
        str(tmp_path / "child" / "inner.py"),
        str(tmp_path / "outer.py"),
    ]
    assert data["canonical_repo_mutation"] is True


@pytest.mark.parametrize(
    "command",
    [
        'sh -c "$PROGRAM"',
        "sh -c 'exec \"$PROGRAM\"'",
        "bash -c 'touch /project/owned.py\nprintf \"'",
    ],
)
def test_unresolved_shell_program_requires_a_claim(tmp_path: Path, command: str) -> None:
    data = _shell_write_metadata(command, tmp_path)

    assert data.get("canonical_script_execution") is True


def test_mixed_unexpanded_mutation_paths_mark_scope_unknown() -> None:
    metadata = _merge_shell_segment_metadata(
        [
            _ShellSegmentMetadata(
                kind="write",
                paths=("src/ok.py", "$UNEXPANDED/file.py"),
                repo_mutation=True,
            )
        ]
    )

    assert metadata["_canonical_repo_mutation_scope_unknown"] is True
    assert metadata["canonical_file_paths"] == ["src/ok.py"]
    assert metadata["canonical_repo_mutation"] is True


def test_git_add_keeps_paths_after_flags_and_skips_chmod_values() -> None:
    all_flag = _classify_shell_segment_without_redirection(
        ["git", "add", "-A", "src/ok.py"],
        cwd=None,
    )
    force_flag = _classify_shell_segment_without_redirection(
        ["git", "add", "-f", "src/ok.py"],
        cwd=None,
    )
    chmod = _classify_shell_segment_without_redirection(
        ["git", "add", "--chmod", "+x", "src/ok.py"],
        cwd=None,
    )

    assert all_flag.paths == ("src/ok.py",)
    assert force_flag.paths == ("src/ok.py",)
    assert chmod.paths == ("src/ok.py",)


def test_read_only_loop_header_paths_stay_out_of_the_mutation_set() -> None:
    """A loop header names iteration words; only a mutating segment names writes."""
    metadata = _merge_shell_segment_metadata(
        [
            _ShellSegmentMetadata(kind="execute", paths=("1", "2", "3")),
            _ShellSegmentMetadata(kind="write", paths=("/dev/null",), repo_mutation=True),
        ]
    )

    assert metadata["canonical_tool_kind"] == "write"
    assert metadata["canonical_file_paths"] == ["/dev/null"]


def test_read_only_probe_loop_is_not_an_in_project_mutation(tmp_path: Path) -> None:
    """The command that mis-attributed `1`, `2`, and `3` to a claimed task.

    Runs the whole annotation path, `apply_path_scope_metadata` included, then
    checks the exact predicate `_tool.py` uses to decide whether to attribute
    edited files. A false predicate means `_record_successful_file_mutation`
    is never reached for this command.
    """
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {
            "command": (
                'for i in 1 2 3; do curl -s -o /dev/null -w "attempt $i" '
                "http://localhost:60887/health; done"
            ),
            "cwd": str(tmp_path),
        },
        "project_path": str(tmp_path),
    }

    _set_canonical_tool_metadata(data)

    assert data["canonical_file_paths"] == ["/dev/null"]
    assert data.get("canonical_repo_mutation") is not True
    is_canonical_edit = (
        data.get("canonical_tool_kind") == "write" and data.get("canonical_repo_mutation") is True
    )
    assert is_canonical_edit is False, "no edited-file attribution is recorded"


def test_in_project_shell_write_still_attributes_its_own_path(tmp_path: Path) -> None:
    """Guard: the same annotation path still reports a real in-project write."""
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": "echo hi > out.txt", "cwd": str(tmp_path)},
        "project_path": str(tmp_path),
    }

    _set_canonical_tool_metadata(data)

    assert data["canonical_file_paths"] == ["out.txt"]
    assert data["canonical_repo_mutation"] is True


def test_unresolved_shell_write_exposes_its_unknown_scope(tmp_path: Path) -> None:
    """A variable target is a repository write but has no safe attribution path."""
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {
            "command": 'TARGET=src/generated\nmkdir -p "$TARGET"',
            "cwd": str(tmp_path),
        },
        "project_path": str(tmp_path),
    }

    _set_canonical_tool_metadata(data)

    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True
    assert data.get("canonical_file_paths") in (None, [])


def test_dynamic_python_write_exposes_its_unknown_scope(tmp_path: Path) -> None:
    """A proven Python write with a dynamic target remains fail-closed."""
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {
            "command": (
                "uv run python -c \"from pathlib import Path; Path(input()).write_text('x')\""
            ),
            "cwd": str(tmp_path),
        },
        "project_path": str(tmp_path),
    }

    _set_canonical_tool_metadata(data)

    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True
    assert data.get("canonical_file_paths") in (None, [])


def test_loop_header_still_scopes_a_mutating_body_with_unexpanded_paths() -> None:
    """Guard: `for f in a.py b.py; do sed -i ... "$f"; done` still attributes both.

    The header literals are the only scope signal a body with unexpanded
    operands has, which is the case the header classifier was built for.
    """
    metadata = _merge_shell_segment_metadata(
        [
            _ShellSegmentMetadata(
                kind="execute",
                paths=("a.py", "b.py"),
                loop_binding_variable="f",
            ),
            _ShellSegmentMetadata(
                kind="write",
                paths=("$f",),
                repo_mutation=True,
                shell_words=("$f",),
                shell_raw_words=('"$f"',),
            ),
        ]
    )

    assert metadata["canonical_file_paths"] == ["a.py", "b.py"]
    assert metadata["_canonical_repo_mutation_scope_unknown"] is True
    assert metadata["_canonical_repo_mutation_scope_resolved_by_loop_binding"] is True


def test_loop_binding_clears_only_the_public_unknown_scope(tmp_path: Path) -> None:
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {
            "command": 'for f in a.py b.py; do sed -i "s/x/y/" "$f"; done',
            "cwd": str(tmp_path),
        },
        "project_path": str(tmp_path),
    }

    _set_canonical_tool_metadata(data)

    assert data["canonical_file_paths"] == ["a.py", "b.py"]
    assert data["canonical_repo_mutation"] is True
    assert "canonical_repo_mutation_scope_unknown" not in data


def test_bash_loop_binding_unstable_parameters_are_pinned() -> None:
    assert _BASH_LOOP_BINDING_UNSTABLE_PARAMETERS == frozenset(
        {
            "RANDOM",
            "SRANDOM",
            "SECONDS",
            "LINENO",
            "BASHPID",
            "BASH_COMMAND",
            "BASH_SUBSHELL",
            "BASH_ARGV",
            "BASH_ARGC",
            "BASH_ARGV0",
            "BASH_SOURCE",
            "BASH_LINENO",
            "BASH_VERSINFO",
            "BASH_VERSION",
            "BASH_ALIASES",
            "BASH_CMDS",
            "BASH_EXECUTION_STRING",
            "BASH_REMATCH",
            "FUNCNAME",
            "HISTCMD",
            "EPOCHSECONDS",
            "EPOCHREALTIME",
            "PIPESTATUS",
            "DIRSTACK",
            "GROUPS",
            "UID",
            "EUID",
            "PPID",
            "SHELLOPTS",
            "BASHOPTS",
            "OPTIND",
            "OPTARG",
            "OPTERR",
            "REPLY",
            "COMP_WORDS",
            "COMP_CWORD",
            "COMP_LINE",
            "COMP_POINT",
            "COMP_KEY",
            "COMP_TYPE",
            "COMPREPLY",
            "IFS",
            "PATH",
            "HOME",
            "PWD",
            "OLDPWD",
            "SHLVL",
            "_",
        }
    )


@pytest.mark.parametrize("variable", sorted(_BASH_LOOP_BINDING_UNSTABLE_PARAMETERS))
def test_bash_unstable_parameter_cannot_supply_loop_binding(
    tmp_path: Path,
    variable: str,
) -> None:
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {
            "command": (f'for {variable} in a.py b.py; do sed -i "s/x/y/" "${{{variable}}}"; done'),
            "cwd": str(tmp_path),
        },
        "project_path": str(tmp_path),
    }

    _set_canonical_tool_metadata(data)

    assert data.get("canonical_file_paths") in (None, [])
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize("reference", ['"$f"', '"${f}"'])
def test_double_quoted_loop_parameter_references_preserve_binding(
    tmp_path: Path,
    reference: str,
) -> None:
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {
            "command": f'for f in a.py b.py; do sed -i "s/x/y/" {reference}; done',
            "cwd": str(tmp_path),
        },
        "project_path": str(tmp_path),
    }

    _set_canonical_tool_metadata(data)

    assert data["canonical_file_paths"] == ["a.py", "b.py"]
    assert "canonical_repo_mutation_scope_unknown" not in data


@pytest.mark.parametrize("header_word", ["'src/a.py src/b.py'", "'src/*.py'"])
def test_unquoted_loop_parameter_reference_keeps_scope_unknown(
    tmp_path: Path,
    header_word: str,
) -> None:
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {
            "command": f"for f in {header_word}; do rm $f; done",
            "cwd": str(tmp_path),
        },
        "project_path": str(tmp_path),
    }

    _set_canonical_tool_metadata(data)

    assert data.get("canonical_file_paths") in (None, [])
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize(
    ("header_word", "expected_path"),
    [
        ("'src/a.py src/b.py'", "src/a.py src/b.py"),
        ("'src/*.py'", "src/*.py"),
    ],
)
def test_double_quoted_loop_parameter_reference_preserves_literal_header_path(
    tmp_path: Path,
    header_word: str,
    expected_path: str,
) -> None:
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {
            "command": f'for f in {header_word}; do rm "$f"; done',
            "cwd": str(tmp_path),
        },
        "project_path": str(tmp_path),
    }

    _set_canonical_tool_metadata(data)

    assert data["canonical_file_paths"] == [expected_path]
    assert "canonical_repo_mutation_scope_unknown" not in data


@pytest.mark.parametrize(
    "reference",
    [
        "${f:-x}",
        "${f#p}",
        "${f//a/b}",
        "${f^^}",
        "${!f}",
        "${f[0]}",
        "${f[@]}",
        "'$f'",
        r"\$f",
        "$1",
        "$@",
        "$*",
        "$#",
        "$?",
        "$!",
        "$0",
        "$-",
        "$$",
    ],
)
def test_non_plain_loop_parameter_reference_drops_binding(
    tmp_path: Path,
    reference: str,
) -> None:
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {
            "command": f'for f in a.py b.py; do sed -i "s/x/y/" {reference}; done',
            "cwd": str(tmp_path),
        },
        "project_path": str(tmp_path),
    }

    _set_canonical_tool_metadata(data)

    assert data.get("canonical_file_paths") in (None, [])
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize(
    "header_words",
    [
        "*.py",
        "a.py $FILES",
        "$(printf a.py)",
        "`printf a.py`",
        "{a,b}.py",
    ],
)
def test_non_literal_loop_header_words_drop_binding(
    tmp_path: Path,
    header_words: str,
) -> None:
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {
            "command": f'for f in {header_words}; do sed -i "s/x/y/" "$f"; done',
            "cwd": str(tmp_path),
        },
        "project_path": str(tmp_path),
    }

    _set_canonical_tool_metadata(data)

    assert data.get("canonical_file_paths") in (None, [])
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize(
    "declaration",
    [
        "readonly f",
        "declare -r f",
        "declare -n f=target",
        "declare -i f",
        "typeset -in f",
        "f=seed for",
        "env f=seed for",
    ],
)
def test_shell_declaration_attributes_disqualify_later_loop_binding(
    tmp_path: Path,
    declaration: str,
) -> None:
    if declaration.endswith("for"):
        command = f'{declaration} f in a.py b.py; do sed -i "s/x/y/" "$f"; done'
    else:
        command = f'{declaration}; for f in a.py b.py; do sed -i "s/x/y/" "$f"; done'
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": command, "cwd": str(tmp_path)},
        "project_path": str(tmp_path),
    }

    _set_canonical_tool_metadata(data)

    assert data.get("canonical_file_paths") in (None, [])
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize(
    "intervening_segment",
    [
        'f="$SRC"',
        "unset x",
        'printf -v x %s "$SRC"',
        "read -p f response",
        "((f = 1))",
        "eval 'f=\"$SRC\"'",
        "source ./rebind.sh",
        ". ./rebind.sh",
        "readarray f",
        "mapfile f",
        "getopts x f",
        'declare f="$SRC"',
        "x=$(rebind_f)",
        "rebind_f",
    ],
)
def test_unproven_intervening_segment_restores_unknown_write_scope(
    tmp_path: Path,
    intervening_segment: str,
) -> None:
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {
            "command": (
                f'for f in a.py b.py; do {intervening_segment}; sed -i "s/x/y/" "$f"; done'
            ),
            "cwd": str(tmp_path),
        },
        "project_path": str(tmp_path),
    }

    _set_canonical_tool_metadata(data)

    assert data.get("canonical_file_paths") in (None, [])
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize("safe_segment", ["x=value", "echo processing"])
def test_proven_safe_intervening_segment_preserves_loop_binding(
    tmp_path: Path,
    safe_segment: str,
) -> None:
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {
            "command": (f'for f in a.py b.py; do {safe_segment}; sed -i "s/x/y/" "$f"; done'),
            "cwd": str(tmp_path),
        },
        "project_path": str(tmp_path),
    }

    _set_canonical_tool_metadata(data)

    assert data["canonical_file_paths"] == ["a.py", "b.py"]
    assert data["canonical_repo_mutation"] is True
    assert "canonical_repo_mutation_scope_unknown" not in data


def _shell_write_metadata(command: str, project: Path) -> dict[str, Any]:
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": command, "cwd": str(project)},
        "project_path": str(project),
    }
    _set_canonical_tool_metadata(data)
    return data


@pytest.mark.parametrize(
    ("template", "target"),
    [
        pytest.param(
            "out={scratch}/a.json; cat > \"$out\" <<'EOF'\nhi\nEOF", "a.json", id="heredoc"
        ),
        pytest.param('S={scratch}; echo hi > "$S/a.txt"', "a.txt", id="suffix"),
        pytest.param('S={scratch} && printf hi > "${{S}}/a.txt"', "a.txt", id="braced"),
        pytest.param('A=1; S={scratch}; mkdir -p "$S"; echo hi > "$S/a.txt"', "a.txt", id="chain"),
    ],
)
def test_leading_literal_assignment_resolves_scratch_write(
    tmp_path: Path, template: str, target: str
) -> None:
    project, scratch = tmp_path / "project", tmp_path / "scratch"

    data = _shell_write_metadata(template.format(scratch=scratch), project)

    assert data["canonical_file_paths"][-1] == f"{scratch}/{target}"
    assert data["canonical_repo_mutation"] is False
    assert "canonical_repo_mutation_scope_unknown" not in data


def test_leading_literal_assignment_keeps_repository_writes_attributed(tmp_path: Path) -> None:
    data = _shell_write_metadata(f'out={tmp_path}/src/a.py; echo x > "$out"', tmp_path)

    assert data["canonical_file_paths"] == [f"{tmp_path}/src/a.py"]
    assert data["canonical_repo_mutation"] is True
    assert "canonical_repo_mutation_scope_unknown" not in data


@pytest.mark.parametrize(
    "template",
    [
        pytest.param('echo hi > "$TMPDIR/a.txt"', id="environment-value"),
        pytest.param("S={scratch}; echo hi > $S/a.txt", id="unquoted-reference"),
        pytest.param('S=scratch; echo hi > "$S/a.txt"', id="relative-value"),
        pytest.param('S=~/scratch; echo hi > "$S/a.txt"', id="tilde-value"),
        pytest.param('S="$HOME/x"; echo hi > "$S/a.txt"', id="expanded-value"),
        pytest.param('S={scratch}; S="$Y"; echo hi > "$S/a.txt"', id="rebound"),
        pytest.param('S={scratch}; mystery; echo hi > "$S/a.txt"', id="unknown-command"),
        pytest.param('S={scratch} true; echo hi > "$S/a.txt"', id="env-prefix"),
        pytest.param('S={scratch} & echo hi > "$S/a.txt"', id="backgrounded"),
        pytest.param('S={scratch} | true; echo hi > "$S/a.txt"', id="pipeline"),
        pytest.param('false && S={scratch}; echo hi > "$S/a.txt"', id="conditional"),
        pytest.param('declare -n S; S={scratch}; echo hi > "$S/a.txt"', id="nameref"),
        pytest.param('S={scratch}; echo hi > "$S$T/a.txt"', id="second-expansion"),
    ],
)
def test_unproven_assignment_keeps_write_scope_unknown(tmp_path: Path, template: str) -> None:
    scratch = tmp_path / "scratch"

    data = _shell_write_metadata(template.format(scratch=scratch), tmp_path / "project")

    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize(
    "untrusted_execution",
    [
        "def redirect(value):\n    pass\nredirect(Path)",
        "import pathlib\ndef redirect(value):\n    pass\nredirect(pathlib)",
        "def redirect(value):\n    pass\nconstructor = Path\nredirect(constructor)",
        "import pathlib\ndef redirect(value):\n    pass\nmodule = pathlib\nredirect(module)",
        "def redirect():\n    pass\nredirect()",
        "redirect()",
        "from redirector import redirect\nredirect()",
        "import redirector",
    ],
    ids=[
        "constructor-argument",
        "module-argument",
        "constructor-alias",
        "module-alias",
        "local-no-argument-call",
        "opaque-no-argument-call",
        "imported-no-argument-call",
        "untrusted-import",
    ],
)
def test_python_heredoc_untrusted_execution_keeps_write_scope_unknown(
    tmp_path: Path, untrusted_execution: str
) -> None:
    scratch, project = tmp_path / "scratch", tmp_path / "project"
    command = (
        "uv run python - <<'EOF'\nfrom pathlib import Path\n"
        f"p = Path('{scratch}/safe.txt')\n{untrusted_execution}\np.write_text('x')\nEOF\n"
    )

    data = _shell_write_metadata(command, project)

    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize("untrusted_execution", ["redirect()", "import redirector"])
def test_python_heredoc_untrusted_execution_invalidates_direct_constructor_scope(
    tmp_path: Path, untrusted_execution: str
) -> None:
    scratch, project = tmp_path / "scratch", tmp_path / "project"
    command = (
        "uv run python - <<'EOF'\nfrom pathlib import Path\n"
        f"{untrusted_execution}\nPath('{scratch}/safe.txt').write_text('x')\nEOF\n"
    )

    data = _shell_write_metadata(command, project)

    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


def test_python_heredoc_trusted_stdlib_keeps_literal_scratch_scope(tmp_path: Path) -> None:
    scratch, project = tmp_path / "scratch", tmp_path / "project"
    command = (
        "uv run python - <<'EOF'\nimport json\nimport math\nfrom pathlib import Path\n"
        f"p = Path('{scratch}/safe.txt')\n"
        "print(json.dumps({'n': math.ceil(1.5)}), p.name, p.as_posix())\np.write_text('x')\nEOF\n"
    )

    data = _shell_write_metadata(command, project)

    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is False
    assert data.get("canonical_repo_mutation_scope_unknown", False) is False


def test_python_heredoc_with_open_handle_keeps_literal_scratch_scope(tmp_path: Path) -> None:
    scratch, project = tmp_path / "scratch", tmp_path / "project"
    command = (
        f"python3 - <<'EOF'\nwith open('{scratch}/safe.txt', 'w') as fh:\n    fh.write('x')\nEOF\n"
    )

    data = _shell_write_metadata(command, project)

    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is False
    assert data.get("canonical_repo_mutation_scope_unknown", False) is False


def test_python_heredoc_aliased_open_handle_keeps_scope_unknown(tmp_path: Path) -> None:
    scratch, project = tmp_path / "scratch", tmp_path / "project"
    command = (
        f"python3 - <<'EOF'\nwith open('{scratch}/safe.txt', 'w') as fh:\n"
        "    g = fh\n    g.write('x')\nEOF\n"
    )

    data = _shell_write_metadata(command, project)

    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


def test_python_heredoc_loop_rebinding_a_scratch_path_name_keeps_scope_unknown(
    tmp_path: Path,
) -> None:
    scratch, project = tmp_path / "scratch", tmp_path / "project"
    command = (
        "uv run python - <<'EOF'\n"
        "from pathlib import Path\n"
        f"p = Path('{scratch}/safe.txt')\n"
        "for p in [Path('src/a.py')]:\n"
        "    p.write_text('x')\n"
        "EOF"
    )

    data = _shell_write_metadata(command, project)

    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


def test_python_heredoc_class_rebinding_a_scratch_path_name_keeps_scope_unknown(
    tmp_path: Path,
) -> None:
    scratch, project = tmp_path / "scratch", tmp_path / "project"
    command = (
        "uv run python - <<'EOF'\n"
        "from pathlib import Path\n"
        f"p = Path('{scratch}/safe.txt')\n"
        "class p:\n"
        "    write_text = Path('src/a.py').write_text\n"
        "p.write_text('x')\n"
        "EOF"
    )

    data = _shell_write_metadata(command, project)

    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize(
    ("first_import", "second_import", "constructor"),
    [
        ("from redirector import Path", "from pathlib import Path", "Path"),
        ("from pathlib import Path", "from redirector import Path", "Path"),
        ("from redirector import Path as P", "from pathlib import Path as P", "P"),
        ("from pathlib import Path as P", "from redirector import Path as P", "P"),
        ("import redirector as lib", "import pathlib as lib", "lib.Path"),
        ("import pathlib as lib", "import redirector as lib", "lib.Path"),
    ],
)
def test_python_heredoc_conflicting_path_imports_keep_scope_unknown(
    tmp_path: Path, first_import: str, second_import: str, constructor: str
) -> None:
    scratch, project = tmp_path / "scratch", tmp_path / "project"
    command = (
        f"uv run python - <<'EOF'\n{first_import}\n"
        f"p = {constructor}('{scratch}/safe.txt')\n{second_import}\np.write_text('x')\nEOF"
    )

    data = _shell_write_metadata(command, project)

    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize(
    ("import_statement", "constructor"),
    [("from pathlib import Path", "Path"), ("import pathlib as lib", "lib.Path")],
)
def test_python_heredoc_identical_path_imports_keep_scratch_scope(
    tmp_path: Path, import_statement: str, constructor: str
) -> None:
    scratch, project = tmp_path / "scratch", tmp_path / "project"
    command = (
        f"uv run python - <<'EOF'\n{import_statement}\n"
        f"p = {constructor}('{scratch}/safe.txt')\n{import_statement}\np.write_text('x')\nEOF"
    )

    data = _shell_write_metadata(command, project)

    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is False
    assert data.get("canonical_repo_mutation_scope_unknown", False) is False


@pytest.mark.parametrize(
    "state_change",
    [
        "p._raw_paths = ['src/a.py']",
        "q = p\nq._raw_paths = ['src/a.py']",
        "parts = p._raw_paths\nparts.clear()\nparts.append('src/a.py')",
    ],
)
def test_python_heredoc_mutable_path_receiver_keeps_scope_unknown(
    tmp_path: Path, state_change: str
) -> None:
    scratch, project = tmp_path / "scratch", tmp_path / "project"
    command = (
        "uv run python - <<'EOF'\n"
        "from pathlib import Path\n"
        f"p = Path('{scratch}/safe.txt')\n"
        f"{state_change}\n"
        "p.write_text('x')\n"
        "EOF"
    )

    data = _shell_write_metadata(command, project)

    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize(
    "escape",
    [
        "redirect(p.write_text)",
        "method = p.write_text\nredirect(method)",
        "redirect(p.as_posix)",
        "method = p.as_posix\nredirect(method)",
        "redirect(p.absolute())",
        "redirect(p.expanduser())",
        "redirect(p.glob('*'))",
    ],
)
def test_python_heredoc_bound_path_receiver_escape_keeps_scope_unknown(
    tmp_path: Path, escape: str
) -> None:
    scratch, project = tmp_path / "scratch", tmp_path / "project"
    command = (
        "uv run python - <<'EOF'\n"
        "from pathlib import Path\n"
        "from redirector import redirect\n"
        f"p = Path('{scratch}/safe.txt')\n"
        f"{escape}\n"
        "p.write_text('x')\n"
        "EOF"
    )

    data = _shell_write_metadata(command, project)

    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize(
    "options",
    [
        pytest.param("-t 202601010000", id="stamp"),
        pytest.param("-t202601010000", id="attached-stamp"),
        pytest.param("-mt 202601010000", id="clustered-stamp"),
        pytest.param("-d 2026-01-01", id="date"),
        pytest.param("--date 2026-01-01", id="long-date"),
        pytest.param("--date=2026-01-01", id="long-date-equals"),
        pytest.param("-r ref.txt", id="reference"),
        pytest.param("--reference ref.txt", id="long-reference"),
        pytest.param("-A -01", id="adjust"),
        pytest.param("--time atime", id="long-time"),
    ],
)
def test_touch_option_values_are_not_write_targets(tmp_path: Path, options: str) -> None:
    project, scratch = tmp_path / "project", tmp_path / "scratch"

    data = _shell_write_metadata(f"touch {options} {scratch}/a", project)

    assert data["canonical_file_paths"] == [f"{scratch}/a"]
    assert data["canonical_repo_mutation"] is False


def test_scratchpad_touch_t_mv_chain_is_not_repo_mutation(tmp_path: Path) -> None:
    project, d = tmp_path / "project", tmp_path / "scratch" / "dry2"
    command = (
        f"mkdir -p {d}/oldspare {d}/run/cache && touch -t 202601010000 {d}/oldspare"
        f" && touch {d}/marker && sleep 1 && mkdir {d}/run/cache/xdg-cache-home"
        f" && mv {d}/oldspare {d}/run/cache/xdg-cache-home/pre-commit"
    )

    data = _shell_write_metadata(command, project)

    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is False
    assert "canonical_repo_mutation_scope_unknown" not in data


def test_touch_with_option_value_still_attributes_a_repo_path(tmp_path: Path) -> None:
    data = _shell_write_metadata("touch -t 202601010000 out.txt", tmp_path)

    assert data["canonical_file_paths"] == ["out.txt"]
    assert data["canonical_repo_mutation"] is True


@pytest.mark.parametrize(
    "options",
    [
        pytest.param("-m 755", id="mode"),
        pytest.param("-pm 755", id="clustered-mode"),
        pytest.param("--mode 755", id="long-mode"),
    ],
)
def test_mkdir_mode_value_is_not_a_write_target(tmp_path: Path, options: str) -> None:
    project, scratch = tmp_path / "project", tmp_path / "scratch"

    data = _shell_write_metadata(f"mkdir {options} {scratch}/d", project)

    assert data["canonical_file_paths"] == [f"{scratch}/d"]
    assert data["canonical_repo_mutation"] is False


@pytest.mark.parametrize(
    "template",
    [
        pytest.param("cp -t {project}/dest {scratch}/src", id="cp-short"),
        pytest.param("install -t {project}/dest {scratch}/src", id="install-short"),
        pytest.param("cp --target {project}/dest {scratch}/src", id="cp-abbreviated"),
        pytest.param("cp --target-dir={project}/dest {scratch}/src", id="cp-abbreviated-equals"),
        pytest.param("install --target-directory {project}/dest {scratch}/src", id="install-long"),
    ],
)
def test_copy_target_directory_option_is_the_write_target(tmp_path: Path, template: str) -> None:
    project, scratch = tmp_path / "project", tmp_path / "scratch"

    data = _shell_write_metadata(template.format(project=project, scratch=scratch), project)

    assert data["canonical_file_paths"] == [f"{project}/dest"]
    assert data["canonical_repo_mutation"] is True


@pytest.mark.parametrize("options", ["-d", "-dm 755", "--directory", "--dir"])
def test_install_directory_mode_writes_every_operand(tmp_path: Path, options: str) -> None:
    project, scratch = tmp_path / "project", tmp_path / "scratch"

    data = _shell_write_metadata(f"install {options} {project}/a {scratch}/b", project)

    assert data["canonical_file_paths"] == [f"{project}/a", f"{scratch}/b"]
    assert data["canonical_repo_mutation"] is True


def test_exact_long_flag_is_not_read_as_a_value_option_abbreviation() -> None:
    names = frozenset({"--strip-program", "--suffix"})

    assert _resolve_long_option("--strip", names) is None
    assert _resolve_long_option("--strip-p", names) == "--strip-program"
    assert _resolve_long_option("--s", names) is None


@pytest.mark.parametrize("options", ["-S .bak", "--suffix .bak"])
def test_mv_suffix_value_is_not_a_write_target(tmp_path: Path, options: str) -> None:
    project, scratch = tmp_path / "project", tmp_path / "scratch"

    data = _shell_write_metadata(f"mv {options} {scratch}/a {scratch}/b", project)

    assert data["canonical_file_paths"] == [f"{scratch}/a", f"{scratch}/b"]
    assert data["canonical_repo_mutation"] is False


@pytest.mark.parametrize("options", ["-d -S", "-dS", "-S -d", "-Sd"])
def test_bsd_install_safe_directory_mode_keeps_every_target(tmp_path: Path, options: str) -> None:
    project, scratch = tmp_path / "project", tmp_path / "scratch"

    data = _shell_write_metadata(f"install {options} {project}/a {scratch}/b", project)

    assert data["canonical_file_paths"] == [f"{project}/a", f"{scratch}/b"]
    assert data["canonical_repo_mutation"] is True


@pytest.mark.parametrize("options", ["-S .bak", "--suffix .bak"])
def test_install_file_mode_suffix_after_operands_keeps_destination(
    tmp_path: Path, options: str
) -> None:
    project, scratch = tmp_path / "project", tmp_path / "scratch"

    data = _shell_write_metadata(f"install {scratch}/a {project}/b {options}", project)

    assert data["canonical_file_paths"] == [f"{project}/b"]
    assert data["canonical_repo_mutation"] is True


def test_bsd_install_safe_directory_mode_keeps_dynamic_target_unknown(tmp_path: Path) -> None:
    data = _shell_write_metadata(f'install -d -S "$TARGET" {tmp_path}/scratch/b', tmp_path)

    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True
