from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from gobby.hooks._normalization_bindings import _BASH_LOOP_BINDING_UNSTABLE_PARAMETERS
from gobby.hooks._normalization_canonical import (
    _classify_shell_command,
    _classify_shell_segment_without_redirection,
)
from gobby.hooks._normalization_cwd import ShellDirectoryState
from gobby.hooks._normalization_metadata import _merge_shell_segment_metadata
from gobby.hooks._normalization_operands import _resolve_long_option
from gobby.hooks._normalization_segments import _ShellSegmentMetadata
from gobby.hooks._normalization_shell import _substitution_end
from gobby.hooks._normalization_tools import (
    _set_canonical_tool_metadata,
    normalize_tool_display_fields,
    normalize_tool_fields,
)
from gobby.hooks.shell_execution import SCRIPT_READ_BUDGET, read_script_body

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("command", "operands", "edits"),
    [
        (
            "cat connect.head connect.body > connect.rs",
            ["connect.head", "connect.body", "connect.rs"],
            ["connect.rs"],
        ),
        ("rm old.py > receipt.log", ["old.py", "receipt.log"], ["old.py", "receipt.log"]),
    ],
)
def test_redirected_command_preserves_operands_and_attributes_only_edits(
    tmp_path: Path, command: str, operands: list[str], edits: list[str]
) -> None:
    data = _invocation_metadata(
        command,
        tool_cwd=tmp_path,
        project_path=tmp_path,
    )

    assert data["canonical_repo_mutation"] is True
    assert [tmp_path / path for path in data["canonical_file_paths"]] == [
        tmp_path / path for path in operands
    ]
    assert [tmp_path / path for path in data["canonical_edit_file_paths"]] == [
        tmp_path / path for path in edits
    ]


@pytest.mark.parametrize(
    "command",
    [
        "(case x in x) git apply x.patch;; esac)",
        "echo $(case y in y) git apply x.patch;; esac)",
    ],
)
def test_grouped_case_pathless_writes_fail_closed(tmp_path: Path, command: str) -> None:
    data = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


def test_grouped_case_touch_keeps_resolved_scope(tmp_path: Path) -> None:
    data = _invocation_metadata(
        "(case x in x) touch a.py;; esac)", tool_cwd=tmp_path, project_path=tmp_path
    )
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_file_paths"] == ["a.py"]
    assert data.get("canonical_repo_mutation_scope_unknown") is None


def test_grouped_case_copy_keeps_resolved_scope(tmp_path: Path) -> None:
    data = _invocation_metadata(
        "(case x in x) cp a b;; esac)", tool_cwd=tmp_path, project_path=tmp_path
    )
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_file_paths"] == ["b"]
    assert data.get("canonical_repo_mutation_scope_unknown") is None


@pytest.mark.parametrize(
    "command",
    [
        "shopt -s extglob\ncase x in @(x|y)) git apply x.patch;; esac",
        'case x in(x)"git" apply x.patch;; esac',
        "case x in(x)'git' apply x.patch;; esac",
        "case x in(x)\\git apply x.patch;; esac",
        'case x in(x)g"it" apply x.patch;; esac',
        'case "x)" in("x)")git apply x.patch;; esac',
        'case x in("x")git apply x.patch;; esac',
    ],
)
def test_case_pattern_headers_classify_pathless_writes(tmp_path: Path, command: str) -> None:
    data = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True
    assert data.get("canonical_file_paths") is None


@pytest.mark.parametrize("command", ['case x in(x)"cp" a b;; esac', 'case x in("x")cp a b;; esac'])
def test_case_pattern_headers_classify_copy_scope(tmp_path: Path, command: str) -> None:
    data = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_file_paths"] == ["b"]
    assert data.get("canonical_repo_mutation_scope_unknown") is None


def test_quote_led_case_header_is_not_an_arm(tmp_path: Path) -> None:
    data = _invocation_metadata(
        'case x "in(x)git" apply x.patch;; esac', tool_cwd=tmp_path, project_path=tmp_path
    )
    assert data["canonical_tool_kind"] == "execute"
    assert not data.get("canonical_repo_mutation")


def test_malformed_grouped_case_classification_does_not_raise(tmp_path: Path) -> None:
    data = _invocation_metadata(
        "(case x in x) gem push x)", tool_cwd=tmp_path, project_path=tmp_path
    )
    assert isinstance(data.get("canonical_repo_mutation", False), bool)


def test_malformed_case_arm_tail_does_not_raise(tmp_path: Path) -> None:
    data = _invocation_metadata("case;<())(", tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize(
    "command,paths",
    [
        ("case x in a)(touch a.py);; esac", ["a.py"]),
        ("case x in a) (touch a.py);; esac", ["a.py"]),
        ("case x in a)(rm -rf src);; esac", ["src"]),
        ("case x in a) (rm -rf src);; esac", ["src"]),
        ("case x in (a)(touch a.py);; esac", ["a.py"]),
        ("case x in (a) (touch a.py);; esac", ["a.py"]),
        ("case x in a)(touch a.py)&&(touch b.py);; esac", ["a.py", "b.py"]),
        ("case x in a) (touch a.py)&&(touch b.py);; esac", ["a.py", "b.py"]),
        ("case x in ${x:-(})touch a.py;; esac", ["a.py"]),
        ("case x in ${x:-)})touch a.py;; esac", ["a.py"]),
    ],
)
def test_case_arm_boundary_preserves_write_scope(
    tmp_path: Path, command: str, paths: list[str]
) -> None:
    data = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_file_paths"] == paths
    assert data.get("canonical_repo_mutation_scope_unknown") is None


@pytest.mark.parametrize(
    "body,paths",
    [
        ("touch a.py; touch b.py", ["a.py", "b.py"]),
        ("touch a.py\ntouch b.py", ["a.py", "b.py"]),
        ("cd sub && touch a.py", ["sub/a.py"]),
    ],
)
def test_glued_case_arm_subshell_list_write_scope(
    tmp_path: Path, body: str, paths: list[str]
) -> None:
    data = _invocation_metadata(
        f"case x in a)({body});; esac", tool_cwd=tmp_path, project_path=tmp_path
    )
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_file_paths"] == paths
    assert data.get("canonical_repo_mutation_scope_unknown") is None


@pytest.mark.parametrize("separator", ["", " "])
def test_case_arm_subshell_list_read_only(tmp_path: Path, separator: str) -> None:
    data = _invocation_metadata(
        f"case x in a){separator}(echo hi; echo there);; esac",
        tool_cwd=tmp_path,
        project_path=tmp_path,
    )
    assert data["canonical_tool_kind"] == "execute"
    assert not data.get("canonical_repo_mutation")
    assert not data.get("canonical_file_paths")


@pytest.mark.parametrize(
    "prefix",
    [
        "case x in a) echo $(true);; b)",
        "case x in a) x=$(true);; b)",
        "case x in a) true $(true);; b)",
        "case x in a) echo ${x/)/};; b)",
        "case $(echo x) in b)",
        "case ${x/)/} in b)",
        "case x in a) echo `true`;; b)",
        "case $(case x in x) echo x;; esac) in b)",
        "case $(case $(echo x) in x) echo x;; esac) in b)",
        "case x in $${x})",
        "case x in a)\\\n",
    ],
)
def test_case_arm_after_expansion_preserves_write_scope(tmp_path: Path, prefix: str) -> None:
    data = _invocation_metadata(
        f"{prefix}(touch a.py; touch b.py);; esac", tool_cwd=tmp_path, project_path=tmp_path
    )
    assert data["canonical_file_paths"] == ["a.py", "b.py"]
    assert data.get("canonical_repo_mutation_scope_unknown") is None


@pytest.mark.parametrize(
    "command",
    [
        "echo `case y in y) git apply x.patch;; esac`",
        "`case y in y) git apply x.patch;; esac`",
        "cat <(case y in (y) git apply x.patch;; esac)",
        "echo >(case y in (y) git apply x.patch;; esac)",
    ],
)
def test_existing_grouped_case_writes_remain_unresolved(tmp_path: Path, command: str) -> None:
    data = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize(
    "command,kind",
    [
        ("diff <(a) <(b)", "execute"),
        ("diff <(cat a) <(cat b)", "read"),
        ("(diff <(cat a) <(cat b))", "read"),
        ("echo $(echo a)", "execute"),
        ("$(( (1+2) ))", "execute"),
        ("case x in (a|b) true;; esac", "execute"),
    ],
)
def test_grouped_case_controls_keep_non_write_scope(
    tmp_path: Path, command: str, kind: str
) -> None:
    data = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_tool_kind"] == kind
    assert not data.get("canonical_repo_mutation")
    assert data.get("canonical_repo_mutation_scope_unknown") is None


@pytest.mark.parametrize(
    "command,unknown,paths",
    [
        ("case x in(x)git apply x.patch;; esac", True, None),
        ("case x in(x) git apply x.patch;; esac", True, None),
        ("case x in(x)cp a b;; esac", None, ["b"]),
        ("case x in(x|y) git apply x.patch;; esac", True, None),
        ("case x in (x) git apply x.patch;; esac", True, None),
    ],
)
def test_case_header_unspaced_keyword_write_scope(
    tmp_path: Path, command: str, unknown: bool | None, paths: list[str] | None
) -> None:
    data = normalize_tool_fields(
        {
            "tool_name": "Bash",
            "tool_input": {"command": command, "cwd": str(tmp_path)},
            "project_path": str(tmp_path),
        }
    )
    assert (
        data.get("canonical_tool_kind"),
        data.get("canonical_repo_mutation"),
        data.get("canonical_repo_mutation_scope_unknown"),
        data.get("canonical_script_execution"),
        data.get("canonical_file_paths"),
    ) == ("write", True, unknown, None, paths)


@pytest.mark.parametrize(
    "command,kind,mutation,unknown,paths",
    [
        pytest.param("case x in x)git apply x.patch;; esac", "write", True, True, None, id="G1"),
        pytest.param("case x in (x)git apply x.patch;; esac", "write", True, True, None, id="G3"),
        pytest.param("case x in x|y)git apply x.patch;; esac", "write", True, True, None, id="G4"),
        pytest.param("case x in *)git apply x.patch;; esac", "write", True, True, None, id="G5"),
        pytest.param(
            "case x in x) echo ok;; y)git apply x.patch;; esac",
            "write",
            True,
            True,
            None,
            id="G6",
        ),
        pytest.param("case x in x)git apply x.patch;;esac", "write", True, True, None, id="G7"),
        pytest.param('case x in "x")git apply x.patch;; esac', "write", True, True, None, id="G8"),
        pytest.param(
            "case x in x) echo ok;& y)git apply x.patch;; esac",
            "write",
            True,
            True,
            None,
            id="G9",
        ),
        pytest.param(
            "case x in\nx)git apply x.patch\n;;\nesac",
            "write",
            True,
            True,
            None,
            id="G10",
        ),
        pytest.param(
            "case x in x) echo ok;;& y)git apply x.patch;; esac",
            "write",
            True,
            True,
            None,
            id="G12",
        ),
        pytest.param(
            "case x in 'x)' | y) git apply x.patch;; esac", "write", True, True, None, id="Q1"
        ),
        pytest.param(
            "case x in $(echo x) | y) git apply x.patch;; esac", "write", True, True, None, id="Q2"
        ),
        pytest.param(
            'case x in "x)" ) git apply x.patch;; esac', "write", True, True, None, id="Q3"
        ),
        pytest.param(
            "case x in $(echo x) ) git apply x.patch;; esac", "write", True, True, None, id="Q4"
        ),
        pytest.param(
            "case x in $'x)' | y) git apply x.patch;; esac", "write", True, True, None, id="Q5"
        ),
        pytest.param(
            r"case x in \) | y) git apply x.patch;; esac", "write", True, True, None, id="Q6"
        ),
        pytest.param(
            'case x in "$(echo x)" | y) git apply x.patch;; esac',
            "write",
            True,
            True,
            None,
            id="Q7",
        ),
        pytest.param(
            "case 1 in $((1)) | 2) git apply x.patch;; esac", "write", True, True, None, id="Q8"
        ),
        pytest.param(
            "case x in y | 'x)' | z) git apply x.patch;; esac", "write", True, True, None, id="Q10"
        ),
        pytest.param("case x in x)cp a b;; esac", "write", True, None, ["b"], id="G2"),
        pytest.param("case x in 'x)' | y)cp a b;; esac", "write", True, None, ["b"], id="Q9"),
        pytest.param("case x in x)cat a.py;; esac", "read", None, None, ["a.py"], id="G11"),
    ],
)
def test_case_pattern_terminator_boundary(
    tmp_path: Path,
    command: str,
    kind: str,
    mutation: bool | None,
    unknown: bool | None,
    paths: list[str] | None,
) -> None:
    data = normalize_tool_fields(
        {
            "tool_name": "Bash",
            "tool_input": {"command": command, "cwd": str(tmp_path)},
            "project_path": str(tmp_path),
        }
    )
    assert (
        data.get("canonical_tool_kind"),
        data.get("canonical_repo_mutation"),
        data.get("canonical_repo_mutation_scope_unknown"),
        data.get("canonical_script_execution"),
        data.get("canonical_file_paths"),
    ) == (kind, mutation, unknown, None, paths)


@pytest.mark.parametrize(
    "command,kind,mutation,unknown,paths",
    [
        pytest.param("case x in x) git apply x.patch;; esac", "write", True, True, None, id="C1"),
        pytest.param(
            'case x in "x)") git apply x.patch;; esac', "write", True, True, None, id="C2"
        ),
        pytest.param(
            r"case x in x\)) git apply x.patch;; esac", "write", True, True, None, id="C3"
        ),
        pytest.param(
            "case x in $(echo x)) git apply x.patch;; esac", "write", True, True, None, id="C4"
        ),
        pytest.param(
            'case x in "$(echo x)") git apply x.patch;; esac', "write", True, True, None, id="C5"
        ),
        pytest.param(
            'case x in x) echo "a)"; git apply x.patch;; esac', "write", True, True, None, id="C6"
        ),
        pytest.param(
            "case x in x) echo $(echo a); git apply x.patch;; esac",
            "write",
            True,
            True,
            None,
            id="C7",
        ),
        pytest.param(
            'case x in x | "y)") git apply x.patch;; esac', "write", True, True, None, id="C8"
        ),
        pytest.param("case x in x) echo ok;; esac", "execute", None, None, None, id="C9"),
        pytest.param("case x in x)echo ok;; esac", "execute", None, None, None, id="C10"),
        pytest.param("case x in x) cat a.py;; esac", "read", None, None, ["a.py"], id="C11"),
        pytest.param('case x in "x)") cat a.py;; esac', "read", None, None, ["a.py"], id="C12"),
        pytest.param("case x in x) cp a b;; esac", "write", True, None, ["b"], id="C13"),
        pytest.param("(cp a b)", "write", True, None, ["b"], id="C14"),
        pytest.param(
            'echo "case x in x)git apply x.patch;; esac"', "execute", None, None, None, id="C16"
        ),
        pytest.param("echo $(echo a)b", "execute", None, None, None, id="C17"),
    ],
)
def test_case_pattern_terminator_controls(
    tmp_path: Path,
    command: str,
    kind: str,
    mutation: bool | None,
    unknown: bool | None,
    paths: list[str] | None,
) -> None:
    data = normalize_tool_fields(
        {
            "tool_name": "Bash",
            "tool_input": {"command": command, "cwd": str(tmp_path)},
            "project_path": str(tmp_path),
        }
    )
    assert (
        data.get("canonical_tool_kind"),
        data.get("canonical_repo_mutation"),
        data.get("canonical_repo_mutation_scope_unknown"),
        data.get("canonical_script_execution"),
        data.get("canonical_file_paths"),
    ) == (kind, mutation, unknown, None, paths)


def test_case_pattern_terminator_unterminated_arm_does_not_raise(tmp_path: Path) -> None:
    data = normalize_tool_fields(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "case x in x)git apply x.patch", "cwd": str(tmp_path)},
            "project_path": str(tmp_path),
        }
    )
    assert (
        data["canonical_tool_kind"],
        data.get("canonical_repo_mutation"),
        data.get("canonical_repo_mutation_scope_unknown"),
    ) == ("write", True, True)


