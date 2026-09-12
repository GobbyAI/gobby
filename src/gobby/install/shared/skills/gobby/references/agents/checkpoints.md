# Preserve a terminal child's worktree

Load before recovering uncommitted work from a finished or failed child.
Inspect `get_agent_result`, task ownership, worktree registration, and active runs.
`gobby-agents:checkpoint_agent_worktree` is an original-parent coordinator operation
on a terminal child's isolated task worktree; it is not a live-worker snapshot.

Terminate active writing through the supported lifecycle first. Check that no
active task/worktree run remains, the task is open, and the registered checkout
matches its linked branch on the same machine. Call the checkpoint tool with the
run ID only after confirming the intended recovery boundary.

The operation rejects foreign owners, foreign-attributed and unattributed paths,
invalid isolation, and active runs. It checkpoints the authorized dirty set and
returns commit/path evidence while preserving task ownership; it does not close
or release the task. A clean checkpoint may have no new commit. Inspect the
returned included paths and commit before reusing the worktree.

On ownership or attribution failure, coordinate with the owner rather than
rewriting records. A release failure can return a commit even with success=false;
inspect that commit and claim state before retrying so recovery does not duplicate
or lose work. Checkpoint commits are recovery evidence, not completed validation.

Guide: [Recovery checkpoints](../../../../../../../../docs/guides/agents.md#recovery-checkpoints).

_Last verified: 2026-09-12_
