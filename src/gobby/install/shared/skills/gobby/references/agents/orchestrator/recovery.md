# Orchestrator: recovery

Load before recovering a stalled, held or usage-limited seat, respawning after a
failed spawn, or restarting a seat.

- Recover stalled sessions, including resumes Triage relays from the Recovery Steward and Dev Team Leads. Check the pane with `gobby-sessions:capture_output` first. For an idle composer, send `gobby-sessions:send_keys` with literal text and a trailing newline, for example `continue: check your queued Gobby messages and proceed\n`; a bare Enter does not submit. Send resume keystrokes only, never steering. Preserve human drafts and approval boundaries. Never enter credentials or type into a password or credential pane; hand that to the Assistant. Report each recovery to the Assistant.
- Usage limits. A usage-limit dialog is not an idle composer: `send_keys` refuses it by design, so report it to the Assistant for the operator to dismiss. When the operator wants the seat back without dismissing the dialog, stop it with `gobby-agents:stop_agent(run_id=<full run UUID>)`, then `spawn_agent` with `resume_session_id=<its session>`, the same agent and model, `worktree_id` for a tree seat, and `claim=false`. The tree survives; the resumed seat has a new run ID, so read `team_progress` afterwards.
- Before a seat restarts, it runs `set_handoff` and consumes the handoff in its fresh context first. Wait for that before closing its window and resuming it.
- Failure handling.
  - A spawn that fails sandbox preflight can hold the task's dispatch lease until it expires (600 s). Read the lease before respawning; do not retry in a loop.
  - A spawned seat can be held by a pending handoff on its own session; only that session's main thread can consume it. Tell the seat's main thread to call `get_handoff`.
  - A `claim=false` reviewer in a developer's worktree can have the developer's dirty paths attributed to it; treat that as the developer's in-flight work, not a reviewer write.

_Version: 1.0.0_

_Last verified: 2026-11-05_
