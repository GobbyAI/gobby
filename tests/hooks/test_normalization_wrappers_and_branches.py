"""Regression coverage for wrapper and parent-shell directory attribution."""

from pathlib import Path
from typing import Any

import pytest

from gobby.hooks._normalization_canonical import _set_canonical_tool_metadata

pytestmark = pytest.mark.unit


def _metadata(command: str, root: Path) -> dict[str, Any]:
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": command, "cwd": str(root)},
        "project_path": str(root),
    }
    _set_canonical_tool_metadata(data)
    return data


@pytest.mark.parametrize(
    "command",
    [
        "for d in a b; do ./w.sh; cd sub; done",
        "while true; do ./w.sh; cd sub; done",
        "until false; do ./w.sh; cd sub; done",
        "while ./w.sh; do cd sub; done",
        "for d in a; do for e in b; do ./w.sh; done; cd sub; done",
    ],
)
def test_unstable_loop_body_cannot_prove_relative_script(tmp_path: Path, command: str) -> None:
    (tmp_path / "w.sh").write_text("cat README\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub/w.sh").write_text("touch evil.py\n")
    data = _metadata(command, tmp_path)
    assert data["canonical_script_execution"] is True


@pytest.mark.parametrize(
    "command",
    ["for d in a b; do ./w.sh; done", "if true; then for d in a b; do ./w.sh; done; fi"],
)
def test_stable_loop_body_keeps_relative_script_proof(tmp_path: Path, command: str) -> None:
    (tmp_path / "w.sh").write_text("cat README\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub/w.sh").write_text("touch evil.py\n")
    data = _metadata(command, tmp_path)
    assert data["canonical_tool_kind"] == "read"
    assert not data.get("canonical_script_execution")


@pytest.mark.parametrize("command", ["git -C /tmp/x grep -n P", 'cd "$X"; grep -rn P'])
def test_implicit_search_root_reports_foreign_or_unknown_scope(
    tmp_path: Path, command: str
) -> None:
    data = _metadata(command, tmp_path)
    assert data["canonical_tool_kind"] == "search"
    assert data["canonical_code_navigation_repo_scope"] is False


@pytest.mark.parametrize(
    "command", ["git -C sub grep -n P", "git grep -n P", "grep -rn P", "cd sub; grep -rn P"]
)
def test_resolved_implicit_search_root_keeps_repo_scope(tmp_path: Path, command: str) -> None:
    (tmp_path / "sub").mkdir()
    data = _metadata(command, tmp_path)
    assert data["canonical_tool_kind"] == "search"
    assert data["canonical_code_navigation_repo_scope"] is True


@pytest.mark.parametrize(
    "command",
    [
        "if true; then pushd sub; fi; ./w.sh",
        "if true; then false || cd sub; fi; ./w.sh",
        "case x in x) pushd sub;; esac; ./w.sh",
        "case x in x) false || cd sub;; esac; ./w.sh",
        "if cd sub && false; then :; else ./w.sh; fi",
        "if cd sub && false; then :; elif ./w.sh; then :; fi",
    ],
)
def test_branch_navigation_cannot_restore_relative_script_proof(
    tmp_path: Path, command: str
) -> None:
    (tmp_path / "w.sh").write_text("cat README\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub/w.sh").write_text("touch evil.py\n")
    data = _metadata(command, tmp_path)
    assert data["canonical_script_execution"] is True


@pytest.mark.parametrize(
    "command",
    [
        "for d in a; do cd sub; done; touch z.py",
        "while false; do cd sub; done; touch z.py",
        "until true; do cd sub; done; touch z.py",
        "while cd sub && false; do true; done; touch z.py",
        "for d in a; do if true; then cd sub; fi; done; touch z.py",
        "if true; then for d in a; do cd sub; done; fi; touch z.py",
    ],
)
def test_loop_directory_join_does_not_assume_an_iteration(tmp_path: Path, command: str) -> None:
    data = _metadata(command, tmp_path)
    assert data["canonical_repo_mutation_scope_unknown"] is True
    assert not data.get("canonical_file_paths")


