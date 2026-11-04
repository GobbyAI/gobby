"""Git apply modes must retain the distinction between index and file writes."""

import subprocess
from pathlib import Path
from typing import Any

import pytest

from gobby.hooks._normalization_tools import _set_canonical_tool_metadata

pytestmark = pytest.mark.unit


def invocation(command: str, cwd: Path) -> dict[str, Any]:
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": command, "cwd": str(cwd)},
        "project_path": str(cwd),
    }
    _set_canonical_tool_metadata(data)
    return data


@pytest.mark.parametrize(
    "arguments",
    [
        "--check change.patch",
        "--stat change.patch",
        "--numstat change.patch",
        "--summary change.patch",
        "--cached --check change.patch",
        "--cached --reject --check change.patch",
        "-h",
        "--help",
    ],
)
def test_git_apply_inspection_does_not_mutate(tmp_path: Path, arguments: str) -> None:
    data = invocation(f"git apply {arguments}", tmp_path)
    assert data["canonical_tool_kind"] == "execute"
    assert not data.get("canonical_repo_mutation")


@pytest.mark.parametrize(
    "arguments",
    [
        "change.patch",
        "--stat --apply change.patch",
        "--apply --stat change.patch",
        "--include --check change.patch",
        "--directory --stat change.patch",
        "-- --check",
        "--cached --unknown change.patch",
        "--cached --whitespace",
        "--check --build-fake-ancestor fake-index change.patch",
        "--cached --build-fake-ancestor=fake-index change.patch",
        "--cached --reject change.patch",
        "--cached --reject --check --apply change.patch",
        '--cached "$options" change.patch',
        "--cached *",
    ],
)
def test_git_apply_unproven_worktree_writes_stay_unknown(tmp_path: Path, arguments: str) -> None:
    data = invocation(f"git apply {arguments}", tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize(
    "arguments",
    [
        "--cached change.patch",
        "--cached --3way change.patch",
        "--cached --stat --apply change.patch",
        "--cached --reverse -p1 -C2 --whitespace=fix change.patch",
        "--cached --include owned.py --exclude ignored.py change.patch",
    ],
)
def test_git_apply_cached_only_mutates_index(tmp_path: Path, arguments: str) -> None:
    data = invocation(f"git apply {arguments}", tmp_path)
    assert data["canonical_tool_kind"] == "execute"
    assert data["canonical_repo_mutation"] is True
    assert data.get("canonical_edit_file_paths", []) == []
    assert not data.get("canonical_repo_mutation_scope_unknown")


def test_git_apply_dynamic_index_selector_stays_unknown(tmp_path: Path) -> None:
    data = invocation('GIT_INDEX_FILE="$index" git apply --cached change.patch', tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation_scope_unknown"] is True


MODIFY = "diff --git a/src/owned.py b/src/owned.py\n--- a/src/owned.py\n+++ b/src/owned.py\n"
PATCH_TARGETS = {
    "modify": (MODIFY + "@@ -1 +1 @@\n-a\n+b\n", ["src/owned.py"]),
    "add": (
        "diff --git a/new.py b/new.py\nnew file mode 100644\n--- /dev/null\n+++ b/new.py\n"
        "@@ -0,0 +1 @@\n+x\n",
        ["new.py"],
    ),
    "delete": (
        "diff --git a/old.py b/old.py\ndeleted file mode 100644\n--- a/old.py\n+++ /dev/null\n"
        "@@ -1 +0,0 @@\n-x\n",
        ["old.py"],
    ),
    "rename": (
        "diff --git a/a.py b/b.py\nsimilarity index 100%\nrename from a.py\nrename to b.py\n",
        ["a.py", "b.py"],
    ),
    "mode": ("diff --git a/run.sh b/run.sh\nold mode 100644\nnew mode 100755\n", ["run.sh"]),
    "binary": (
        "diff --git a/logo.png b/logo.png\nGIT binary patch\nliteral 1\nIcmZo*00001\n\n",
        ["logo.png"],
    ),
    "traditional": ("--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n", ["x.py"]),
    "two files": (
        MODIFY + "diff --git a/run.sh b/run.sh\nold mode 100644\nnew mode 100755\n",
        ["run.sh", "src/owned.py"],
    ),
}


def _saved_patch(tmp_path: Path, text: str) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    patch = tmp_path / "workspace" / "set-aside.patch"
    patch.parent.mkdir()
    patch.write_text(text, encoding="utf-8")
    return repo.resolve(), patch.resolve()


@pytest.mark.parametrize("case", sorted(PATCH_TARGETS))
@pytest.mark.parametrize("flags", ["", "--index ", "-3 ", "-R --whitespace=fix -C1 "])
@pytest.mark.parametrize("subdirectory", ["", "src"])
def test_git_apply_of_a_literal_patch_writes_the_files_it_names(
    tmp_path: Path, case: str, flags: str, subdirectory: str
) -> None:
    text, targets = PATCH_TARGETS[case]
    repo, patch = _saved_patch(tmp_path, text)
    data = invocation(f"git apply {flags}{patch}", repo / subdirectory)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert not data.get("canonical_repo_mutation_scope_unknown")
    assert sorted(data["canonical_file_paths"]) == [str(repo / path) for path in targets]


@pytest.mark.parametrize(
    ("flags", "text"),
    [
        pytest.param("-p2 ", MODIFY, id="strip-depth"),
        pytest.param("--directory=src ", MODIFY, id="directory"),
        pytest.param("--include=src/owned.py ", MODIFY, id="include"),
        pytest.param("--exclude=x ", MODIFY, id="exclude"),
        pytest.param("--unsafe-paths ", MODIFY, id="unsafe-paths"),
        pytest.param("", '--- "a/we\\tird.py"\n+++ "b/we\\tird.py"\n', id="quoted-path"),
        pytest.param("", "--- x.py\n+++ x.py\n", id="no-prefix"),
        pytest.param("", "--- a/../outside.py\n+++ b/../outside.py\n", id="parent-escape"),
        pytest.param("", "diff --git a/x b/y\nold mode 100644\nnew mode 100755\n", id="header"),
        pytest.param("", "no patch here\n", id="no-targets"),
    ],
)
def test_git_apply_of_an_unprovable_patch_stays_unknown(
    tmp_path: Path, flags: str, text: str
) -> None:
    repo, patch = _saved_patch(tmp_path, text)
    data = invocation(f"git apply {flags}{patch}", repo)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize("operand", ["", " -", " missing.patch", " ~/set-aside.patch"])
def test_git_apply_without_a_readable_patch_file_stays_unknown(
    tmp_path: Path, operand: str
) -> None:
    repo, _ = _saved_patch(tmp_path, MODIFY)
    data = invocation(f"git apply{operand}", repo)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation_scope_unknown"] is True