@pytest.mark.parametrize(
    "command, expected",
    [
        ("{ cd sub; } > log.txt; touch y.py", ["log.txt", "sub/y.py"]),
        ("{ cd sub; echo }; } | cat; touch y.py", ["y.py"]),
        ("cd sub || exit 1; touch y.py", ["sub/y.py"]),
        ("cd sub || exit 1; git add x.py", ["sub/x.py"]),
        ("cd sub || return 1; touch y.py", ["sub/y.py"]),
        ("cd sub & touch x.py", ["x.py"]),
    ],
)
def test_compound_cwd_followups(tmp_path: Path, command: str, expected: list[str]) -> None:
    data = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_file_paths"] == expected
    assert not data.get("canonical_repo_mutation_scope_unknown")


@pytest.mark.parametrize("separator", ["|", "&&", "||", "&"])
@pytest.mark.parametrize("continuation", ["\n", " # c\n", "\n\n"])
def test_chain_newline_preserves_cwd_scope(
    tmp_path: Path, separator: str, continuation: str
) -> None:
    tail = "cat; touch e.py" if separator == "|" else "touch e.py"
    single_line = _invocation_metadata(
        f"cd sub {separator} {tail}", tool_cwd=tmp_path, project_path=tmp_path
    )
    continued = _invocation_metadata(
        f"cd sub {separator}{continuation} {tail}", tool_cwd=tmp_path, project_path=tmp_path
    )
    assert continued.get("canonical_file_paths") == single_line.get("canonical_file_paths")
    assert continued.get("canonical_repo_mutation_scope_unknown") == single_line.get(
        "canonical_repo_mutation_scope_unknown"
    )
    if separator in {"|", "&"}:
        assert continued["canonical_file_paths"] == ["e.py"]


@pytest.mark.parametrize(
    ("command", "expected_paths", "expected_unknown"),
    [
        ("builtin cd sub; touch y.py", ["sub/y.py"], None),
        ('eval "builtin cd sub"; touch y.py', None, True),
        ('eval "f() { cd sub; }"; f; touch y.py', None, True),
    ],
)
def test_indirect_cwd_followups(
    tmp_path: Path, command: str, expected_paths: list[str] | None, expected_unknown: bool | None
) -> None:
    data = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    assert data.get("canonical_file_paths") == expected_paths
    assert data.get("canonical_repo_mutation_scope_unknown") is expected_unknown


@pytest.mark.parametrize("operand", ["-Cd=e", "-C{absolute}"])
def test_attached_env_directory_preserves_equals(tmp_path: Path, operand: str) -> None:
    directory = tmp_path / "d=e"
    argument = operand.format(absolute=directory)
    data = _invocation_metadata(
        f"env {argument} touch x.py", tool_cwd=tmp_path, project_path=tmp_path
    )
    assert data["canonical_file_paths"] == [str(directory / "x.py")]


