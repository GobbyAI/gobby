"""Helpers for probing branch protection during merge delivery."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from gobby.utils.daemon_git import daemon_git

if TYPE_CHECKING:
    from gobby.worktrees.git import WorktreeGitManager

_PROTECTED_PUSH_MARKERS = (
    "protected branch hook declined",
    "protected branch",
    "branch is protected",
    "required status check",
    "required status checks",
    "pull request",
    "pre-receive hook declined",
    "gh006",
)
_PROTECTION_PROBE_TIMEOUT_SECONDS = 30


def protection_payload(
    *,
    branch: str,
    source: str,
    requires_pr: bool,
    protection_unknown: bool = False,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "success": True,
        "branch": branch,
        "source": source,
        "requires_pr": requires_pr,
        "protection_unknown": protection_unknown,
        "error": error,
    }


def git_output(result: Any) -> str:
    return (result.stderr or result.stdout or "").strip()


async def push_dry_run_probe(
    *,
    repo_path: str,
    branch: str,
    git_manager: WorktreeGitManager | None,
    source: str,
    error: str | None,
) -> dict[str, Any]:
    command = ["push", "--dry-run", "origin", f"HEAD:{branch}"]
    returncode: int | None
    if git_manager is not None:
        result = await git_manager.run_git_command(
            command,
            cwd=repo_path,
            timeout=_PROTECTION_PROBE_TIMEOUT_SECONDS,
        )
        returncode = result.returncode
        output = f"{result.stdout}\n{result.stderr}"
    else:
        daemon_result = await daemon_git.run(
            command,
            cwd=repo_path,
            timeout=_PROTECTION_PROBE_TIMEOUT_SECONDS,
        )
        returncode = daemon_result.returncode
        output = f"{daemon_result.stdout}\n{daemon_result.stderr}"

    lowered = output.lower()
    looks_protected = any(marker in lowered for marker in _PROTECTED_PUSH_MARKERS)
    if returncode == 0:
        requires_pr = False
        protection_unknown = False
    else:
        requires_pr = True
        protection_unknown = not looks_protected
    return protection_payload(
        branch=branch,
        source=source,
        requires_pr=requires_pr,
        protection_unknown=protection_unknown,
        error=error or (output.strip() if returncode != 0 else None),
    )
