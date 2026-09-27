# Lane 9 developer (memory)

Your session is the one named for this file in `roster.md`. Own the memory lane, epic #22641 (memory semantics and recall-signal retirement). Follow `_common.md`.

- First ready leaf: #22837, the combined 1.1 + 1.2 item from the approved expansion 854b12e3 (at 44bcd14). Follow each task's spec and dependencies in order.
- Claim each task and work in an isolated worktree. Spawn nothing.
- Preserve search ranking and the boundaries callers depend on.
- Make no live schema changes. Any later migration's number needs PD reconciliation first: the plan says 452, but the live schema is already 453.
- Validate and commit, then submit to the PD, who reviews, lands and restarts. Run heavy commands and close reviews only in a Lane Manager (gobby#14556) slot. Don't restart the daemon or promote binaries.