@pytest.mark.parametrize("wrapper", ["env -C d=e", "env --chdir=d=e", "env -Csub"])
def test_env_directory_operand_controls(tmp_path: Path, wrapper: str) -> None:
    directory = "sub" if wrapper.endswith("-Csub") else "d=e"
    data = _invocation_metadata(f"{wrapper} touch x.py", tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_file_paths"] == [str(tmp_path / directory / "x.py")]


@pytest.mark.parametrize(
    "wrapper",
    ["FOO=1 time", "env time", "nohup time", "command time", '"time"', "env time -p --"],
)
def test_external_time_preserves_write_scope(tmp_path: Path, wrapper: str) -> None:
    data = _invocation_metadata(f"{wrapper} touch x.py", tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_file_paths"] == ["x.py"]
    assert data["canonical_repo_mutation"] is True


@pytest.mark.parametrize("wrapper", ["sh -c", "bash -c", "zsh -c", "eval", "env sh -c"])
def test_nested_dollar_pair_quote_write_fails_closed(tmp_path: Path, wrapper: str) -> None:
    program = r"""echo \$\$'\\' ; git apply x.patch ; echo '\\'"""
    command = f'{wrapper} "{program}"'
    data = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


def test_nested_shell_parse_error_write_fails_closed(tmp_path: Path) -> None:
    command = '''sh -c "echo 'unterminated"'''
    data = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize(
    "body",
    ["echo $$'\\' ; git apply x.patch ; echo '\\'", "git apply x.patch"],
)
def test_nested_shell_parse_heredoc_reports_mutation(tmp_path: Path, body: str) -> None:
    data = _invocation_metadata(
        f"sh <<'EOF'\n{body}\nEOF", tool_cwd=tmp_path, project_path=tmp_path
    )
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize(
    "command,kind,paths,unknown",
    [
        ('sh -c "cat a.py"', "read", ["a.py"], None),
        ('sh -c "git status"', "execute", None, None),
        (r'''sh -c "echo \$\$'.txt'"''', "execute", None, None),
        (r'''bash -c "echo \$'a\\n'"''', "execute", None, None),
        ('sh -c "cp a b"', "write", ["b"], None),
        ('''sh -c "sh -c 'git apply x.patch'"''', "write", None, True),
    ],
)
def test_nested_shell_parse_controls_keep_scope(
    tmp_path: Path, command: str, kind: str, paths: list[str] | None, unknown: bool | None
) -> None:
    data = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_tool_kind"] == kind
    assert bool(data.get("canonical_repo_mutation")) is (kind == "write")
    assert data.get("canonical_file_paths") == paths
    assert data.get("canonical_repo_mutation_scope_unknown") is unknown


@pytest.mark.parametrize(
    "command,kind,paths,unknown",
    [
        ("bash -c 'cat src/long.py; touch /tmp/x'", "write", ["/tmp/x"], None),
        ("( cat src/long.py; touch /tmp/x )", "write", ["/tmp/x"], None),
        ('for f in src/long.py; do cat "$f"; touch /tmp/x; done', "write", ["/tmp/x"], None),
        ('for f in src/long.py; do cat "$f"; done; touch /tmp/x', "write", ["/tmp/x"], None),
        ("bash -c 'sed -n p src/long.py; touch /tmp/x'", "write", ["/tmp/x"], None),
        ('for f in src/long.py; do sed -n "${start},${end}p" "$f"; done', "write", None, True),
        ('for f in src/long.py; do sed -n -f script.sed "$f"; done', "write", ["/tmp/x"], None),
        (
            'for f in src/long.py; do sed -n "/value/w out.txt" "$f"; done',
            "write",
            ["out.txt"],
            None,
        ),
        ("bash -c 'sed -n \"${start},${end}p\" src/long.py'", "write", None, True),
        ("sh -c 'sed -n \"${start},${end}p\" src/long.py'", "write", None, True),
        ('( sed -n "${start},${end}p" src/long.py )', "write", None, True),
        ("bash -c \"sed -n '/value/w /tmp/x' src/long.py\"", "write", ["/tmp/x"], None),
        ("( sed -n -f script.sed src/long.py )", "write", ["/tmp/x"], None),
        ("bash -c 'cat src/long.py'", "read", ["src/long.py"], None),
        ("{ cat src/long.py; touch /tmp/x; }", "write", ["/tmp/x"], None),
        ('for f in src/long.py; do cat "$f"; done', "read", ["src/long.py"], None),
    ],
)
def test_nested_and_loop_bound_reads_preserve_mutation_scope(
    tmp_path: Path, command: str, kind: str, paths: list[str] | None, unknown: bool | None
) -> None:
    (tmp_path / "script.sed").write_text("w /tmp/x\n")
    data = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_tool_kind"] == kind
    assert data.get("canonical_file_paths") == paths
    assert data.get("canonical_repo_mutation_scope_unknown") is unknown


@pytest.mark.parametrize(
    "command",
    [
        """for f in src/long.py; do bash -c 'cat "$f"'; touch /tmp/x; done""",
        """for f in src/long.py; do sh -c 'cat "$f"'; touch /tmp/x; done""",
        'for f in src/long.py; do ( cat "$f" ); touch /tmp/x; done',
        """for f in src/long.py; do bash -c 'sed -n p "$f"; touch /tmp/x'; done""",
        'for f in src/long.py; do ( sed -n "/value/w /tmp/x" "$f" ); done',
        """for f in src/long.py; do bash -c 'cat "$f"'; done; touch /tmp/x""",
        'for f in src/long.py; do ( cat "$f"; touch /tmp/x ); done',
    ],
)
def test_nested_wrapper_loop_reads_keep_only_mutation_paths(tmp_path: Path, command: str) -> None:
    data = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_file_paths"] == ["/tmp/x"]
    assert data.get("canonical_repo_mutation_scope_unknown") is None


@pytest.mark.parametrize(
    "command",
    [
        "echo $$'\\' ; git apply x.patch ; echo '\\'",
        "echo $(echo $$'\\') ; git apply x.patch ; echo ')'\\'",
        "echo $$'\\'' ; git apply x.patch ; echo \\'",
    ],
)
def test_dollar_pair_quote_write_fails_closed(tmp_path: Path, command: str) -> None:
    data = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize("command", ["echo $$'.txt'", "echo \\$$'\\''", "echo $$$'it\\'s'"])
def test_dollar_pair_quote_literals_stay_non_write(tmp_path: Path, command: str) -> None:
    data = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_tool_kind"] == "execute"
    assert not data.get("canonical_repo_mutation")
    assert data.get("canonical_repo_mutation_scope_unknown") is None


@pytest.mark.parametrize("wrapper", ["env -C other", "env --chdir=other"])
@pytest.mark.parametrize("operation", ["add x.py", "revert --quit"])
def test_env_directory_rebases_git_scope(tmp_path: Path, wrapper: str, operation: str) -> None:
    data = _invocation_metadata(
        f"{wrapper} git {operation}", tool_cwd=tmp_path, project_path=tmp_path
    )
    target = tmp_path / "other"
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_file_paths"] == [
        str(target / "x.py") if operation.startswith("add") else str(target)
    ]


@pytest.mark.parametrize("selector", ["GIT_DIR", "GIT_WORK_TREE"])
@pytest.mark.parametrize("operation", ["add x.py", "revert --quit"])
@pytest.mark.parametrize("wrapper", ["{}", "sh -c '{}'"])
@pytest.mark.parametrize(
    "setup",
    [
        "export {selector}=/other",
        "declare -x {selector}=/other",
        "typeset -x {selector}=/other",
        "set -a; {selector}=/other",
        "{selector}=/other",
        "readonly {selector}=/other",
        "local -x {selector}=/other",
        "declare -x {selector}",
    ],
)
def test_exported_git_selector_never_falls_back_to_cwd(
    tmp_path: Path, selector: str, operation: str, wrapper: str, setup: str
) -> None:
    data = _invocation_metadata(
        f"{setup.format(selector=selector)} && {wrapper.format('git ' + operation)}",
        tool_cwd=tmp_path,
    )
    assert data["canonical_repo_mutation"] is True
    assert data.get("canonical_file_paths", []) == []
    assert data["canonical_repo_mutation_scope_unknown"] is True
    ordinary = _invocation_metadata("git add x.py", tool_cwd=tmp_path)
    assert ordinary["canonical_file_paths"] == [str(tmp_path / "x.py")]


def test_env_directory_named_git_cannot_hide_repository_selector(tmp_path: Path) -> None:
    data = _invocation_metadata(
        "env -C git GIT_DIR=/other git add x.py", tool_cwd=tmp_path, project_path=tmp_path
    )
    assert data["canonical_repo_mutation_scope_unknown"] is True
    assert data.get("canonical_file_paths", []) == []


@pytest.mark.parametrize(
    "command",
    [
        "case x in (x) touch c.py;; esac",
        "case x in x) touch c.py;; y) touch d.py;; esac",
        "case x in x|y) touch c.py;; esac",
        "case x in x) (touch c.py);; esac",
        "cat <(touch c.py)",
        "cat <(cd other && touch c.py)",
        "cat >(touch c.py)",
    ],
)
def test_compound_bodies_attribute_writes(tmp_path: Path, command: str) -> None:
    data = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    expected = "other/c.py" if "cd other" in command else "c.py"
    assert data["canonical_repo_mutation"] is True
    assert expected in data["canonical_file_paths"]


@pytest.mark.parametrize("source", ["~user", "~", "~+", "~-"])
def test_tilde_copy_source_keeps_directory_scope(tmp_path: Path, source: str) -> None:
    data = _invocation_metadata(f"cp -R {source} tests/", tool_cwd=tmp_path)
    assert data["canonical_file_paths"] == ["tests/"]


def test_env_directory_does_not_rebase_parent_redirect_or_substitution(tmp_path: Path) -> None:
    data = _invocation_metadata(
        "env -C other cat <(touch p.py) > out.py; touch next.py",
        tool_cwd=tmp_path,
        project_path=tmp_path,
    )
    assert set(data["canonical_file_paths"]) == {"p.py", "out.py", "next.py"}


@pytest.mark.parametrize("directory", ["$D", "~user", "*"])
def test_dynamic_env_directory_fails_closed(tmp_path: Path, directory: str) -> None:
    data = _invocation_metadata(
        f"env -C {directory} git add x.py", tool_cwd=tmp_path, project_path=tmp_path
    )
    assert data["canonical_repo_mutation"] is True
    assert data.get("canonical_file_paths", []) == []
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize("wrapper", ["env -C other env -C child", "X=1 command env -C other"])
def test_nested_env_directory_changes_rebase_git(tmp_path: Path, wrapper: str) -> None:
    data = _invocation_metadata(f"{wrapper} git add x.py", tool_cwd=tmp_path)
    target = tmp_path / ("other/child/x.py" if "child" in wrapper else "other/x.py")
    assert data["canonical_file_paths"] == [str(target)]


@pytest.mark.parametrize(
    ("wrapper", "expected"),
    [
        ("env -C a -C b", "b/x.py"),
        ("env --chdir=a --chdir=b", "b/x.py"),
        ("env -C a env -C b", "a/b/x.py"),
        ("env -C a -C b env -C c -C d", "b/d/x.py"),
    ],
)
def test_env_uses_last_chdir_per_invocation(tmp_path: Path, wrapper: str, expected: str) -> None:
    data = _invocation_metadata(f"{wrapper} touch x.py", tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_file_paths"] == [str(tmp_path / expected)]


def test_case_pattern_alternation_preserves_arm_directory(tmp_path: Path) -> None:
    data = _invocation_metadata(
        "case x in x|y) cd sub; touch c.py;; esac", tool_cwd=tmp_path, project_path=tmp_path
    )
    assert data["canonical_file_paths"] == ["sub/c.py"]


@pytest.mark.parametrize(
    "command",
    [
        "case $(git apply change.patch) in x) ;; esac",
        "case x in $(git apply change.patch)) ;; esac",
        "case `git apply change.patch` in x) ;; esac",
        "case <(git apply change.patch) in x) ;; esac",
        "case x in <(git apply change.patch)) ;; esac",
        "case x in (<(git apply change.patch)) ;; esac",
        "case x in (y|<(git apply change.patch)) ;; esac",
        "case x in (y|>(git apply change.patch)) ;; esac",
        "case x in ( y | <(git apply change.patch) ) ;; esac",
        "case x in (y) true;; (<(git apply change.patch)) ;; esac",
    ],
)
def test_case_subject_and_pattern_substitutions_attribute_pathless_writes(
    tmp_path: Path, command: str
) -> None:
    data = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


def test_case_subject_substitution_keeps_resolved_write_scope(tmp_path: Path) -> None:
    data = _invocation_metadata(
        "case $(cp a b) in x) git status;; esac", tool_cwd=tmp_path, project_path=tmp_path
    )
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_file_paths"] == ["b"]
    assert data.get("canonical_repo_mutation_scope_unknown") is None


@pytest.mark.parametrize(
    "command",
    [
        "case $x in a) echo hi;; esac",
        "case x in (a) echo hi;; esac",
        "case x in (a|b) echo hi;; esac",
        "case x in x) diff <(cat a) <(cat b);; esac",
    ],
)
def test_case_literal_subject_and_pattern_stay_non_write(tmp_path: Path, command: str) -> None:
    data = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_tool_kind"] != "write"
    assert not data.get("canonical_repo_mutation")
    assert data.get("canonical_repo_mutation_scope_unknown") is None


@pytest.mark.parametrize(
    "command",
    [
        "(cd sub && cat <(touch p.py))",
        "case x in x) (cd sub && cat <(touch p.py));; esac",
    ],
)
def test_case_arm_subshell_keeps_process_substitution_directory(
    tmp_path: Path, command: str
) -> None:
    data = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_file_paths"] == ["sub/p.py"]
    assert data.get("canonical_repo_mutation_scope_unknown") is None


def test_process_substitution_keeps_literal_write_after_directory_change(tmp_path: Path) -> None:
    data = _invocation_metadata(
        "cd sub && cat <(touch p.py)", tool_cwd=tmp_path, project_path=tmp_path
    )
    assert data["canonical_file_paths"] == ["sub/p.py"]
    assert data["canonical_repo_mutation"] is True


@pytest.mark.parametrize("command", ["echo 'cat <(touch p.py)'", "cat '<(touch p.py)'"])
def test_quoted_process_substitution_text_is_not_a_write(tmp_path: Path, command: str) -> None:
    data = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    assert not data.get("canonical_repo_mutation")


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
    "command",
    [
        "source /tmp/scratchpad/probe.sh",
        ". /tmp/scratchpad/probe.sh",
        "bash -c 'source /tmp/scratchpad/probe.sh'",
    ],
)
def test_sourced_script_requires_conservative_mutation_scope(tmp_path: Path, command: str) -> None:
    data = _shell_write_metadata(command, tmp_path)

    assert data["canonical_tool_kind"] == "write"
    assert data.get("canonical_script_execution") is True
    assert data.get("canonical_repo_mutation") is True
    assert data.get("canonical_repo_mutation_scope_unknown") is True


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
    ("prefix", "outcome"),
    [
        (b"#!/bin/sh\ntouch x", "write"),
        (b"#!/usr/bin/env -S bash -e\ntouch x", "write"),
        # A shell runs executable text without `#!` as a script once execve fails.
        (b"touch x\n", "write"),
        (b"", "clean"),
        # Magic alone is not native: a first line without a NUL still runs as a script.
        (b"\x7fELF\ntouch x\n", "write"),
        # That script is not UTF-8, so its body stays opaque.
        (b"\xcf\xfa\xed\xfe\ntouch x\n", "gated"),
        (b"\x7fELF\x02\x01\x01\x00", "clean"),
        (b"\xcf\xfa\xed\xfe\x0c\x00\x00\x01", "clean"),
        (b"\xca\xfe\xba\xbe\x00\x00\x00\x02", "clean"),
        # A non-shell interpreter runs the file like `python3 script.py`.
        (b"#!/usr/bin/env python3\nopen('x', 'w')\n", "clean"),
        (b"#!/opt/venv/bin/python\nimport sys\n", "clean"),
        # An env option with a value hides which interpreter runs.
        (b"#!/usr/bin/env -u HOME python3\n", "gated"),
        (b"#!\ntouch x\n", "gated"),
    ],
)
@pytest.mark.parametrize("relative", [False, True])
def test_direct_path_execution_classifies_shell_script_bodies(
    tmp_path: Path, prefix: bytes, outcome: str, relative: bool
) -> None:
    executable = tmp_path / "scratchpad" / "bash"
    executable.parent.mkdir()
    executable.write_bytes(prefix)
    command = "./scratchpad/bash" if relative else str(executable)
    data = _shell_write_metadata(command, tmp_path)

    assert bool(data.get("canonical_script_execution")) is (outcome == "gated")
    assert bool(data.get("canonical_repo_mutation")) is (outcome == "write")


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


