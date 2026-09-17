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

from gobby.cli.installers.git_hooks import (
    _CODE_INDEX_REINDEX_BODY,
    HOOK_TEMPLATES,
    install_git_hooks,
)

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
    """pre-commit double: auto-fix every staged file in the worktree, then fail.

    Mimics the framework's contract: a hook that modifies files makes the run
    exit non-zero. The fixes land only in the worktree, never in the index.
    """
    _write_executable(
        bin_dir / "pre-commit",
        "#!/bin/sh\n"
        "for file in $(git diff --name-only --cached); do\n"
        '    printf "auto-fixed\\n" >> "$file"\n'
        "done\n"
        'echo "files were modified by pre-commit hooks" >&2\n'
        "exit 1\n",
    )


def _hook_env(bin_dir: Path) -> dict[str, str]:
    """Isolated environment: fake tools first on PATH, no host git config."""
    env = os.environ | {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "HOME": str(bin_dir.parent / "home"),
    }
    env.pop("GIT_INDEX_FILE", None)
    return env


def _git(repo: Path, bin_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git in the repo with hermetic config and the fake tools on PATH."""
    return subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@gobby.local",
            "-c",
            "commit.gpgsign=false",
            "-c",
            f"core.hooksPath={repo / '.git' / 'hooks'}",
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_hook_env(bin_dir),
    )


def _init_hook_repo(tmp_path: Path, files: dict[str, str]) -> tuple[Path, Path]:
    """Temporary repo whose installed hooks come from the live templates."""
    repo = tmp_path / "repo"
    bin_dir = tmp_path / "bin"
    repo.mkdir()
    bin_dir.mkdir()
    _write_fake_precommit(bin_dir)
    _write_executable(bin_dir / "gobby", "#!/bin/sh\nexit 0\n")

    for name, content in files.items():
        (repo / name).write_text(content, encoding="utf-8")
    (repo / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")

    for args in (
        ("init", "-q"),
        ("add", "-A"),
        ("commit", "-q", "-m", "baseline", "--no-verify"),
    ):
        step = _git(repo, bin_dir, *args)
        assert step.returncode == 0, step.stderr

    result = install_git_hooks(repo)
    assert result["success"] is True
    return repo, bin_dir


class TestPreCommitAutoFixBoundary:
    """The pre-commit wrapper keeps auto-fixes inside the caller's commit."""

    def test_template_never_runs_git_commit(self) -> None:
        """The generated section never creates a commit of its own."""
        assert "git commit" not in HOOK_TEMPLATES["pre-commit"]
        assert "git add --" in HOOK_TEMPLATES["pre-commit"]

    def test_fully_staged_fixes_join_the_callers_commit(self, tmp_path: Path) -> None:
        """Auto-fixes to fully staged files land in the caller's single commit."""
        repo, bin_dir = _init_hook_repo(tmp_path, {"F.txt": "base\n", "G.txt": "base\n"})

        (repo / "F.txt").write_text("base\nedit-f\n", encoding="utf-8")
        (repo / "G.txt").write_text("base\nedit-g\n", encoding="utf-8")
        assert _git(repo, bin_dir, "add", "-A").returncode == 0

        result = _git(repo, bin_dir, "commit", "-m", "feat: deliverable")

        assert result.returncode == 0, result.stderr + result.stdout
        # Exactly one commit: no separate style commit exists.
        subjects = _git(repo, bin_dir, "log", "--format=%s").stdout.splitlines()
        assert subjects == ["feat: deliverable", "baseline"]
        # The commit carries the staged changes plus the fixes and no other paths.
        changed = _git(repo, bin_dir, "show", "--name-only", "--format=", "HEAD").stdout.split()
        assert sorted(changed) == ["F.txt", "G.txt"]
        assert _git(repo, bin_dir, "show", "HEAD:F.txt").stdout == "base\nedit-f\nauto-fixed\n"
        assert _git(repo, bin_dir, "show", "HEAD:G.txt").stdout == "base\nedit-g\nauto-fixed\n"
        # The fixes were absorbed, so the worktree is left clean.
        assert _git(repo, bin_dir, "status", "--porcelain").stdout == ""

    def test_fix_on_partially_staged_file_refuses_and_names_it(self, tmp_path: Path) -> None:
        """An auto-fix entangled with unstaged hunks refuses the commit instead."""
        repo, bin_dir = _init_hook_repo(tmp_path, {"A.txt": "base\n"})

        (repo / "A.txt").write_text("base\nstaged\n", encoding="utf-8")
        assert _git(repo, bin_dir, "add", "A.txt").returncode == 0
        (repo / "A.txt").write_text("base\nstaged\nunstaged\n", encoding="utf-8")

        result = _git(repo, bin_dir, "commit", "-m", "feat: deliverable")

        assert result.returncode != 0
        assert "A.txt" in result.stdout
        # No commit was created and the staged boundary is untouched.
        assert _git(repo, bin_dir, "log", "--format=%s").stdout.splitlines() == ["baseline"]
        assert _git(repo, bin_dir, "show", ":0:A.txt").stdout == "base\nstaged\n"
        # The unstaged hunks and the fix stay out of the index.
        assert _git(repo, bin_dir, "diff", "--name-only").stdout.splitlines() == ["A.txt"]
        assert (repo / "A.txt").read_text(encoding="utf-8") == (
            "base\nstaged\nunstaged\nauto-fixed\n"
        )

    def test_path_limited_commit_keeps_exactly_its_path_set(self, tmp_path: Path) -> None:
        """git commit -- <path> commits only its own path, fixes included."""
        repo, bin_dir = _init_hook_repo(tmp_path, {"F.txt": "base\n", "H.txt": "base\n"})

        (repo / "F.txt").write_text("base\nedit-f\n", encoding="utf-8")
        (repo / "H.txt").write_text("base\nedit-h\n", encoding="utf-8")
        assert _git(repo, bin_dir, "add", "-A").returncode == 0

        result = _git(repo, bin_dir, "commit", "-m", "feat: deliverable", "--", "F.txt")

        assert result.returncode == 0, result.stderr + result.stdout
        changed = _git(repo, bin_dir, "show", "--name-only", "--format=", "HEAD").stdout.split()
        assert changed == ["F.txt"]
        # The fix for the named path is inside the path-limited commit.
        assert _git(repo, bin_dir, "show", "HEAD:F.txt").stdout == "base\nedit-f\nauto-fixed\n"
        # The out-of-path staged file keeps its staged content for a later commit.
        assert _git(repo, bin_dir, "show", ":0:H.txt").stdout == "base\nedit-h\n"
        assert (repo / "H.txt").read_text(encoding="utf-8") == "base\nedit-h\n"

    def test_commit_a_keeps_exactly_its_path_set(self, tmp_path: Path) -> None:
        """git commit -a commits exactly the swept paths, fixes included."""
        repo, bin_dir = _init_hook_repo(tmp_path, {"F.txt": "base\n", "H.txt": "base\n"})

        (repo / "F.txt").write_text("base\nedit-f\n", encoding="utf-8")
        (repo / "H.txt").write_text("base\nedit-h\n", encoding="utf-8")

        result = _git(repo, bin_dir, "commit", "-a", "-m", "feat: deliverable")

        assert result.returncode == 0, result.stderr + result.stdout
        changed = _git(repo, bin_dir, "show", "--name-only", "--format=", "HEAD").stdout.split()
        assert sorted(changed) == ["F.txt", "H.txt"]
        assert _git(repo, bin_dir, "show", "HEAD:F.txt").stdout == "base\nedit-f\nauto-fixed\n"
        assert _git(repo, bin_dir, "show", "HEAD:H.txt").stdout == "base\nedit-h\nauto-fixed\n"
        # Everything swept and fixed was committed; nothing is left behind.
        assert _git(repo, bin_dir, "status", "--porcelain").stdout == ""

    def test_mixed_fixes_refuse_without_staging_the_clean_files(self, tmp_path: Path) -> None:
        """When clean and conflicted fixes coexist, the index stays untouched."""
        repo, bin_dir = _init_hook_repo(tmp_path, {"F.txt": "base\n", "A.txt": "base\n"})

        (repo / "F.txt").write_text("base\nedit-f\n", encoding="utf-8")
        (repo / "A.txt").write_text("base\nstaged\n", encoding="utf-8")
        assert _git(repo, bin_dir, "add", "-A").returncode == 0
        (repo / "A.txt").write_text("base\nstaged\nunstaged\n", encoding="utf-8")

        result = _git(repo, bin_dir, "commit", "-m", "feat: deliverable")

        assert result.returncode != 0
        assert "A.txt" in result.stdout
        assert _git(repo, bin_dir, "log", "--format=%s").stdout.splitlines() == ["baseline"]
        # Neither file's staged content was swept into the refused commit's index.
        assert _git(repo, bin_dir, "show", ":0:F.txt").stdout == "base\nedit-f\n"
        assert _git(repo, bin_dir, "show", ":0:A.txt").stdout == "base\nstaged\n"
        # The fixes stay in the worktree for the caller to resolve and restage.
        assert (repo / "F.txt").read_text(encoding="utf-8") == "base\nedit-f\nauto-fixed\n"
        assert (repo / "A.txt").read_text(encoding="utf-8") == (
            "base\nstaged\nunstaged\nauto-fixed\n"
        )
