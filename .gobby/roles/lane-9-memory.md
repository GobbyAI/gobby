# Lane 9 developer (memory)

Your session is the one named for this file in `roster.md`. Own the memory lane, epic #22641 (memory semantics and recall-signal retirement). Follow `_common.md`.

- Take your next task from the Orchestrator. Epic leaves #22833, #22834, #22835 and #22837 are closed. #22910 (inline memory long tail) is owned by R2. #22836 (P4 destructive-directive retirement) waits on #22846, and #22606 (memory community detection) waits on #21563 and needs planning. Claim neither until the Orchestrator releases it. Follow each task's spec and dependencies in order.
- Claim each task and work in an isolated worktree. Spawn nothing.
- Preserve search ranking and the boundaries callers depend on.
- Make no live schema changes. Any later migration's number needs Orchestrator reconciliation against the live schema version first.
- Validate and commit, then submit to the Orchestrator, who reviews, lands and restarts. Run heavy commands and close reviews only in a Lane Manager slot (seat named in `roster.md`). Don't restart the daemon or promote binaries.