@pytest.mark.parametrize(
    "command, expected",
    [
        ("{ cd sub; touch x.py; }; touch y.py", ["sub/x.py", "sub/y.py"]),
        ("( cd sub; touch x.py ); touch y.py", ["sub/x.py", "y.py"]),
        ("{ cd sub; } | cat; touch y.py", ["y.py"]),
        ("f()\n{ cd sub; }\ntouch y.py", ["y.py"]),
        ("f ()\n{ cd sub; }\ntouch y.py", ["y.py"]),
        ("function f\n{ cd sub; }\ntouch y.py", ["y.py"]),
        ("function f()\n{ cd sub; }\ntouch y.py", ["y.py"]),
        ("my-f()\n{ cd sub; }\ntouch y.py", ["y.py"]),
        ("function my-f\n{ cd sub; }\ntouch y.py", ["y.py"]),
        ("! cd sub; touch x.py", ["sub/x.py"]),
        ("! { cd sub; }; touch y.py", ["sub/y.py"]),
        ("{ :; } > out.log", ["out.log"]),
    ],
)
def test_grammar_group_directory_scope(tmp_path: Path, command: str, expected: list[str]) -> None:
    data = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_file_paths"] == expected


@pytest.mark.parametrize("operation", ["touch x.py", "rm -rf src", "sed -i s/a/b/ x.py"])
@pytest.mark.parametrize(
    "template",
    [
        "{{ {}; }}",
        "! {}",
        "if ! {}; then :; fi",
        "time {}",
        "time -p {}",
        "time -- {}",
        "time -p -- {}",
    ],
)
def test_grammar_prefix_preserves_write_classification(
    tmp_path: Path, template: str, operation: str
) -> None:
    wrapped = _invocation_metadata(
        template.format(operation), tool_cwd=tmp_path, project_path=tmp_path
    )
    bare = _invocation_metadata(operation, tool_cwd=tmp_path, project_path=tmp_path)
    for key in ("canonical_tool_kind", "canonical_file_paths", "canonical_repo_mutation"):
        assert wrapped.get(key) == bare.get(key)


@pytest.mark.parametrize(
    "command",
    [
        "! { touch x.py; }",
        "time { touch x.py; }",
        "time -p { touch x.py; }",
        "if ! { touch x.py; }; then :; fi",
        "! time { touch x.py; }",
    ],
)
def test_combined_grammar_prefixes_preserve_write_classification(
    tmp_path: Path, command: str
) -> None:
    grouped = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    bare = _invocation_metadata("touch x.py", tool_cwd=tmp_path, project_path=tmp_path)
    for key in ("canonical_tool_kind", "canonical_file_paths", "canonical_repo_mutation"):
        assert grouped.get(key) == bare.get(key)


@pytest.mark.parametrize(
    "command",
    [
        "cd sub || touch x.py",
        "{ cd sub; } || touch x.py",
        "{ cd sub; } & touch x.py",
        "cd sub && if true; then true; fi || touch x.py",
        "cd sub && while false; do true; done || touch x.py",
        "cd sub && case x in x) true;; esac || touch x.py",
        "cd sub && { true; } || touch x.py",
        "{ cd sub; make; } || true; touch x.py",
        "false || cd sub; touch x.py",
        "false || cd sub && touch x.py",
        "false && cd sub; touch x.py",
        "test -d sub && cd sub; git add x.py",
        "cd sub; false && cd ..; touch x.py",
        "false && cd sub\ntouch x.py",
        "true || { true; cd sub; }; touch y.py",
        "false && { true; cd sub; }; touch y.py",
        "false && { cd sub || exit 1; }; touch y.py",
        "false && cd sub && { true; } | cat; touch y.py",
        "cd sub && cd deeper; touch y.py",
        "if false && cd sub; then touch y.py; fi",
        "f() { cd sub; }; f; touch y.py",
        "f(){ cd sub; }; f; touch y.py",
        "f() { cd sub; }; false || f; touch y.py",
        "if true; then f() { cd sub; }; fi; f; touch y.py",
        "while true; do f() { cd sub; }; break; done; f; touch y.py",
        "{ f() { cd sub; }; }; f; touch y.py",
        "case x in x) f() { cd sub; };; esac; f; touch y.py",
        "function f { cd sub; }\nf\ntouch y.py",
        "function my-f() { cd sub; }; my-f; touch y.py",
        "f()\n{ cd sub; }\nf\ntouch y.py",
        "f() { pushd sub; }; f; touch y.py",
        "f() { popd; }; f; touch y.py",
        "eval 'cd sub'; touch y.py",
        "eval cd sub; touch y.py",
        'eval "cd sub"; touch y.py',
        "eval pushd sub; touch y.py",
        "eval 'pushd sub'; touch y.py",
        "eval 'popd'; touch y.py",
        "eval 'cd sub'; git add x.py",
    ],
)
def test_indirect_directory_changes_fail_closed(tmp_path: Path, command: str) -> None:
    data = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_repo_mutation"] is True
    assert data.get("canonical_file_paths", []) == []
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize(
    "command",
    [
        "f() { cd sub; }; touch y.py",
        "f() { echo cd; }; f; touch y.py",
        "eval 'echo cd'; touch y.py",
        "eval 'echo pushd'; touch y.py",
        "eval 'echo popd'; touch y.py",
    ],
)
def test_nonexecuted_directory_words_preserve_scope(tmp_path: Path, command: str) -> None:
    data = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_file_paths"] == ["y.py"]
    assert data.get("canonical_repo_mutation_scope_unknown", False) is False