@pytest.mark.parametrize(
    "command",
    [
        "for d in a b; do touch x.py; cd sub; done",
        "while true; do touch x.py; cd sub; done",
        "until false; do touch x.py; cd sub; done",
        "for d in a b; do cd sub; touch x.py; done",
        "while cd sub; do touch x.py; done",
        "while touch x.py; do cd sub; done",
        "for d in a b; do touch x.py; pushd sub; done",
        "for d in a b; do if true; then touch x.py; fi; cd sub; done",
        "for d in a; do for e in b; do touch x.py; done; cd sub; done",
        "for d in a b; do git add x.py; cd sub; done",
        'for f in a.py b.py; do sed -i s/x/y/ "$f"; cd sub; done',
        "for d in a b\ndo\ntouch x.py\ncd sub\ndone",
        "for d in a b; do echo $d; done | while read d; do touch x.py; cd sub; done",
        "for d in a b; do touch x.py; cd ROOT/sub; done",
    ],
)
def test_directory_changing_loop_fails_closed(tmp_path: Path, command: str) -> None:
    data = _metadata(command.replace("ROOT", str(tmp_path)), tmp_path)
    assert data["canonical_repo_mutation_scope_unknown"] is True
    assert not any(path.endswith(".py") for path in data.get("canonical_file_paths", []))


@pytest.mark.parametrize(
    "command",
    [
        "pushd sub; touch z.py",
        "popd; touch z.py",
        'cd "$X"; cd ..; touch z.py',
        'cd "$X"; cd sub; cd ../..; touch z.py',
        'cd "$X"; cd ../sub; touch z.py',
        'cd "$X"; touch ../z.py',
        "for d in a; do cd sub; done; cd ..; touch z.py",
    ],
)
def test_unresolved_directory_navigation_fails_closed(tmp_path: Path, command: str) -> None:
    data = _metadata(command, tmp_path)
    assert data["canonical_repo_mutation_scope_unknown"] is True
    assert not any(path.endswith(".py") for path in data.get("canonical_file_paths", []))


@pytest.mark.parametrize(
    "command",
    [
        'git -C "$X" grep -n P',
        "git --git-dir=/tmp/x grep -n P",
        "git --work-tree /tmp/x grep -n P -- src/",
        'cd "$X"; git grep -n P',
    ],
)
def test_unproven_git_selector_grep_reports_unknown_scope(tmp_path: Path, command: str) -> None:
    data = _metadata(command, tmp_path)
    assert data["canonical_tool_kind"] == "search"
    assert data["canonical_code_navigation_repo_scope"] is False


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("for d in a b; do touch x.py; done", ["x.py"]),
        ("cd sub; for d in a b; do touch x.py; done", ["sub/x.py"]),
        ("for d in a b; do (cd sub; touch y.py); touch x.py; done", ["sub/y.py", "x.py"]),
        ("for d in a b; do touch x.py; (cd sub); done", ["x.py"]),
        ("for d in a b; do bash -c 'touch x.py; cd sub'; done", ["x.py"]),
        ("for d in a b; do cd ROOT/sub; touch x.py; done", ["ROOT/sub/x.py"]),
        ("for f in $(touch h.py) a; do cd sub; done", ["h.py"]),
        ("cd sub; cd ..; touch z.py", ["z.py"]),
    ],
)
def test_loop_directory_controls_keep_exact_paths(
    tmp_path: Path, command: str, expected: list[str]
) -> None:
    data = _metadata(command.replace("ROOT", str(tmp_path)), tmp_path)
    assert data["canonical_file_paths"] == [
        path.replace("ROOT", str(tmp_path)) for path in expected
    ]
    assert not data.get("canonical_repo_mutation_scope_unknown")


@pytest.mark.parametrize(
    "command",
    [
        "if cd sub && false; then :; else touch u.py; fi",
        "if cd sub && false; then true; elif touch g.py; then true; fi",
    ],
)
def test_condition_navigation_does_not_attribute_else_to_entry_cwd(
    tmp_path: Path, command: str
) -> None:
    data = _metadata(command, tmp_path)
    assert data["canonical_repo_mutation_scope_unknown"] is True
    assert not data.get("canonical_file_paths")


