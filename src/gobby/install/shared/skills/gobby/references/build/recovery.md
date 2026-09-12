# Cancellation and recovery

Load before stopping, resuming, cleaning, or restarting automation. First inspect
build status, dispatch explanation, history, stages, active owners, and workspace
health. Fetch each `gobby-tasks-ops` control schema; target `input_ref` and project
scope determine the blast radius.

- `build_stop` without a target pauses future project dispatcher ticks. With a
  target it disables the task/subtree, cancels its active agents, gives up parked
  daemon-stop runs, clears dispatch mutexes, releases stale agent claims, and
  resets stoppable stage work. It preserves task history/build artifacts. It is
  not a harmless pause of future work only.
- `build_resume` without a target re-enables project ticks. With a target it
  enables the tree, clears stale leases/agent claims, resumes project automation
  if needed, and kicks dispatch. It does not apply a new profile.
- `build_clean` requires a task target. Preview with `dry_run=true`; inspect
  affected tasks, agents, artifacts, blockers, and retained dirty workspaces.
  Actual cleanup requires `yes=true`. `force` changes blocker/cancellation and
  cleanup behavior; it does not grant permission to destroy user work.
  `delete_dirty_worktrees` explicitly permits dirty descendant worktree deletion.
  Preserve such work by default. Cleanup errors leave recovery unfinished.
- `build_restart` requires a target and confirmation for actual execution. It
  stops, cleans, reconstructs eligible manifests, resets restartable failures/
  escalations, then resumes unless `no_resume=true`. Preview first. The MCP tool
  exposes recovery controls, not every CLI/HTTP restart option; do not invent
  extra parameters. Read the returned manifest and deferred artifacts.

Prefer task-scoped recovery for one build. Do not repair ownership/stages with
SQL, direct storage, or HTTP lifecycle calls. Keep related workspace ID/path
artifacts atomic and use supported source-control cleanup. Clean/restart is
destructive recovery, not a shortcut around review, failed evidence, or a claim.
Control previews may record best-effort history; do not promise zero database
writes for every `dry_run` surface.

After a code fix affecting dispatch, spawn, controls, stages, handoff, isolation,
or startup, keep affected builds blocked until the running daemon has the fix.
Record stale agents and workspace metadata; coordinate stopping them as needed.
Notify active sessions through `gobby-agents:send_message`, obtain a quiet
restart window, restart from the main checkout, verify health, save structured
handoff, and inspect the next eligible spawn's workspace/isolation metadata.
Use admin guidance for daemon lifecycle. A commit alone does not update runtime
code. Keep stale-behavior defects open until the affected path is verified.

Guides: [Build controls](../../../../../../../../docs/guides/cli-commands.md#build-automation)
and [Isolation](../../../../../../../../docs/guides/dispatch.md#isolation).

_Last verified: 2026-09-12_