@pytest.mark.parametrize(
    ("command", "paths"),
    [
        ("test -f x.py || touch x.py", ["x.py"]),
        ("mkdir -p d || exit 1; touch d/x.py", ["d", "d/x.py"]),
        ("rm -f x.py || true; touch x.py", ["x.py"]),
        ("false || true; touch x.py", ["x.py"]),
        ("sleep 1 & touch x.py", ["x.py"]),
        ("make & wait; touch x.py", ["x.py"]),
        ("false || true; git add x.py", ["x.py"]),
        ("sleep 1 & git add x.py", ["x.py"]),
        ("f() { false || true; }; f; touch y.py", ["y.py"]),
        ("if true; then false || true; fi; touch y.py", ["y.py"]),
        ("cd sub; false || true; touch x.py", ["sub/x.py"]),
        ("cd sub; false || touch x.py", ["sub/x.py"]),
        ("cd sub; sleep 1 & touch x.py", ["sub/x.py"]),
        ("if true; then\n  cd sub\n  test -f x.py || touch x.py\nfi", ["sub/x.py"]),
        ("{ cd sub; false || true; touch x.py; }", ["sub/x.py"]),
        ("cd sub && { false || touch x.py; }", ["sub/x.py"]),
        ("cd sub; { true; } || touch x.py", ["sub/x.py"]),
        ("{ cd sub; } | cat || touch x.py", ["x.py"]),
        ("true && cd sub && touch y.py", ["sub/y.py"]),
        ("cd sub && touch y.py; touch z.py", ["sub/y.py", "sub/z.py"]),
        ("test -d sub && cd sub || exit 1; touch y.py", ["sub/y.py"]),
        ("{ true; cd sub; }; touch y.py", ["sub/y.py"]),
        ("false && { cd sub; } | cat; touch y.py", ["y.py"]),
    ],
)
def test_unchanged_and_or_list_preserves_directory_scope(
    tmp_path: Path, command: str, paths: list[str]
) -> None:
    data = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    resolved_paths = [
        str(Path(path).relative_to(tmp_path)) if Path(path).is_absolute() else path
        for path in data["canonical_file_paths"]
    ]
    assert resolved_paths == paths
    assert data["canonical_repo_mutation"] is True
    assert data.get("canonical_repo_mutation_scope_unknown", False) is False


def test_directory_change_after_background_boundary_fails_closed(tmp_path: Path) -> None:
    data = _invocation_metadata(
        "sleep 1 & cd sub; touch x.py", tool_cwd=tmp_path, project_path=tmp_path
    )
    assert data.get("canonical_file_paths", []) == []
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize(
    "command",
    ["cd sub && touch a.py || touch b.py", "{ cd sub; } && touch a.py || touch b.py"],
)
def test_changed_and_or_list_retains_prior_write_and_unknown_scope(
    tmp_path: Path, command: str
) -> None:
    data = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_file_paths"] == ["sub/a.py"]
    assert data["canonical_repo_mutation_scope_unknown"] is True


def test_background_grammar_group_invalidates_later_scope(tmp_path: Path) -> None:
    _signature_fixture(tmp_path / "bin")
    data = _invocation_metadata(
        "{ cd sub; } & ./bin/tool", tool_cwd=tmp_path, project_path=tmp_path
    )
    assert data.get("canonical_script_execution") is True


def test_grammar_group_preserves_enclosing_branch_metadata(tmp_path: Path) -> None:
    grouped = _invocation_metadata(
        "if true; then { cd sub; touch x.py; }; fi; touch y.py",
        tool_cwd=tmp_path,
        project_path=tmp_path,
    )
    bare = _invocation_metadata(
        "if true; then cd sub; touch x.py; fi; touch y.py",
        tool_cwd=tmp_path,
        project_path=tmp_path,
    )
    assert {key: value for key, value in grouped.items() if key.startswith("canonical_")} == {
        key: value for key, value in bare.items() if key.startswith("canonical_")
    }