@pytest.mark.parametrize(
    "loop",
    [
        "for d in a; do true",
        "while false; do true",
        "until true; do true",
        "for d in a; do echo $d",
        "for d in a; do :",
        "while false; do echo $d",
        "while false; do :",
        "until true; do echo $d",
        "until true; do :",
    ],
)
def test_directory_preserving_loop_keeps_following_write_scope(tmp_path: Path, loop: str) -> None:
    data = _metadata(f"{loop}; done; touch z.py", tmp_path)
    assert data["canonical_file_paths"] == ["z.py"]
    assert not data.get("canonical_repo_mutation_scope_unknown")


@pytest.mark.parametrize("assignment", ["FOO+=x", "a.b=1", "-- FOO+=x", "-- A=1"])
def test_env_accepts_assignment_names_outside_shell_identifier_grammar(
    tmp_path: Path, assignment: str
) -> None:
    data = _metadata(f"env {assignment} touch x.py", tmp_path)
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_file_paths"] == ["x.py"]


@pytest.mark.parametrize(
    "prefix",
    [
        "env -v -C other",
        "env --debug --chdir=other",
        "nohup env -v -C other",
        "timeout -s TERM -k 2s 5 env -C other",
        "nice -n 5 env -C other",
    ],
)
def test_wrapper_directory_rebases_git(tmp_path: Path, prefix: str) -> None:
    data = _metadata(f"{prefix} git add x.py", tmp_path)
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_file_paths"] == [str(tmp_path / "other/x.py")]
    assert not data.get("canonical_repo_mutation_scope_unknown")


