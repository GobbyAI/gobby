"""Unresolved-scope guard inputs: redirect read operands, bindings, and clobber redirects."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gobby.hooks._normalization_shell import scan_shell_command, shell_token_values
from gobby.hooks._normalization_tools import _set_canonical_tool_metadata

pytestmark = pytest.mark.unit


def _metadata(command: str, project: Path) -> dict[str, Any]:
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": command, "cwd": str(project)},
        "project_path": str(project),
    }
    _set_canonical_tool_metadata(data)
    return data


def _assert_known_write(data: dict[str, Any], target: str, *, in_project: bool) -> None:
    assert data["canonical_tool_kind"] == "write"
    assert target in data["canonical_file_paths"]
    assert data["canonical_repo_mutation"] is in_project
    assert "canonical_repo_mutation_scope_unknown" not in data


@pytest.mark.parametrize(
    ("command", "target"),
    [
        pytest.param("rg foo src > out.txt", "out.txt", id="rg"),
        pytest.param("head -50 file > out", "out", id="head"),
        pytest.param("tail -n 5 a > b.txt", "b.txt", id="tail"),
        pytest.param("rg foo $SRC > out.txt", "out.txt", id="rg-unresolved-operand"),
        pytest.param('head -50 "$F" > out', "out", id="head-unresolved-operand"),
        pytest.param('tail -n 5 "${LOG}" >> b.txt', "b.txt", id="tail-unresolved-operand"),
    ],
)
def test_read_command_redirect_scope_is_its_target(
    tmp_path: Path, command: str, target: str
) -> None:
    _assert_known_write(_metadata(command, tmp_path), target, in_project=True)


@pytest.mark.parametrize(
    "template",
    [
        'rg foo src > "{scratch}/out dir/x.txt"',
        "cat src/a.py > {scratch}/x.txt",
        "head -50 $F > {scratch}/x.txt",
    ],
)
def test_read_operands_do_not_attribute_an_outside_redirect_to_the_project(
    tmp_path: Path, template: str
) -> None:
    project, scratch = tmp_path / "project", tmp_path / "scratch"
    command = template.format(scratch=scratch)
    target = f"{scratch}/out dir/x.txt" if "out dir" in command else f"{scratch}/x.txt"

    data = _metadata(command, project)

    _assert_known_write(data, target, in_project=False)
    assert data["canonical_file_paths"][-1] == target
    assert data["canonical_edit_file_paths"] == [target]
    assert "_canonical_read_operand_paths" not in data


@pytest.mark.parametrize(
    "template",
    [
        "cat src/a.py > {scratch}/x.txt; echo hi > src/a.py",
        "echo hi > src/a.py; cat src/a.py > {scratch}/x.txt",
        "bash -c 'cat src/a.py > {scratch}/x.txt'; echo hi > src/a.py",
        "cat src/a.py > src/a.py",
    ],
)
def test_read_operand_written_elsewhere_stays_in_write_scope(tmp_path: Path, template: str) -> None:
    project = tmp_path / "project"

    data = _metadata(template.format(scratch=tmp_path / "scratch"), project)

    assert "src/a.py" in data["canonical_file_paths"]
    assert data["canonical_repo_mutation"] is True
    assert "canonical_repo_mutation_scope_unknown" not in data


def test_nested_shell_read_operand_does_not_attribute_outside_redirect(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"

    data = _metadata(f"bash -c 'cat src/a.py > {scratch}/x.txt'", tmp_path / "project")

    _assert_known_write(data, f"{scratch}/x.txt", in_project=False)


@pytest.mark.parametrize(
    ("command", "paths"),
    [
        ("sed -i s/a/b/ src/a.py > log.txt", ["src/a.py", "log.txt"]),
        ("cp src/a.py src/b.py > log.txt", ["src/b.py", "log.txt"]),
    ],
)
def test_mutating_base_keeps_its_targets_beside_the_redirect(
    tmp_path: Path, command: str, paths: list[str]
) -> None:
    data = _metadata(command, tmp_path)

    assert data["canonical_file_paths"] == paths
    assert data["canonical_repo_mutation"] is True
    assert "canonical_repo_mutation_scope_unknown" not in data


def test_unresolved_redirect_target_stays_unknown(tmp_path: Path) -> None:
    data = _metadata('rg foo src > "$OUT"', tmp_path)

    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize(
    "template",
    [
        pytest.param('export D={scratch}; echo x > "$D/f"', id="export"),
        pytest.param('export D={scratch} E=/x; echo x > "$D/f"', id="export-many"),
        pytest.param('D={scratch}; F="$D/f"; echo x > "$F"', id="chained-quoted"),
        pytest.param('D={scratch}; F=$D/f; echo x > "$F"', id="chained-unquoted"),
        pytest.param('D={scratch}; F=${{D}}/f; echo x > "$F"', id="chained-braced"),
        pytest.param('D={scratch} F="$D/f"; echo x > "$F"', id="same-segment"),
        pytest.param('export D={scratch}; export F="$D/f"; echo x > "$F"', id="export-chain"),
        pytest.param('set -e; D={scratch}; echo x > "$D/f"', id="set-e"),
        pytest.param('set -euo pipefail; D={scratch}; echo x > "$D/f"', id="set-euo"),
        pytest.param('set -euxo pipefail\nD={scratch}\necho x > "$D/f"', id="set-newlines"),
        pytest.param('D={scratch}; set -e; echo x > "$D/f"', id="set-after-binding"),
    ],
)
def test_bound_variable_resolves_scratch_write(tmp_path: Path, template: str) -> None:
    project, scratch = tmp_path / "project", tmp_path / "scratch"

    data = _metadata(template.format(scratch=scratch), project)

    assert data["canonical_file_paths"][-1] == f"{scratch}/f"
    assert data["canonical_repo_mutation"] is False
    assert "canonical_repo_mutation_scope_unknown" not in data


@pytest.mark.parametrize(
    "template",
    [
        'export OUT={project}/src/a.py; echo x > "$OUT"',
        'set -e; D={project}/src; F="$D/a.py"; echo x > "$F"',
    ],
)
def test_bound_variable_inside_project_stays_attributed(tmp_path: Path, template: str) -> None:
    data = _metadata(template.format(project=tmp_path), tmp_path)

    assert data["canonical_file_paths"] == [f"{tmp_path}/src/a.py"]
    assert data["canonical_repo_mutation"] is True
    assert "canonical_repo_mutation_scope_unknown" not in data


def test_bound_variable_outside_scope_is_judged_like_the_literal_path(tmp_path: Path) -> None:
    keys = (
        "canonical_tool_kind",
        "canonical_file_paths",
        "canonical_repo_mutation",
        "canonical_repo_mutation_scope_unknown",
    )
    literal = _metadata("echo x > /etc/f", tmp_path)

    for command in ('D=/etc; echo x > "$D/f"', 'export D=/etc; F="$D/f"; echo x > "$F"'):
        bound = _metadata(command, tmp_path)
        assert {key: bound.get(key) for key in keys} == {key: literal.get(key) for key in keys}
        assert bound["canonical_file_paths"] == ["/etc/f"]


@pytest.mark.parametrize(
    "template",
    [
        pytest.param('D=rel; F="$D/f"; echo x > "$F"', id="relative-base"),
        pytest.param('F="$UNBOUND/f"; echo x > "$F"', id="unbound-base"),
        pytest.param('D={scratch}; F="$D/$G"; echo x > "$F"', id="second-expansion"),
        pytest.param('D={scratch}; F=~/f; echo x > "$F"', id="tilde"),
        pytest.param('D={scratch}; F="$D"/*; echo x > "$F"', id="glob-suffix"),
        pytest.param("export D={scratch}; echo x > $D/f", id="unquoted-target"),
        pytest.param('export -n D={scratch}; echo x > "$D/f"', id="export-flag"),
        pytest.param('export D={scratch} X; echo x > "$D/f"', id="export-non-assignment"),
        pytest.param('export D={scratch} | true; echo x > "$D/f"', id="export-pipeline"),
        pytest.param('set a b; D={scratch}; echo x > "$D/f"', id="set-positional"),
        pytest.param('set -- a; D={scratch}; echo x > "$D/f"', id="set-double-dash"),
        pytest.param('set -o vi; D={scratch}; echo x > "$D/f"', id="set-unlisted-option"),
        pytest.param('D={scratch}; mystery; F="$D/f"; echo x > "$F"', id="after-command"),
    ],
)
def test_unproven_binding_keeps_write_scope_unknown(tmp_path: Path, template: str) -> None:
    data = _metadata(template.format(scratch=tmp_path / "scratch"), tmp_path / "project")

    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


CLOBBER_OPERATORS = (">|", ">>|", "&>|", "&>>|", ">&|", ">>&", ">>&|", "2>|", "2>>|")


@pytest.mark.parametrize("operator", CLOBBER_OPERATORS)
def test_clobber_redirect_scans_as_one_output_operator(operator: str) -> None:
    """``>|`` overrides noclobber in Bash and zsh; the rest are zsh-only (Bash rejects them)."""
    scan = scan_shell_command(f"echo hi {operator} /etc/x")

    assert shell_token_values(scan.tokens) == ["echo", "hi", operator, "/etc/x"]


@pytest.mark.parametrize("operator", CLOBBER_OPERATORS)
def test_clobber_redirect_is_a_write_like_plain_redirect(tmp_path: Path, operator: str) -> None:
    inside = _metadata(f"echo hi {operator} {tmp_path}/src/a.py", tmp_path)
    outside = _metadata(f"echo hi {operator} /etc/x", tmp_path)
    unresolved = _metadata(f'echo hi {operator} "$OUT"', tmp_path)

    _assert_known_write(inside, f"{tmp_path}/src/a.py", in_project=True)
    _assert_known_write(outside, "/etc/x", in_project=False)
    assert unresolved["canonical_tool_kind"] == "write"
    assert unresolved["canonical_repo_mutation_scope_unknown"] is True