def test_grammar_group_can_enter_project_from_foreign_cwd(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    grouped = _invocation_metadata(
        f"{{ cd {project}; touch x.py; }}", tool_cwd=tmp_path, project_path=project
    )
    bare = _invocation_metadata(
        f"cd {project}; touch x.py", tool_cwd=tmp_path, project_path=project
    )
    assert {key: value for key, value in grouped.items() if key.startswith("canonical_")} == {
        key: value for key, value in bare.items() if key.startswith("canonical_")
    }


def _invocation_metadata(
    command: str,
    *,
    event_cwd: Path | None = None,
    tool_cwd: Path | None = None,
    project_path: Path | None = None,
) -> dict[str, Any]:
    tool_input: dict[str, Any] = {"command": command}
    if tool_cwd is not None:
        tool_input["cwd"] = str(tool_cwd)
    data: dict[str, Any] = {"tool_name": "Bash", "tool_input": tool_input}
    if project_path is not None:
        data["project_path"] = str(project_path)
    if event_cwd is not None:
        data["cwd"] = str(event_cwd)
    _set_canonical_tool_metadata(data)
    return data


def _signature_fixture(directory: Path) -> None:
    directory.mkdir(parents=True)
    (directory / "tool").write_bytes(b"\xcf\xfa\xed\xfe\x0c\x00\x00\x01")
    # Runs its arguments, a program unknown until it runs.
    (directory / "script").write_bytes(b'#!/bin/sh\n"$@"\n')


@pytest.mark.parametrize(
    ("command", "cwd_source", "gated"),
    [
        ("./bin/tool --version", "event", False),
        ("bin/tool", "tool_input", False),
        ("bash -c './bin/tool'", "event", False),
        ("./bin/tool && bin/tool | ./bin/tool", "event", False),
        ("./bin/tool || true; ./bin/tool", "event", False),
        ("true & ./bin/tool", "event", False),
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


@pytest.mark.parametrize("operation", ["revert", "cherry-pick"])
@pytest.mark.parametrize("flag", ["--quit", "--skip", "--continue"])
@pytest.mark.parametrize("directory_option", ["", "-C {root}"])
def test_git_sequencer_state_writes_have_worktree_scope(
    tmp_path: Path, operation: str, flag: str, directory_option: str
) -> None:
    root = tmp_path / "worktree"
    option = directory_option.format(root=root)
    data = _invocation_metadata(
        f"git {option} {operation} {flag}", tool_cwd=root, project_path=root
    )
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_file_paths"] == [str(root)]
    assert data["canonical_edit_file_paths"] == []
    assert not data.get("canonical_repo_mutation_scope_unknown")


@pytest.mark.parametrize("operation", ["revert", "cherry-pick"])
def test_git_sequencer_dynamic_directory_is_unknown(tmp_path: Path, operation: str) -> None:
    data = _invocation_metadata(f'git -C "$WT" {operation} --quit', tool_cwd=tmp_path)
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize("wrapper", ["{}", 'bash -c "{}"'])
def test_git_sequencer_scope_keeps_mixed_directory_edit(tmp_path: Path, wrapper: str) -> None:
    command = wrapper.format(f"git revert --quit && cp source.py {tmp_path}")
    data = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_file_paths"] == [str(tmp_path), str(tmp_path / "source.py")]
    assert data["canonical_edit_file_paths"] == [str(tmp_path / "source.py")]
    assert not data.get("canonical_repo_mutation_scope_unknown")


def test_git_sequencer_repeated_relative_directory_changes(tmp_path: Path) -> None:
    data = _invocation_metadata(
        "git -C first -C ../second -c core.editor=true revert --continue", tool_cwd=tmp_path
    )
    assert data["canonical_file_paths"] == [str(tmp_path / "second")]
    assert not data.get("canonical_repo_mutation_scope_unknown")


@pytest.mark.parametrize(
    "prefix",
    [
        "GIT_DIR={target}",
        "env GIT_DIR={target}",
        "GIT_WORK_TREE={target}",
        "env GIT_WORK_TREE={target}",
        "git --git-dir={target}",
    ],
)
@pytest.mark.parametrize("operation", ["revert --quit", "add x.py", "restore x.py"])
def test_explicit_git_repository_selector_never_uses_cwd(
    tmp_path: Path, prefix: str, operation: str
) -> None:
    own = tmp_path / "own"
    target = tmp_path / "other"
    selector = prefix.format(target=target)
    command = (
        f"{selector} {operation}" if selector.startswith("git ") else f"{selector} git {operation}"
    )
    data = _invocation_metadata(command, tool_cwd=own, project_path=own)
    assert data["canonical_repo_mutation"] is True
    assert not any(str(own) in path for path in data.get("canonical_file_paths", []))
    assert data.get("canonical_repo_mutation_scope_unknown") or any(
        str(target) in path for path in data.get("canonical_file_paths", [])
    )


@pytest.mark.parametrize("operation", ["add x.py", "checkout -- x.py", "restore x.py"])
def test_git_directory_option_rebases_mutation_paths(tmp_path: Path, operation: str) -> None:
    target = tmp_path / "other"
    data = _invocation_metadata(f"git -C {target} {operation}", tool_cwd=tmp_path)
    assert data["canonical_file_paths"] == [str(target / "x.py")]


@pytest.mark.parametrize(
    "operation",
    [
        "checkout feature",
        "checkout -f feature",
        "checkout --force feature",
        "checkout -b new",
        "switch feature",
        "switch -c new",
        "switch -f feature",
        "switch --discard-changes feature",
    ],
)
@pytest.mark.parametrize("directory_form", ["git_option", "cd"])
@pytest.mark.parametrize("subdirectory", [False, True])
def test_git_branch_checkout_resolves_worktree_scope(
    tmp_path: Path, operation: str, directory_form: str, subdirectory: bool
) -> None:
    target = tmp_path / "other"
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    nested = target / "sub"
    nested.mkdir()
    directory = nested if subdirectory else target
    prefix = f"git -C {directory}" if directory_form == "git_option" else f"cd {directory} && git"
    data = _invocation_metadata(f"{prefix} {operation}", tool_cwd=tmp_path, project_path=tmp_path)

    forced = any(flag in operation.split() for flag in ("-f", "--force", "--discard-changes"))
    assert data["canonical_tool_kind"] == ("write" if forced else "execute")
    assert data["canonical_file_paths"] == [str(target)]
    if forced:
        assert data["canonical_dirty_edit_paths"] == [str(target)]
        assert data["canonical_git_force_checkout_roots"] == [str(target)]
        assert data["canonical_git_checkout_mutation"] is True
    assert data["canonical_edit_file_paths"] == []
    assert not data.get("canonical_write_file_paths")
    assert data.get("canonical_repo_mutation_scope_unknown") is None


@pytest.mark.parametrize(
    "operand",
    ["a.py", "HEAD a.py", ".", "HEAD .", "--ours a.py", "-f a.py", "--force a.py", "-f HEAD a.py"],
)
@pytest.mark.parametrize("directory_form", ["cwd", "git_option", "cd"])
@pytest.mark.parametrize("subdirectory", [False, True])
@pytest.mark.parametrize("deleted", [False, True])
def test_git_checkout_positional_operands_have_checked_scope(
    tmp_path: Path, operand: str, directory_form: str, subdirectory: bool, deleted: bool
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    directory = tmp_path / "sub" if subdirectory else tmp_path
    directory.mkdir(exist_ok=True)
    (directory / "a.py").write_text("value = 1\n")
    if deleted:
        subprocess.run(["git", "-C", str(directory), "add", "a.py"], check=True)
        (directory / "a.py").unlink()
    prefix = {
        "cwd": "git",
        "git_option": f"git -C {directory}",
        "cd": f"cd {directory} && git",
    }[directory_form]
    data = _invocation_metadata(
        f"{prefix} checkout {operand}", tool_cwd=directory, project_path=tmp_path
    )
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_edit_file_paths"] == []
    assert data["canonical_dirty_edit_paths"] == data["canonical_file_paths"]
    if "-f" in operand.split() or "--force" in operand.split():
        assert data["canonical_file_paths"] == [str(tmp_path)]
        assert data["canonical_git_force_checkout_roots"] == [str(tmp_path)]
    else:
        assert (
            str(directory / ("." if "." in operand.split() else "a.py"))
            in data["canonical_file_paths"]
        )


@pytest.mark.parametrize("failure", ["oserror", "timeout", "exit"])
@pytest.mark.parametrize("probe", ["revision", "index"])
def test_git_checkout_unavailable_index_fails_closed(
    tmp_path: Path, failure: str, probe: str
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    with patch("gobby.hooks._normalization_git.subprocess.run") as run:
        failure_result: subprocess.CompletedProcess[bytes] | OSError | subprocess.TimeoutExpired
        if failure == "oserror":
            failure_result = OSError("git unavailable")
        elif failure == "timeout":
            failure_result = subprocess.TimeoutExpired("git", 1)
        else:
            failure_result = subprocess.CompletedProcess(
                ["git"], 128, stdout=b"", stderr=b"invalid index"
            )
        run.side_effect = (
            [subprocess.CompletedProcess(["git"], 1, stdout=b""), failure_result]
            if probe == "index"
            else [failure_result]
        )
        data = _invocation_metadata(
            "git checkout missing.py", tool_cwd=tmp_path, project_path=tmp_path
        )
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation_scope_unknown"] is True
    assert data["canonical_edit_file_paths"] == []


@pytest.mark.parametrize("deleted", [False, True])
@pytest.mark.parametrize("forced", [False, True])
def test_git_checkout_proven_branch_wins_over_file(
    tmp_path: Path, deleted: bool, forced: bool
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    path = tmp_path / "a.py"
    path.write_text("value = 1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "a.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    subprocess.run(["git", "-C", str(tmp_path), "branch", "a.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "checkout", "-q", "a.py"], check=True)
    branch = subprocess.run(
        ["git", "-C", str(tmp_path), "symbolic-ref", "--short", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert branch.stdout.strip() == "a.py"
    if deleted:
        path.unlink()
    data = _invocation_metadata(
        f"git checkout {'-f ' if forced else ''}a.py", tool_cwd=tmp_path, project_path=tmp_path
    )
    assert data["canonical_tool_kind"] == ("write" if forced else "execute")
    assert data["canonical_file_paths"] == [str(tmp_path)]
    assert data["canonical_dirty_edit_paths"] == ([str(tmp_path)] if forced else [])
    assert data["canonical_edit_file_paths"] == []
    if forced:
        assert data["canonical_git_force_checkout_roots"] == [str(tmp_path)]


@pytest.mark.parametrize("selector", ["GIT_INDEX_FILE", "GIT_DIR"])
def test_git_checkout_index_proof_ignores_inherited_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selector: str
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    path = tmp_path / "a.py"
    path.write_text("value = 1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "a.py"], check=True)
    path.unlink()
    monkeypatch.setenv(selector, str(tmp_path / "unrelated"))
    data = _invocation_metadata("git checkout a.py", tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_file_paths"] == [str(path)]
    assert data["canonical_edit_file_paths"] == []


def test_git_checkout_probes_disable_fsmonitor(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    with patch(
        "gobby.hooks._normalization_git.subprocess.run",
        side_effect=[
            subprocess.CompletedProcess(["git"], 1, stdout=b""),
            subprocess.CompletedProcess(["git"], 0, stdout=b"missing.py\0"),
        ],
    ) as run:
        data = _invocation_metadata(
            "git checkout missing.py", tool_cwd=tmp_path, project_path=tmp_path
        )
    assert data["canonical_tool_kind"] == "write"
    assert run.call_count == 2
    for call in run.call_args_list:
        assert "core.fsmonitor=false" in call.args[0]


def test_display_normalization_never_probes_git(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    command = f"git -C {tmp_path} checkout missing.py"
    probes = [
        subprocess.CompletedProcess(["git"], 1, stdout=b""),
        subprocess.CompletedProcess(["git"], 0, stdout=b"missing.py\0"),
    ]
    with patch("gobby.hooks._normalization_git.subprocess.run", side_effect=probes) as run:
        canonical = normalize_tool_fields({"tool_name": "Bash", "tool_input": {"command": command}})
    assert run.call_count == 2
    assert canonical["canonical_tool_kind"] == "write"

    with patch("gobby.hooks._normalization_git.subprocess.run") as run:
        display = normalize_tool_display_fields(
            {"tool_name": "Bash", "tool_input": {"command": command}}
        )
    run.assert_not_called()
    assert display["tool_input"] == {"command": command}
    assert display["command"] == command
    assert "canonical_tool_kind" not in display


@pytest.mark.parametrize(
    "command",
    [
        "git checkout HEAD~0; touch foreign.txt",
        "touch foreign.txt; git checkout HEAD~0",
        'git checkout HEAD~0; for f in foreign.txt; do touch "$f"; done',
        'for f in foreign.txt; do touch "$f"; done; git checkout HEAD~0',
        "bash -c 'git checkout HEAD~0; touch foreign.txt'",
    ],
)
def test_checkout_dirty_edit_scope_preserves_sibling_writes(tmp_path: Path, command: str) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    data = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_repo_mutation"] is True
    if data.get("canonical_repo_mutation_scope_unknown"):
        assert data["canonical_tool_kind"] == "write"
    else:
        paths = {(tmp_path / path).resolve() for path in data["canonical_dirty_edit_paths"]}
        assert tmp_path / "foreign.txt" in paths
        assert tmp_path not in paths


@pytest.mark.parametrize(
    "operand", ["-p", "--patch a.py", "--pathspec-from-file=paths", "*.py", ":(glob)*.py"]
)
def test_git_checkout_opaque_pathspec_fails_closed(tmp_path: Path, operand: str) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    data = _invocation_metadata(f"git checkout {operand}", tool_cwd=tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize(
    "command",
    [
        "git checkout {a,b}.py",
        "git checkout ~+/a.py",
        "git checkout ~/a.py",
        "git checkout @{-1}",
        "git --icase-pathspecs checkout A.py",
        "GIT_ICASE_PATHSPECS=1 git checkout A.py",
        "env GIT_ICASE_PATHSPECS=1 git checkout A.py",
        "git --icase-pathspecs checkout -- A.py",
        "git --icase-pathspecs restore A.py",
        "GIT_ICASE_PATHSPECS=1 git restore A.py",
        "git --icase-pathspecs rm A.py",
        "GIT_ICASE_PATHSPECS=1 git rm A.py",
        "git --icase-pathspecs mv A.py b.py",
        "GIT_ICASE_PATHSPECS=1 git mv A.py b.py",
        "GIT_ICASE_PATHSPECS+=1 git checkout A.py",
        "GIT_ICASE_PATHSPECS+=1 git restore A.py",
        "GIT_ICASE_PATHSPECS+=1 git rm A.py",
        "GIT_ICASE_PATHSPECS+=1 git mv A.py b.py",
        'env "$n=1" git restore A.py',
        "env GIT_ICASE_$x=1 git restore A.py",
        "GIT_INDEX_FILE=/foreign/index git restore A.py",
        "GIT_COMMON_DIR=/foreign/common git restore A.py",
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.worktree GIT_CONFIG_VALUE_0=/foreign git restore A.py",
        "git checkout -- 'a*.py'",
        "git checkout HEAD -- 'a*'",
        "git restore 'a*.py'",
        "git restore '[a].py'",
        "git restore -- 'a?.py'",
        "git rm -f 'a*.py'",
    ],
)
def test_git_checkout_script_and_icase_scope_fails_closed(tmp_path: Path, command: str) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    data = _invocation_metadata(command, tool_cwd=tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data.get("canonical_repo_mutation_scope_unknown") is True


@pytest.mark.parametrize("operation", ["checkout -f feature", "switch --discard-changes feature"])
@pytest.mark.parametrize("directory_form", ["git_option", "cd"])
def test_git_branch_checkout_unproven_root_fails_closed(
    tmp_path: Path, operation: str, directory_form: str
) -> None:
    target = tmp_path / "not-a-checkout"
    target.mkdir()
    prefix = f"git -C {target}" if directory_form == "git_option" else f"cd {target} && git"
    data = _invocation_metadata(f"{prefix} {operation}", tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize("operation", ["checkout -f feature", "switch --discard-changes feature"])
@pytest.mark.parametrize(
    "prefix",
    [
        "git --git-dir x",
        "git --work-tree x",
        "GIT_DIR=x git",
        "export GIT_WORK_TREE=x; git",
        "git -C $X",
        "git -c core.worktree=x",
    ],
)
def test_git_branch_checkout_selectors_stay_unknown(
    tmp_path: Path, prefix: str, operation: str
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    data = _invocation_metadata(f"{prefix} {operation}", tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_repo_mutation_scope_unknown"] is True


def test_git_branch_checkout_retains_each_force_scope_and_only_real_edits(tmp_path: Path) -> None:
    roots = [tmp_path / "first", tmp_path / "second"]
    for root in roots:
        subprocess.run(["git", "init", "-q", str(root)], check=True)
    edited = roots[1] / "edited.py"
    data = _invocation_metadata(
        f"git -C {roots[0]} checkout -f feature && git -C {roots[1]} switch -f feature "
        f"&& touch {edited}",
        tool_cwd=tmp_path,
        project_path=tmp_path,
    )
    assert data["canonical_git_force_checkout_roots"] == [str(root) for root in roots]
    assert data["canonical_file_paths"] == [str(root) for root in roots] + [str(edited)]
    assert data["canonical_edit_file_paths"] == [str(edited)]


@pytest.mark.parametrize("prefix", ["true &&", "git status &&", "git --no-pager", "git -c k=v"])
def test_sequencer_scope_inherits_base_directory(tmp_path: Path, prefix: str) -> None:
    command = (
        f"{prefix} revert --quit" if prefix.startswith("git -") else f"{prefix} git revert --quit"
    )
    data = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_file_paths"] == [str(tmp_path)]
    assert data["canonical_edit_file_paths"] == []
    assert not data.get("canonical_repo_mutation_scope_unknown")


def test_sequencer_redirect_is_an_edit(tmp_path: Path) -> None:
    data = _invocation_metadata("git revert --quit > out.txt", tool_cwd=tmp_path)
    assert [tmp_path / path for path in data["canonical_edit_file_paths"]] == [tmp_path / "out.txt"]


@pytest.mark.parametrize(
    "command",
    [
        "(touch x.py)",
        "( touch x.py )",
        "true && (touch x.py)",
        "(touch x.py; touch y.py)",
        "(touch 'x.py')",
        "(echo ready # ) ignored\n touch x.py)",
        "(cat <<'EOF'\n)\nEOF\ntouch x.py)",
    ],
)
def test_parenthesized_subshell_attributes_writes(tmp_path: Path, command: str) -> None:
    data = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    expected = [str(tmp_path / "x.py")]
    if "y.py" in command:
        expected.append(str(tmp_path / "y.py"))
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert [
        str(tmp_path / path)
        for path in data.get("canonical_edit_file_paths", data["canonical_file_paths"])
    ] == expected


@pytest.mark.parametrize("prefix", ["", "true && ", "true; ", "! "])
@pytest.mark.parametrize("body", ["touch x.py", "echo ready > x.py", "touch x.py; touch y.py"])
def test_adjacent_negated_subshell_matches_spaced_mutation(
    tmp_path: Path, prefix: str, body: str
) -> None:
    adjacent = _invocation_metadata(f"{prefix}!({body})", tool_cwd=tmp_path, project_path=tmp_path)
    spaced = _invocation_metadata(f"{prefix}! ({body})", tool_cwd=tmp_path, project_path=tmp_path)
    assert adjacent["canonical_tool_kind"] == spaced["canonical_tool_kind"] == "write"
    assert adjacent["canonical_repo_mutation"] is spaced["canonical_repo_mutation"] is True
    assert adjacent["canonical_file_paths"] == spaced["canonical_file_paths"]
    assert adjacent["canonical_file_paths"]


@pytest.mark.parametrize(
    "command", ["! true", "!(true)", "echo '!touch x.py'", "echo '!(touch x.py)'", "cat !(x.py)"]
)
def test_negated_subshell_readonly_and_literal_controls(tmp_path: Path, command: str) -> None:
    data = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_tool_kind"] == ("read" if command.startswith("cat ") else "execute")
    assert data.get("canonical_repo_mutation", False) is False


@pytest.mark.parametrize("command", ["cp", "mv"])
@pytest.mark.parametrize("destination", [".", "tests"])
def test_file_transfer_records_exact_directory_destination(
    tmp_path: Path, command: str, destination: str
) -> None:
    (tmp_path / "tests").mkdir()
    data = _invocation_metadata(f"{command} f.py {destination}", tool_cwd=tmp_path)
    target = tmp_path / destination / "f.py"
    expected = [str(target)] if command == "cp" else [str(tmp_path / "f.py"), str(target)]
    assert [str(tmp_path / path) for path in data["canonical_file_paths"]] == list(
        dict.fromkeys(expected)
    )


@pytest.mark.parametrize(
    ("command", "destination"),
    [
        ("cp -R docs/ tests", "tests"),
        ("cp -R docs tests", "tests/docs"),
        ("cp a.py tests/", "tests/a.py"),
        ('cp "$f" tests/', "tests/"),
        ('for f in a.py b.py; do cp "$f" tests/; done', "tests/"),
        ("cp src/*.py tests/", "tests/"),
        ("cp src/a?.py tests/", "tests/"),
        ("cp -R docs/* tests/", "tests/"),
        ("cp src/{a,b}.py tests/", "tests/"),
    ],
)
def test_file_transfer_directory_scope_covers_unknown_source_basename(
    tmp_path: Path, command: str, destination: str
) -> None:
    (tmp_path / "tests").mkdir()
    data = _invocation_metadata(command, tool_cwd=tmp_path)
    assert data["canonical_file_paths"] == [destination]
    assert data["canonical_write_file_paths"] == [destination]
    assert not data.get("canonical_repo_mutation_scope_unknown")


@pytest.mark.parametrize("wrapper", ["{}", "bash -c '{}'"])
@pytest.mark.parametrize(
    "command",
    [
        "cp a b && git apply change.patch",
        "git apply change.patch && cp a b",
        'for file in a.py; do touch "$file"; done; git apply change.patch',
        'git apply change.patch; for file in a.py; do touch "$file"; done',
        'bash -c "git apply change.patch"; for file in a.py; do touch "$file"; done',
    ],
)
def test_resolved_sibling_paths_cannot_mask_pathless_mutation(
    tmp_path: Path, wrapper: str, command: str
) -> None:
    data = _invocation_metadata(wrapper.format(command), tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation_scope_unknown"] is True
    assert data["canonical_file_paths"]


@pytest.mark.parametrize(
    "command",
    [
        "git apply change.patch > /tmp/log",
        "python3 -c \"import os; os.remove(os.environ['F'])\" > log.txt",
    ],
)
def test_redirect_cannot_resolve_a_pathless_write(tmp_path: Path, command: str) -> None:
    data = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


def test_redirect_preserves_resolved_write_scope(tmp_path: Path) -> None:
    data = _invocation_metadata("cp a b > /tmp/log", tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_file_paths"] == ["b", "/tmp/log"]
    assert data.get("canonical_repo_mutation_scope_unknown") is None


@pytest.mark.parametrize(
    "command", ["git apply change.patch", "cp a b", "git apply change.patch && cp a b"]
)
@pytest.mark.parametrize(
    "wrapper", ["({})", "echo $({})", 'echo "$({})"', "echo `{}`", 'echo "$(echo $({}))"']
)
def test_nested_write_matches_unwrapped_scope(tmp_path: Path, command: str, wrapper: str) -> None:
    plain = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    nested = _invocation_metadata(wrapper.format(command), tool_cwd=tmp_path, project_path=tmp_path)
    assert nested["canonical_repo_mutation"] is True
    assert nested["canonical_tool_kind"] == "write"
    assert nested.get("canonical_file_paths", []) == plain.get("canonical_file_paths", [])
    assert nested.get("canonical_repo_mutation_scope_unknown") == plain.get(
        "canonical_repo_mutation_scope_unknown"
    )


@pytest.mark.parametrize(
    "command",
    [
        "echo '$(git apply change.patch)'",
        "echo $'$(git apply change.patch)'",
        'echo "\\$(git apply change.patch)"',
        "echo '`cp a b`'",
    ],
)
def test_literal_substitution_text_does_not_mutate(tmp_path: Path, command: str) -> None:
    data = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    assert not data.get("canonical_repo_mutation")
    assert data.get("canonical_repo_mutation_scope_unknown") is None


@pytest.mark.parametrize(
    "body",
    [
        "$(git apply change.patch)",
        "`git apply change.patch`",
        "'$(git apply change.patch)'",
        '"$(git apply change.patch)"',
    ],
)
def test_unquoted_heredoc_substitution_mutates(tmp_path: Path, body: str) -> None:
    data = _invocation_metadata(f"cat <<EOF\n{body}\nEOF", tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize(
    "command",
    [
        "cat <<'EOF'\n$(git apply change.patch)\nEOF",
        'cat <<"EOF"\n`git apply change.patch`\nEOF',
        "cat <<EOF\n\\$(git apply change.patch)\nEOF",
        "cat <<EOF\n\\`git apply change.patch\\`\nEOF",
    ],
)
def test_literal_heredoc_substitution_does_not_mutate(tmp_path: Path, command: str) -> None:
    data = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    assert not data.get("canonical_repo_mutation")
    assert data.get("canonical_repo_mutation_scope_unknown") is None


@pytest.mark.parametrize("prefix", ["", "cd sub && "])
def test_heredoc_substitution_preserves_resolved_scope(tmp_path: Path, prefix: str) -> None:
    plain = _invocation_metadata(f"{prefix}cp a b", tool_cwd=tmp_path, project_path=tmp_path)
    nested = _invocation_metadata(
        f"{prefix}cat <<EOF\n$(cp a b)\nEOF", tool_cwd=tmp_path, project_path=tmp_path
    )
    assert nested["canonical_repo_mutation"] is True
    assert nested["canonical_file_paths"] == plain["canonical_file_paths"]
    assert nested.get("canonical_repo_mutation_scope_unknown") is None


@pytest.mark.parametrize(
    "command",
    [
        "sed -i s/a/b/ f.txt && cat <<EOF\nthe ` char\nEOF",
        "git apply p.patch && cat <<EOF\nthe ` char\nEOF",
        "git apply p.patch && cat <<EOF\nprice $(5\nEOF",
        "cat <<EOF\n$(git apply p.patch) then `\nEOF",
        "cat <<EOF\n$(git apply p.patch)",
    ],
)
def test_incomplete_heredoc_substitution_cannot_mask_mutations(
    tmp_path: Path, command: str
) -> None:
    data = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


def test_malformed_heredoc_keeps_prior_complete_substitution(tmp_path: Path) -> None:
    data = _invocation_metadata(
        "cat <<EOF\n$(cp a b) then `\nEOF", tool_cwd=tmp_path, project_path=tmp_path
    )
    assert data["canonical_repo_mutation"] is True
    assert "b" in data["canonical_file_paths"]
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize(
    "command",
    [
        'git add file.py && git commit -m "fix"',
        'git commit -am "fix" && git push',
        'uv run ruff format file.py && git add -A && git commit -m "fix"',
        "git switch -c fix",
        "git stash push",
        "git rebase --continue",
        "git merge --continue",
    ],
)
def test_sequencer_commands_keep_resolved_scope(tmp_path: Path, command: str) -> None:
    data = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_tool_kind"] in {"execute", "write"}
    assert data.get("canonical_repo_mutation_scope_unknown") is None


@pytest.mark.parametrize(
    "command",
    [
        "echo $'\\'$(' ; git apply x.patch ; echo \")\"",
        "echo $'\\''$(git apply x.patch)",
    ],
)
def test_ansi_c_quote_cannot_mask_a_live_mutation(tmp_path: Path, command: str) -> None:
    data = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


def test_ansi_c_quote_substitution_text_stays_read_only(tmp_path: Path) -> None:
    data = _invocation_metadata("cat $'a$(b)'", tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_tool_kind"] == "read"
    assert not data.get("canonical_repo_mutation")
    assert data.get("canonical_repo_mutation_scope_unknown") is None


@pytest.mark.parametrize("parser", ["scan_shell_command", "command_substitution_bodies"])
def test_shell_parser_failure_fails_closed(tmp_path: Path, parser: str) -> None:
    with patch(f"gobby.hooks._normalization_canonical.{parser}", side_effect=ValueError("invalid")):
        data = _classify_shell_command(
            "echo safe", directory=ShellDirectoryState(str(tmp_path)), depth=0, base_cwd=tmp_path
        )
    assert data == {
        "canonical_tool_kind": "write",
        "canonical_tool_confidence": "low",
        "canonical_repo_mutation": True,
        "canonical_repo_mutation_scope_unknown": True,
    }


def test_nested_ansi_c_quote_cannot_hide_mutation(tmp_path: Path) -> None:
    command = "echo \"$(echo $'\\')' ; git apply x.patch ; echo ')')\""
    data = _invocation_metadata(command, tool_cwd=tmp_path, project_path=tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize("wrapper", ["echo $({})", 'echo "$({})"'])
def test_nested_ansi_c_literal_matches_double_quoted_twin(tmp_path: Path, wrapper: str) -> None:
    literal = _invocation_metadata(
        wrapper.format("echo $'it\\'s'"), tool_cwd=tmp_path, project_path=tmp_path
    )
    twin = _invocation_metadata(
        wrapper.format('echo "it\'s"'), tool_cwd=tmp_path, project_path=tmp_path
    )
    assert literal["canonical_tool_kind"] == twin["canonical_tool_kind"] == "execute"
    assert not literal.get("canonical_repo_mutation")
    assert literal.get("canonical_repo_mutation_scope_unknown") is None


@pytest.mark.parametrize(
    "text",
    [
        "echo $'\\')' ; git apply x.patch ; echo ')')",
        "echo $'it\\'s')",
        'echo "$\'" )',
    ],
)
def test_substitution_end_preserves_ansi_c_quotes(text: str) -> None:
    assert _substitution_end(text, 0, False, 1) == len(text) - 1


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
        "f() { cd sub; }; false || f; ./bin/tool",
        "true & cd sub; ./bin/tool",
        "./bin/tool () cd sub; ./bin/tool; ./bin/tool",
        "{cd,./sub}; ./bin/tool",
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
        # Redirecting a navigation read's output must not drop the opaque program.
        "bash -c 'cat /project/source.py; /project/missing.sh' > /tmp/scratchpad/out.txt",
    ],
)
def test_unresolved_shell_program_requires_a_claim(tmp_path: Path, command: str) -> None:
    data = _shell_write_metadata(command, tmp_path)

    assert data.get("canonical_script_execution") is True


def test_malformed_nested_shell_program_fails_closed(tmp_path: Path) -> None:
    data = _shell_write_metadata("bash -c 'touch /project/owned.py\nprintf \"'", tmp_path)

    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


def _script(path: Path, body: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body.encode() if isinstance(body, str) else body)
    return path.resolve()


@pytest.mark.parametrize("invocation", ["zsh {script}", "bash -e {script} --once", "{script}"])
def test_scratch_only_script_body_is_not_a_repo_write(tmp_path: Path, invocation: str) -> None:
    scratch = tmp_path / "scratchpad"
    script = _script(
        scratch / "idle.sh",
        f"#!/bin/zsh\n# poll\ndate > {scratch}/state.txt\nsed -i '' 's/a/b/' {scratch}/state.txt\n",
    )
    data = _shell_write_metadata(invocation.format(script=script), tmp_path / "repo")

    assert data["canonical_file_paths"] == [str(scratch / "state.txt")]
    assert data["canonical_repo_mutation"] is False
    assert data["canonical_script_bodies"] == [str(script)]
    assert not data.get("canonical_script_execution")


def test_script_body_repo_write_is_a_repo_mutation(tmp_path: Path) -> None:
    target = tmp_path / "owned.py"
    script = _script(tmp_path / "scratchpad" / "edit.sh", f"#!/bin/sh\ntouch {target}\n")
    nested = _script(tmp_path / "scratchpad" / "outer.sh", f"bash {script}\n")
    data = _shell_write_metadata(f"sh {nested}", tmp_path)

    assert data["canonical_repo_mutation"] is True
    assert data["canonical_write_file_paths"] == [str(target)]
    assert data["canonical_script_bodies"] == sorted([str(script), str(nested)])
    assert not data.get("canonical_script_execution")


def test_read_only_script_with_arguments_is_not_gated(tmp_path: Path) -> None:
    script = _script(tmp_path / "scratchpad" / "q.sh", '#!/bin/zsh\npsql "$DB_URL" -c "$1"\n')
    data = _shell_write_metadata(f'{script} "select count(*) from tasks"', tmp_path)

    assert data["canonical_tool_kind"] == "execute"
    assert not data.get("canonical_repo_mutation")
    assert not data.get("canonical_script_execution")


def test_virtualenv_entry_point_runs_like_its_interpreter(tmp_path: Path) -> None:
    pytest_entry = _script(
        tmp_path / "repo" / ".venv" / "bin" / "pytest",
        f"#!{tmp_path}/repo/.venv/bin/python\n# -*- coding: utf-8 -*-\nimport sys\n"
        "from pytest import console_main\n",
    )
    export = tmp_path / "scratchpad" / "c23451"
    # A reviewer's export retest, blocked after cutover 1.
    data = _shell_write_metadata(
        "DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test "
        f"GOBBY_TEST_PROTECT=1 PYTHONPATH={export}/src {pytest_entry} "
        f"-c {export}/pyproject.toml --rootdir={export} {export}/tests/agents/test_x.py "
        f"-q -p no:cacheprovider --basetemp={tmp_path}/scratchpad/pt",
        tmp_path / "repo",
    )

    assert not data.get("canonical_script_execution")
    assert not data.get("canonical_repo_mutation")


@pytest.mark.parametrize(
    "command",
    [
        "printf 'touch {project}/owned.py' > {script} && {script}",
        "zsh {script} | tee {scratch}/out.txt",
        "bash -c 'echo > {script}; zsh {script}'",
        "bash -c 'zsh {script}' && cp {scratch}/next.sh {script}",
        "bash {script} >> {script}",
        "bash {script} 2>> {script}",
        "sh -c 'zsh {script}' >> {script}",
    ],
)
def test_script_run_beside_another_write_requires_a_claim(tmp_path: Path, command: str) -> None:
    scratch = tmp_path / "scratchpad"
    script = _script(scratch / "q.sh", "#!/bin/zsh\necho ready\n")
    data = _shell_write_metadata(
        command.format(project=tmp_path, scratch=scratch, script=script), tmp_path
    )

    assert data.get("canonical_script_execution") is True


@pytest.mark.parametrize(
    "body",
    [
        "#!/bin/sh\necho 'touch {project}/owned.py' >> {script}\n",
        "#!/bin/sh\ncp {scratch}/replacement/s.sh {scratch}\n",
        # A program unknown until it runs.
        '#!/bin/sh\n"$@"\n',
        "#!/bin/sh\n" + "echo ready\n" * 6000,
        b"#!/bin/sh\necho \xff\n",
    ],
)
def test_unclassifiable_or_self_rewriting_script_requires_a_claim(
    tmp_path: Path, body: str | bytes
) -> None:
    scratch = tmp_path / "scratchpad"
    script = scratch / "s.sh"
    if isinstance(body, str):
        body = body.format(project=tmp_path, scratch=scratch, script=script)
    data = _shell_write_metadata(f"bash {_script(script, body)}", tmp_path)

    assert data.get("canonical_script_execution") is True


def test_self_calling_script_spends_one_bounded_read_budget(tmp_path: Path) -> None:
    fan = tmp_path / "scratchpad" / "fan.sh"
    _script(fan, "#!/bin/bash\n" + f"{fan}\n" * 4)
    reader = _script(tmp_path / "scratchpad" / "q.sh", "#!/bin/zsh\necho ready\n")
    with patch(
        "gobby.hooks._normalization_canonical.read_script_body", wraps=read_script_body
    ) as reads:
        fanned = _shell_write_metadata(str(fan), tmp_path)
        assert reads.call_count == SCRIPT_READ_BUDGET
        # The next command gets a fresh budget.
        later = _shell_write_metadata(str(reader), tmp_path)

    assert fanned.get("canonical_script_execution") is True
    assert not later.get("canonical_script_execution")


def test_variable_write_in_script_body_has_unknown_scope(tmp_path: Path) -> None:
    script = _script(tmp_path / "scratchpad" / "mk.sh", '#!/bin/sh\nmkdir -p "$1"\n')
    data = _shell_write_metadata(f"bash {script} {tmp_path}/generated", tmp_path)

    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True
    assert not data.get("canonical_script_execution")


@pytest.mark.parametrize(
    "command",
    [
        "source {script}",
        ". {script}",
        # Relative script paths fail closed once the directory may have changed.
        "cd {scratch} && zsh q.sh",
    ],
)
def test_sourced_or_unresolved_script_stays_opaque(tmp_path: Path, command: str) -> None:
    scratch = tmp_path / "scratchpad"
    script = _script(scratch / "q.sh", "#!/bin/zsh\necho ready\n")
    data = _shell_write_metadata(command.format(scratch=scratch, script=script), tmp_path)

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

    assert data["canonical_file_paths"] == [f"{project}/dest/src"]
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


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("case x in x) cd sub;& d) touch e.py;; esac", "sub/e.py"),
        ("case x in x) cd sub;&\n d) touch e.py;; esac", "sub/e.py"),
        ("case x in x) cd sub;& # fallthrough\n d) touch e.py;; esac", "sub/e.py"),
        ("case x in x) cd sub;;& d) touch e.py;; esac", "sub/e.py"),
        ("case x in x) cd sub  # go\n;& d) touch e.py;; esac", "sub/e.py"),
        ("case x in x) cd sub  # go\n;;& d) touch e.py;; esac", "sub/e.py"),
        ("case x in x) cd sub & ;& d) touch e.py;; esac", "e.py"),
        ("case x in x) cd sub \\\n& ;& d) touch e.py;; esac", "e.py"),
        ("case x in x) cd sub \\\n& ;;& d) touch e.py;; esac", "e.py"),
        ("case x in x) cd sub & # bg\n;& d) touch e.py;; esac", "e.py"),
        ("case x in x) cd sub  # a & b\n;& d) touch e.py;; esac", "sub/e.py"),
        ("case x in x) cd sub  # x ;&\ntouch e.py;; esac", "sub/e.py"),
        ("case x in x) cd sub  # x\ntouch e.py;; esac", "sub/e.py"),
        ("case x in x) cd sub ;\\\n& d) touch e.py;; esac", "sub/e.py"),
        ("cd sub & touch x.py", "x.py"),
    ],
)
def test_case_fallthrough_cd_is_not_a_background_job(
    tmp_path: Path, command: str, expected: str
) -> None:
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "cwd": str(tmp_path),
        "project_path": str(tmp_path),
        "tool_input": {"command": command},
    }

    normalize_tool_fields(data)

    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_file_paths"] == [expected]
    assert data["canonical_repo_mutation"] is True
