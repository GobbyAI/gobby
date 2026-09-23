"""Operation-owned merge cleanup and commit-based landing reconciliation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from gobby.utils.git import run_to_completion, stash_ref_for_oid
from gobby.worktrees.git.manager import WorktreeGitManager


@dataclass
class MergeRecovery:
    """Keep cleanup failures separate from the Git-proven delivery outcome."""

    git: WorktreeGitManager
    cwd: str
    target_ref: str
    original_branch: str = ""
    checked_out_target: bool = False
    merge_cleanup_required: bool = False
    stash_oid: str | None = None
    source_sha: str | None = None
    target_before_sha: str | None = None
    warnings: list[dict[str, Any]] = field(default_factory=list)

    async def _run(self, args: list[str], timeout: int = 10) -> Any:
        return await run_to_completion(
            self.git.run_git_command(args, cwd=self.cwd, timeout=timeout)
        )

    async def capture(self, source_ref: str) -> None:
        """Capture both identities before making any checkout mutation."""
        for ref, attribute in (
            (source_ref, "source_sha"),
            (self.target_ref, "target_before_sha"),
        ):
            result = await self._run(["rev-parse", ref])
            if result.returncode != 0 or not result.stdout.strip():
                raise RuntimeError(f"Failed to capture merge identity {ref}: {result.stderr}")
            setattr(self, attribute, result.stdout.strip())

    def warn(self, step: str, error: Exception) -> None:
        self.warnings.append(
            {"step": step, "error": str(error), "retained_stash_oid": self.stash_oid}
        )

    async def _abort(self) -> None:
        if not self.merge_cleanup_required:
            return
        result = await self._run(["rev-parse", "--verify", "-q", "MERGE_HEAD"])
        if result.returncode == 1:
            self.merge_cleanup_required = False
            return
        if result.returncode != 0:
            raise RuntimeError(f"Failed to inspect failed merge state: {result.stderr}")
        result = await self._run(["merge", "--abort"])
        if result.returncode != 0:
            raise RuntimeError(f"Failed to abort merge_worktree merge: {result.stderr}")
        self.merge_cleanup_required = False

    async def cleanup(self, merge_target: str) -> None:
        """Attempt independent cleanup steps without replacing the merge result."""
        try:
            await self._abort()
        except Exception as error:
            self.warn("abort-merge", error)
        if self.checked_out_target and self.original_branch != merge_target:
            try:
                result = await self._run(["checkout", self.original_branch], timeout=30)
                if result.returncode != 0:
                    raise RuntimeError(
                        f"Failed to restore original branch {self.original_branch}: {result.stderr}"
                    )
            except Exception as error:
                self.warn("restore-branch", error)
        if not self.stash_oid:
            return
        step = "stash-list"
        try:
            result = await self._run(["stash", "list", "--format=%gd%x00%H"])
            if result.returncode != 0:
                raise RuntimeError(f"Failed to locate merge_worktree stash: {result.stderr}")
            stash_ref = stash_ref_for_oid(result.stdout, self.stash_oid)
            if stash_ref is None:
                raise RuntimeError(f"Failed to locate exact merge_worktree stash {self.stash_oid}")
            step = "stash-pop"
            result = await self._run(["stash", "pop", "--index", stash_ref])
            if result.returncode != 0:
                raise RuntimeError(f"Failed to restore stashed .gobby/ files: {result.stderr}")
            self.stash_oid = None
        except Exception as error:
            # Pop may already have applied changes. Never retry or drop an ambiguous stash.
            self.warn(step, error)

    async def reconcile(self) -> dict[str, Any]:
        """Prove ancestry using captured source and a single observed target commit."""
        outcome: dict[str, Any] = {
            "landing_state": "unknown",
            "source_sha": self.source_sha,
            "target_before_sha": self.target_before_sha,
        }
        if not self.source_sha or not self.target_before_sha:
            return outcome
        try:
            target = await self._run(["rev-parse", self.target_ref])
            if target.returncode != 0 or not target.stdout.strip():
                raise RuntimeError(
                    f"Failed to resolve target during reconciliation: {target.stderr}"
                )
            target_sha = target.stdout.strip()
            ancestor = await self._run(["merge-base", "--is-ancestor", self.source_sha, target_sha])
            if ancestor.returncode == 1:
                outcome["landing_state"] = "not-landed"
            elif ancestor.returncode == 0:
                outcome.update(
                    landing_state="landed",
                    success=True,
                    merged=True,
                    reconciled=True,
                    merge_sha=target_sha,
                    target_head_sha=target_sha,
                    commit_sha=target_sha,
                )
            else:
                raise RuntimeError(f"Failed to verify target ancestry: {ancestor.stderr}")
        except Exception as error:
            outcome["reconciliation_error"] = str(error)
        return outcome
