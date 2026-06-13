"""Shadow git checkpoint manager.

Creates checkpoints of uncommitted agent work using git plumbing commands,
storing them as hidden refs (refs/gobby/ckpt/<task_id>/<seq>) without
touching HEAD or the working branch.

This preserves agent work before the lifecycle monitor kills a doom-looping
agent, allowing the work to be recovered later.
"""

from __future__ import annotations

import logging
import re
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

from gobby.storage.checkpoints import Checkpoint, LocalCheckpointManager

logger = logging.getLogger(__name__)


class CheckpointManager:
    """Creates shadow git checkpoints without touching the working branch."""

    def __init__(self, checkpoint_storage: LocalCheckpointManager) -> None:
        self._storage = checkpoint_storage

    def create_checkpoint(
        self,
        cwd: str | Path,
        task_id: str,
        session_id: str | None,
        run_id: str,
    ) -> Checkpoint | None:
        """Create a checkpoint if there are uncommitted changes.

        Uses git plumbing to create a detached commit on a hidden ref
        without modifying HEAD or the working branch.

        Returns None if no changes to checkpoint.
        """
        cwd_str = str(cwd)

        # 0. Sanitize task_id for use in git ref paths
        if not re.match(r"^[\w-]+$", task_id):
            logger.error(f"Invalid task_id for checkpoint ref: {task_id!r}")
            return None

        # 1. Check for uncommitted changes
        status = self._run_git(["status", "--porcelain"], cwd_str)
        if status is None or not status.strip():
            logger.debug(f"No uncommitted changes to checkpoint in {cwd_str}")
            return None

        files_changed = len(status.strip().splitlines())

        # 2. Snapshot the original index so divergent staged blobs survive temporary staging.
        original_index_tree = self._run_git(["write-tree"], cwd_str)
        if not original_index_tree:
            logger.error(f"Failed to snapshot index before checkpoint in {cwd_str}")
            return None
        original_index_tree = original_index_tree.strip()

        try:
            # 3. Stage tracked files only (needed for write-tree).
            # Uses -u to avoid capturing untracked artifacts.
            if self._run_git(["add", "-u"], cwd_str) is None:
                logger.error(f"Failed to stage files for checkpoint in {cwd_str}")
                return None

            # 4. Write tree (captures staged state as a tree object)
            tree_sha = self._run_git(["write-tree"], cwd_str)
            if not tree_sha:
                logger.error(f"Failed to write tree for checkpoint in {cwd_str}")
                return None
            tree_sha = tree_sha.strip()

            # 5. Get parent commit
            parent_sha = self._run_git(["rev-parse", "HEAD"], cwd_str)
            if not parent_sha:
                logger.error(f"Failed to get HEAD for checkpoint in {cwd_str}")
                return None
            parent_sha = parent_sha.strip()

            # 5. Create detached commit
            message = f"gobby: auto-checkpoint for task {task_id} (run {run_id[:8]})"
            commit_sha = self._run_git(
                ["commit-tree", tree_sha, "-p", parent_sha, "-m", message],
                cwd_str,
            )
            if not commit_sha:
                logger.error(f"Failed to create checkpoint commit in {cwd_str}")
                return None
            commit_sha = commit_sha.strip()

            # 6. Store as hidden ref
            seq = self._storage.count_for_task(task_id) + 1
            ref_name = f"refs/gobby/ckpt/{task_id}/{seq}"
            if self._run_git(["update-ref", ref_name, commit_sha], cwd_str) is None:
                logger.error(f"Failed to update ref {ref_name} in {cwd_str}")
                return None

            # 7. Record in DB
            checkpoint = Checkpoint(
                id=str(uuid.uuid4()),
                task_id=task_id,
                session_id=session_id,
                run_id=run_id,
                ref_name=ref_name,
                commit_sha=commit_sha,
                parent_sha=parent_sha,
                files_changed=files_changed,
                message=message,
                created_at=datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            )
            self._storage.create(checkpoint)

            logger.info(
                f"Created checkpoint {ref_name} ({files_changed} files, "
                f"commit {commit_sha[:8]}) for task {task_id}"
            )
            return checkpoint

        finally:
            # 9. Restore the exact index we had before temporary staging.
            # Best-effort: must not propagate exceptions from cleanup
            try:
                self._run_git(["read-tree", original_index_tree], cwd_str)
            except Exception as e:
                logger.warning(f"Failed to restore index after checkpoint in {cwd_str}: {e}")

    def _run_git(self, args: list[str], cwd: str, timeout: int = 30) -> str | None:
        """Run a git command synchronously. Returns stdout or None on failure."""
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode != 0:
                logger.debug(
                    f"git {' '.join(args)} failed (rc={result.returncode}): {result.stderr.strip()}"
                )
                return None
            return result.stdout
        except subprocess.TimeoutExpired:
            logger.warning(f"git {' '.join(args)} timed out after {timeout}s")
            return None
        except OSError as e:
            logger.warning(f"git {' '.join(args)} failed: {e}")
            return None