@pytest.mark.parametrize(
    "prefix",
    [
        "nohup",
        "nohup --",
        "/usr/bin/nohup",
        "timeout 5",
        "timeout --foreground 0.5s",
        "timeout --signal=TERM --kill-after=1s 5",
        "nice -n 5",
        "nice --adjustment=-5",
        "nice -n5",
        "nice -5",
        "env -i -v",
        "env -u X",
        "env --unset=X",
        "command -p nohup timeout 5",
    ],
)
def test_wrapped_touch_matches_bare_write(tmp_path: Path, prefix: str) -> None:
    data = _metadata(f"{prefix} touch x.py", tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_file_paths"] == ["x.py"]
    assert data["canonical_write_file_paths"] == ["x.py"]


@pytest.mark.parametrize(
    "command",
    [
        "env --unrecognized touch x.py",
        "env -S 'touch x.py'",
        "nohup -x touch x.py",
        "timeout --unrecognized 5 touch x.py",
        "timeout -s",
        "timeout bad touch x.py",
        "nice -n bad touch x.py",
        "nice --unrecognized touch x.py",
        "env --unrecognized touch x.py > out.py",
        "nohup --unrecognized touch x.py > out.py",
    ],
)
def test_unparseable_wrapper_fails_closed(tmp_path: Path, command: str) -> None:
    data = _metadata(command, tmp_path)
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True
    assert data.get("canonical_file_paths", []) == (["out.py"] if "> out.py" in command else [])


@pytest.mark.parametrize(
    "terminator, expected", [(";;", "e.py"), (";&", "sub/e.py"), (";;&", "sub/e.py")]
)
def test_case_arm_directory_state(tmp_path: Path, terminator: str, expected: str) -> None:
    data = _metadata(f"case x in x) cd sub; touch c.py{terminator} d) touch e.py;; esac", tmp_path)
    assert data["canonical_file_paths"] == ["sub/c.py", expected]


def test_case_write_preserves_following_write_scope(tmp_path: Path) -> None:
    data = _metadata("case x in x) touch c.py;; esac; touch d.py", tmp_path)
    assert data["canonical_file_paths"] == ["c.py", "d.py"]
    assert not data.get("canonical_repo_mutation_scope_unknown")


@pytest.mark.parametrize(
    "command, expected",
    [
        ("if true; then cd sub; else touch g.py; fi", ["g.py"]),
        (
            "cd parent; if true; then cd sub; elif false; then touch g.py; else touch h.py; fi",
            ["parent/g.py", "parent/h.py"],
        ),
        (
            "if true; then if false; then cd sub; else touch a.py; fi; else touch b.py; fi",
            ["a.py", "b.py"],
        ),
    ],
)
def test_conditional_branches_restore_entry_directory(
    tmp_path: Path, command: str, expected: list[str]
) -> None:
    assert _metadata(command, tmp_path)["canonical_file_paths"] == expected


@pytest.mark.parametrize(
    "command",
    [
        "case x in x) cd sub;; y) true;; esac; touch after.py",
        "if true; then cd sub; else true; fi; touch after.py",
        'cd "$X"; touch after.py',
        'cd "$X"; git add after.py',
        "cd; touch after.py",
    ],
)
def test_unproven_directory_does_not_fall_back_to_tool_cwd(tmp_path: Path, command: str) -> None:
    data = _metadata(command, tmp_path)
    assert data["canonical_repo_mutation_scope_unknown"] is True
    assert not data.get("canonical_file_paths")


@pytest.mark.parametrize("operation", ["add x.py", "revert --quit"])
def test_literal_dollar_checkout_remains_resolved(tmp_path: Path, operation: str) -> None:
    root = tmp_path / "$checkout"
    root.mkdir()
    data = _metadata(f"git {operation}", root)
    assert data["canonical_repo_mutation"] is True
    assert not data.get("canonical_repo_mutation_scope_unknown")
    assert data["canonical_file_paths"] == [
        str(root / "x.py") if operation.startswith("add") else str(root)
    ]


def test_dynamic_operand_cannot_borrow_literal_checkout_proof(tmp_path: Path) -> None:
    root = tmp_path / "$checkout"
    root.mkdir()
    data = _metadata(f"git add {root}/x.py", root)
    assert not data.get("canonical_file_paths")
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_tool_kind"] == "execute"


@pytest.mark.parametrize("selector", ["GIT_DIR", "GIT_WORK_TREE"])
@pytest.mark.parametrize("setup", ["export", "declare -x", "readonly"])
def test_eval_selector_setup_persists_fail_closed(
    tmp_path: Path, selector: str, setup: str
) -> None:
    data = _metadata(f"eval '{setup} {selector}=/other'; git add x.py", tmp_path)
    assert data["canonical_repo_mutation_scope_unknown"] is True
    assert not data.get("canonical_file_paths")


@pytest.mark.parametrize("command", ["cp sub/*.py .", "cp -r sub/ .", 'cp "$X" .'])
def test_root_copy_records_destination_directory(tmp_path: Path, command: str) -> None:
    data = _metadata(command, tmp_path)
    assert data["canonical_file_paths"] == [str(tmp_path)]
    assert data["canonical_repo_mutation"] is True


@pytest.mark.parametrize("command", ["cp sub/*.py .", "cp -r sub/ .", 'cp "$X" .'])
def test_root_copy_without_directory_proof_fails_closed(command: str) -> None:
    data: dict[str, Any] = {"tool_name": "Bash", "tool_input": {"command": command}}
    _set_canonical_tool_metadata(data)
    assert data["canonical_repo_mutation_scope_unknown"] is True
    assert not data.get("canonical_file_paths")


def test_nested_case_restores_outer_arm_directory(tmp_path: Path) -> None:
    data = _metadata(
        "case x in x) case y in y) cd sub; touch a.py;; esac;; z) touch b.py;; esac",
        tmp_path,
    )
    assert data["canonical_file_paths"] == ["sub/a.py", "b.py"]


@pytest.mark.parametrize(
    "options",
    [
        "-C /tmp/x",
        "-c color.ui=false -C /tmp/x",
        "--git-dir=/tmp/x/.git --work-tree=/tmp/x",
        "--no-pager -C /tmp/x",
    ],
)
@pytest.mark.parametrize("revision", ["", "HEAD "])
def test_git_global_options_preserve_search_classification(
    tmp_path: Path, options: str, revision: str
) -> None:
    data = _metadata(f"git {options} grep -n P {revision}-- src/", tmp_path)
    assert data["canonical_tool_kind"] == "search"
    assert data["canonical_code_navigation_action"] == "search"
    assert bool(data.get("canonical_search_revision_scoped")) == bool(revision)
