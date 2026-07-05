"""Wiki branch/worktree setup for Git-backed Gobby projects."""

import logging
import shutil
import subprocess  # nosec B404 - scoped git commands, no shell
from pathlib import Path
from typing import Any

from gobby.utils.wiki_vault import resolve_vault_dir

logger = logging.getLogger(__name__)

WIKI_BRANCH = "wiki"
GITIGNORE_START = "# >>> GOBBY WIKI START >>>"
GITIGNORE_END = "# <<< GOBBY WIKI END <<<"


def _gitignore_block(vault_dir: str) -> str:
    # Root-anchored: an unanchored `wiki/` would also ignore nested source
    # directories such as src/gobby/wiki/.
    return f"""{GITIGNORE_START}
# Gobby local wiki vault; pre-push publishes it to branch `wiki`.
/{vault_dir}/
{GITIGNORE_END}
"""


def default_wiki_setup_result() -> dict[str, Any]:
    """Return the standard wiki setup result shape."""
    return {
        "success": False,
        "gitignore_updated": False,
        "gitignore_status": "unknown",
        "worktree_path": None,
        "branch": WIKI_BRANCH,
        "vault_dir": None,
        "warnings": [],
        "tracked_files": [],
    }


def setup_wiki_branch(
    project_path: Path,
    *,
    require_project_root: bool = False,
) -> dict[str, Any]:
    """Prepare branch-local wiki vault publishing for a Git repository."""
    result = default_wiki_setup_result()
    repo_candidate = project_path.resolve()
    git_root = _git_toplevel(repo_candidate)
    if git_root is None:
        result["warnings"].append("Wiki setup skipped: not a Git repository.")
        return result

    if require_project_root and git_root != repo_candidate:
        result["warnings"].append(
            "Wiki setup skipped: "
            f"{repo_candidate} is inside Git repository {git_root}. "
            "Run `gobby init` from the repository root to enable wiki branch publishing."
        )
        return result

    vault_path = resolve_vault_dir(git_root)
    if vault_path is None:
        result["warnings"].append(
            f"Wiki setup skipped: no usable wiki vault directory under {git_root}; "
            "every candidate is occupied by a non-vault path."
        )
        return result
    vault_dir = vault_path.name
    result["vault_dir"] = vault_dir

    gitignore_status, gitignore_warning = _ensure_gitignore_block(git_root, vault_dir)
    result["gitignore_status"] = gitignore_status
    result["gitignore_updated"] = gitignore_status == "updated"
    if gitignore_warning:
        result["warnings"].append(gitignore_warning)

    tracked_files = _tracked_wiki_files(git_root, vault_dir)
    result["tracked_files"] = tracked_files
    if tracked_files:
        result["warnings"].append(
            f"{vault_dir}/ has tracked files. Leave them as-is for now; "
            f"to make the vault branch-local, run: git rm --cached -r {vault_dir}"
        )

    worktree_path = _wiki_worktree_path(git_root)
    result["worktree_path"] = str(worktree_path)
    if not _ensure_wiki_worktree(git_root, worktree_path, result["warnings"]):
        return result

    if vault_path.is_dir():
        _mirror_wiki_vault(vault_path, worktree_path, result["warnings"])

    result["success"] = True
    return result


def _run_git(repo_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _git_toplevel(project_path: Path) -> Path | None:
    if not project_path.exists():
        return None

    proc = _run_git(project_path, "rev-parse", "--show-toplevel")
    if proc.returncode != 0:
        return None

    root = proc.stdout.strip()
    return Path(root).resolve() if root else None


def _ensure_gitignore_block(repo_path: Path, vault_dir: str) -> tuple[str, str | None]:
    gitignore_path = repo_path / ".gitignore"
    try:
        original = gitignore_path.read_text(encoding="utf-8") if gitignore_path.exists() else ""
    except OSError as exc:
        warning = f"Failed to read .gitignore for wiki setup: {exc}"
        logger.warning(warning)
        return "failed", warning

    updated = _replace_gitignore_block(original, vault_dir)
    if updated == original:
        return "unchanged", None

    try:
        gitignore_path.write_text(updated, encoding="utf-8")
    except OSError as exc:
        warning = f"Failed to update .gitignore for wiki setup: {exc}"
        logger.warning(warning)
        return "failed", warning
    return "updated", None


def _replace_gitignore_block(content: str, vault_dir: str) -> str:
    block = _gitignore_block(vault_dir).rstrip() + "\n"
    start = content.find(GITIGNORE_START)
    end = content.find(GITIGNORE_END)
    if start != -1 and end != -1 and end > start:
        end += len(GITIGNORE_END)
        while end < len(content) and content[end] in "\r\n":
            end += 1
        prefix = content[:start].rstrip()
        suffix = content[end:].lstrip("\r\n")
        pieces = [piece for piece in (prefix, block.rstrip(), suffix.rstrip()) if piece]
        return "\n\n".join(pieces) + "\n"

    prefix = content.rstrip()
    return f"{prefix}\n\n{block}" if prefix else block


def _tracked_wiki_files(repo_path: Path, vault_dir: str) -> list[str]:
    proc = _run_git(repo_path, "ls-files", "--", vault_dir)
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line]


