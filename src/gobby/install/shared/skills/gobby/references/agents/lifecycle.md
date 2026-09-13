# Inspect, wait for, and end runs

Load when monitoring execution, diagnosing missing completion, cancelling helpers,
or finishing a spawned worker. Use `list_agent_runs`, `list_running_agents`,
`get_running_agent`, and `running_agent_stats` for discovery. Preserve run IDs;
list filters/limits and displayed live state are not proof of task completion.

Read `get_agent_result` for durable results, `get_agent_capture` for saved capture,
and `get_agent_live_output` for current terminal evidence. Capture and result have
different provenance; missing live output does not prove a run is finished.
When a terminal result includes capture metadata, retrieve `get_agent_capture`
and consume every page before judging the result. Follow [oversized-result
retrieval](../mcp-servers/results.md) when the proxy offloads a response.
`wait_for_agent` follows daemon-resume chains and registers a durable completion
subscription for live work. Call it once and yield. `wait_for_output` is a bounded
run-terminal regex diagnostic with internal polling, not a durable completion
subscription; it cannot replace waiting for successful work.

A spawned worker finishes its task/stage obligations first, then calls
`end_agent_run` with current_state and concrete next_steps plus supporting handoff
fields. It resolves the current trusted child boundary and persists the handoff
before completion. The parent reads it with `gobby-sessions:get_handoff` using
`agent_run_id`. Ending a chat turn does not end a run; ending a run does not close
the task. Blocked workers follow their installed step transition and include the
blocker in the final handoff.

Use `stop_agent` to cancel an intended pending/running run. `kill_agent` supports
explicit process termination/cleanup; status=success is restricted to self and is
not the normal authored-handoff path. Inspect errors and retained work before
respawning. `unregister_agent` reconciles/cancels registry state and terminal
delivery; it is not permission to abandon a live process. `cancel_stale_helpers`
cancels all pending/running runs matching parent and definition, without an age
threshold. Confirm that whole target set before invoking it and inspect per-run
errors even when the envelope reports success.

Operators use CLI status/stop/kill/stats/cleanup and HTTP run cancellation/cleanup.
CLI cleanup offers `--dry-run`; inspect its age criteria before applying it.
Unexpected termination warrants capture, task evidence, and checkpoint inspection,
not direct database status repair.

Guide: [Lifecycle](../../../../../../../../docs/guides/agents.md#lifecycle-model).

_Last verified: 2026-09-12_
