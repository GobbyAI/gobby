"""Focused acceptance tests for generated git hook bodies.

The codewiki curl refresh (and its authentication headers) was removed in
gobby-#19825; the reindex body now only feeds changed files to the local gcode
binary. The pre-commit wrapper keeps pre-commit auto-fixes inside the caller's
in-progress commit (gobby-#22419).
"""

import os
import subprocess
from pathlib import Path

import pytest

from gobby.cli.installers.git_hooks import _CODE_INDEX_REINDEX_BODY, install_git_hooks

pytestmark = pytest.mark.unit


def _write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _run_hook_body(
    work_dir: Path,
    *,
    changed_files: str = "changed.py",
    gcode_available: bool = True,
    strict_unset: bool = False,
) -> list[str]:
    home = work_dir / "home"
    capture = work_dir / "gcode-args"

    if gcode_available:
        _write_executable(
            home / ".gobby/bin/gcode",
            '#!/bin/sh\nprintf "%s\\n" "$@" > "$GCODE_CAPTURE"\n',
        )

    env = os.environ | {
        "CHANGED_FILES": changed_files,
        "GCODE_CAPTURE": str(capture),
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
    }
    prelude = "set -u\n" if strict_unset else ""
    result = subprocess.run(
        ["/bin/bash", "-c", f"{prelude}{_CODE_INDEX_REINDEX_BODY}\nwait"],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    if not capture.exists():
        return []
    return capture.read_text(encoding="utf-8").splitlines()


def test_hook_body_reindexes_changed_files(tmp_path: Path) -> None:
    # A changed file named like an option still reaches gcode as a --files value.
    args = _run_hook_body(tmp_path, changed_files="changed.py\n-l")

    assert args == ["index", "--quiet", "--skip-if-locked", "--files=changed.py", "--files=-l"]


def test_hook_body_skips_when_gcode_missing(tmp_path: Path) -> None:
    args = _run_hook_body(tmp_path, gcode_available=False)

    assert args == []


def test_hook_body_skips_without_changed_files(tmp_path: Path) -> None:
    args = _run_hook_body(tmp_path, changed_files="")

    assert args == []


def test_hook_body_survives_set_u(tmp_path: Path) -> None:
    """Chained user hooks run under set -u; the body must not abort."""
    args = _run_hook_body(tmp_path, strict_unset=True)

    assert args == ["index", "--quiet", "--skip-if-locked", "--files=changed.py"]


def _write_fake_precommit(bin_dir: Path) -> None:
    """pre-commit double: a formatter plus a lint check it cannot fix.

    Like the framework, it checks the paths staged in GIT_INDEX_FILE, writes
    fixes only to the worktree, and fails whenever it modified a file. A staged
    file containing a ``lint-error`` line fails every run, fixed or not.
    """
    _write_executable(
        bin_dir / "pre-commit",
        "#!/bin/sh\n"
        "git diff --cached --name-only | {\n"
        "    status=0\n"
        "    while IFS= read -r file; do\n"
        '        if [ "$(tail -n 1 "$file")" != auto-fixed ]; then\n'
        '            printf "auto-fixed\\n" >> "$file"\n'
        "            status=1\n"
        "        fi\n"
        '        if grep -qx lint-error "$file"; then\n'
        "            status=1\n"
        "        fi\n"
        "    done\n"
        '    exit "$status"\n'
        "}\n",
    )


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git with hermetic config, a temporary HOME, and the fakes first on PATH."""
    work_dir = repo.parent
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env |= {
        "PATH": f"{work_dir / 'bin'}{os.pathsep}{env.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(work_dir / "home"),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@gobby.local",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@gobby.local",
    }
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _git_out(repo: Path, *args: str) -> str:
    result = _git(repo, *args)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _init_hook_repo(tmp_path: Path, names: list[str], *, linked_worktree: bool = False) -> Path:
    """Temporary repo with a baseline commit and the generated hooks installed.

    With ``linked_worktree`` the returned checkout is a linked worktree, whose
    real index lives under the common git dir's ``worktrees/`` entry.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_fake_precommit(tmp_path / "bin")
    _write_executable(tmp_path / "bin" / "gobby", "#!/bin/sh\nexit 0\n")
    (repo / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    for name in names:
        (repo / name).write_text("base\n", encoding="utf-8")

    _git_out(repo, "init", "-q")
    _git_out(repo, "add", "-A")
    _git_out(repo, "commit", "-q", "-m", "baseline")
    if linked_worktree:
        _git_out(repo, "worktree", "add", "-q", "-b", "feature", str(tmp_path / "linked"))
        repo = tmp_path / "linked"

    assert install_git_hooks(repo)["success"] is True
    return repo


def _subjects(repo: Path) -> list[str]:
    return _git_out(repo, "log", "--format=%s").splitlines()


def _committed_paths(repo: Path) -> list[str]:
    return _git_out(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines()


class TestPreCommitAutoFixBoundary:
    """The generated pre-commit section keeps auto-fixes inside the caller's commit."""

    def test_fixes_to_fully_staged_files_join_the_callers_commit(self, tmp_path: Path) -> None:
        repo = _init_hook_repo(tmp_path, ["F.txt", "G file.txt", "U.txt"])
        (repo / "F.txt").write_text("base\nedit-f\n", encoding="utf-8")
        (repo / "G file.txt").write_text("base\nedit-g\n", encoding="utf-8")
        _git_out(repo, "add", "F.txt", "G file.txt")
        # Unstaged work outside the commit must stay out of it.
        (repo / "U.txt").write_text("base\nunstaged\n", encoding="utf-8")

        result = _git(repo, "commit", "-m", "feat: deliverable")

        assert result.returncode == 0, result.stderr
        assert _subjects(repo) == ["feat: deliverable", "baseline"]
        assert _committed_paths(repo) == ["F.txt", "G file.txt"]
        assert _git_out(repo, "show", "HEAD:F.txt") == "base\nedit-f\nauto-fixed\n"
        assert _git_out(repo, "show", "HEAD:G file.txt") == "base\nedit-g\nauto-fixed\n"
        assert _git_out(repo, "status", "--porcelain") == " M U.txt\n"

    def test_fix_to_partially_staged_file_refuses_and_names_it(self, tmp_path: Path) -> None:
        repo = _init_hook_repo(tmp_path, ["A.txt"])
        (repo / "A.txt").write_text("base\nstaged\n", encoding="utf-8")
        _git_out(repo, "add", "A.txt")
        (repo / "A.txt").write_text("base\nstaged\nunstaged\n", encoding="utf-8")

        result = _git(repo, "commit", "-m", "feat: deliverable")

        assert result.returncode != 0
        assert "  A.txt" in result.stderr.splitlines()
        assert _subjects(repo) == ["baseline"]
        # The unstaged hunk and the fix stay out of the index.
        assert _git_out(repo, "show", ":A.txt") == "base\nstaged\n"
        assert (repo / "A.txt").read_text(encoding="utf-8") == (
            "base\nstaged\nunstaged\nauto-fixed\n"
        )

    def test_refusal_restages_nothing_when_fixes_are_mixed(self, tmp_path: Path) -> None:
        repo = _init_hook_repo(tmp_path, ["A.txt", "F.txt"])
        (repo / "F.txt").write_text("base\nedit-f\n", encoding="utf-8")
        (repo / "A.txt").write_text("base\nstaged\n", encoding="utf-8")
        _git_out(repo, "add", "A.txt", "F.txt")
        (repo / "A.txt").write_text("base\nstaged\nunstaged\n", encoding="utf-8")

        result = _git(repo, "commit", "-m", "feat: deliverable")

        assert result.returncode != 0
        stderr_lines = result.stderr.splitlines()
        assert "  A.txt" in stderr_lines
        assert "  F.txt" not in stderr_lines
        assert _subjects(repo) == ["baseline"]
        assert _git_out(repo, "show", ":F.txt") == "base\nedit-f\n"
        assert _git_out(repo, "show", ":A.txt") == "base\nstaged\n"
        assert (repo / "F.txt").read_text(encoding="utf-8") == "base\nedit-f\nauto-fixed\n"

    @pytest.mark.parametrize("linked_worktree", [False, True])
    def test_path_limited_commit_keeps_exactly_its_path_set(
        self, tmp_path: Path, linked_worktree: bool
    ) -> None:
        repo = _init_hook_repo(tmp_path, ["F.txt", "H.txt"], linked_worktree=linked_worktree)
        (repo / "F.txt").write_text("base\nedit-f\n", encoding="utf-8")
        (repo / "H.txt").write_text("base\nedit-h\n", encoding="utf-8")
        _git_out(repo, "add", "F.txt", "H.txt")

        result = _git(repo, "commit", "-m", "feat: deliverable", "--", "F.txt")

        assert result.returncode == 0, result.stderr
        assert _subjects(repo) == ["feat: deliverable", "baseline"]
        assert _committed_paths(repo) == ["F.txt"]
        assert _git_out(repo, "show", "HEAD:F.txt") == "base\nedit-f\nauto-fixed\n"
        # H.txt stays staged and unfixed for a later commit, and the real index
        # entry for F.txt matches the commit instead of the pre-fix snapshot.
        assert _git_out(repo, "status", "--porcelain") == "M  H.txt\n"
        assert _git_out(repo, "show", ":H.txt") == "base\nedit-h\n"
        assert (repo / "H.txt").read_text(encoding="utf-8") == "base\nedit-h\n"

    def test_commit_all_keeps_exactly_its_path_set(self, tmp_path: Path) -> None:
        repo = _init_hook_repo(tmp_path, ["F.txt", "H.txt"])
        (repo / "F.txt").write_text("base\nedit-f\n", encoding="utf-8")
        (repo / "H.txt").write_text("base\nedit-h\n", encoding="utf-8")
        (repo / "N.txt").write_text("untracked\n", encoding="utf-8")

        result = _git(repo, "commit", "-a", "-m", "feat: deliverable")

        assert result.returncode == 0, result.stderr
        assert _subjects(repo) == ["feat: deliverable", "baseline"]
        assert _committed_paths(repo) == ["F.txt", "H.txt"]
        assert _git_out(repo, "show", "HEAD:F.txt") == "base\nedit-f\nauto-fixed\n"
        assert _git_out(repo, "show", "HEAD:H.txt") == "base\nedit-h\nauto-fixed\n"
        assert _git_out(repo, "status", "--porcelain") == "?? N.txt\n"

    def test_failure_left_after_restaging_still_blocks_the_commit(self, tmp_path: Path) -> None:
        repo = _init_hook_repo(tmp_path, ["F.txt"])
        (repo / "F.txt").write_text("base\nlint-error\n", encoding="utf-8")
        _git_out(repo, "add", "F.txt")

        result = _git(repo, "commit", "-m", "feat: deliverable")

        assert result.returncode != 0
        assert _subjects(repo) == ["baseline"]
        # The fix was restaged; the unfixable failure is what refused the commit.
        assert _git_out(repo, "show", ":F.txt") == "base\nlint-error\nauto-fixed\n"