def _wiki_worktree_path(repo_path: Path) -> Path:
    return repo_path.parent / f"{repo_path.name}-wiki"


def _ensure_wiki_worktree(
    repo_path: Path,
    worktree_path: Path,
    warnings: list[str],
) -> bool:
    if worktree_path.exists():
        if _is_usable_wiki_worktree(repo_path, worktree_path):
            return True
        warnings.append(
            f"Wiki setup skipped: {worktree_path} already exists but is not a "
            f"`{WIKI_BRANCH}` worktree for this repository. Move it or configure it manually."
        )
        return False

    args: tuple[str, ...]
    if _local_branch_exists(repo_path):
        args = ("worktree", "add", str(worktree_path), WIKI_BRANCH)
    elif _remote_branch_exists(repo_path):
        args = ("worktree", "add", "-b", WIKI_BRANCH, str(worktree_path), f"origin/{WIKI_BRANCH}")
    else:
        args = ("worktree", "add", "--orphan", "-b", WIKI_BRANCH, str(worktree_path))

    proc = _run_git(repo_path, *args)
    if proc.returncode != 0:
        warnings.append(
            f"Wiki setup skipped: failed to create {WIKI_BRANCH} worktree at "
            f"{worktree_path}: {_git_error(proc)}"
        )
        return False
    return True


def _is_usable_wiki_worktree(repo_path: Path, worktree_path: Path) -> bool:
    inside = _run_git(worktree_path, "rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return False

    branch = _run_git(worktree_path, "branch", "--show-current")
    if branch.returncode != 0 or branch.stdout.strip() != WIKI_BRANCH:
        return False

    return _git_common_dir(repo_path) == _git_common_dir(worktree_path)


def _git_common_dir(repo_path: Path) -> Path | None:
    proc = _run_git(repo_path, "rev-parse", "--git-common-dir")
    if proc.returncode != 0:
        return None

    common_dir = Path(proc.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = repo_path / common_dir
    return common_dir.resolve()


def _local_branch_exists(repo_path: Path) -> bool:
    proc = _run_git(repo_path, "show-ref", "--verify", "--quiet", f"refs/heads/{WIKI_BRANCH}")
    return proc.returncode == 0


def _remote_branch_exists(repo_path: Path) -> bool:
    proc = _run_git(
        repo_path,
        "show-ref",
        "--verify",
        "--quiet",
        f"refs/remotes/origin/{WIKI_BRANCH}",
    )
    return proc.returncode == 0


def _mirror_wiki_vault(
    source_path: Path,
    worktree_path: Path,
    warnings: list[str],
) -> None:
    try:
        for child in worktree_path.iterdir():
            if child.name == ".git":
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()

        for child in source_path.iterdir():
            if child.name == ".git":
                continue
            target = worktree_path / child.name
            if child.is_dir() and not child.is_symlink():
                shutil.copytree(child, target, symlinks=True)
            else:
                shutil.copy2(child, target, follow_symlinks=False)
    except OSError as exc:
        warnings.append(f"Wiki vault mirror skipped: {exc}")
        return

    add_proc = _run_git(worktree_path, "add", "-A")
    if add_proc.returncode != 0:
        warnings.append(f"Wiki vault mirror skipped: git add failed: {_git_error(add_proc)}")
        return

    diff_proc = _run_git(worktree_path, "diff", "--cached", "--quiet", "--exit-code")
    if diff_proc.returncode == 0:
        return

    commit_proc = _run_git(worktree_path, "commit", "-m", "gobby: sync wiki vault", "--no-verify")
    if commit_proc.returncode != 0:
        warnings.append(f"Wiki vault mirrored but local commit failed: {_git_error(commit_proc)}")


def _git_error(proc: subprocess.CompletedProcess[str]) -> str:
    return (proc.stderr or proc.stdout or "unknown git error").strip()
