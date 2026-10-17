# Hub mode #21575: Orchestrator review O1 to O4 (working context)

Session gobby#16490. Orchestrator gobby#15977's review of the stamped plan at
fb13ada49f returned O1 to O4. M1 withdrawn at 2d19349 (diff check clean: removed
exactly the 940 M1 lines, added one changelog line). The repair is committed:
166f69d (O1 to O4) and 30f4656 (guarded generation read; exit decides a row
before any listing). Plan SHA-256 31e9eb2ca908665fd1ba9c6fd09ade8f5a8896de8944c0e1ebfb3de2c08da842.
Sent to 16491 (re-stamp), 15977, and 16011 (O3 only). 16011 LANDed O3;
16491 cleared; consensus 44fd4bf; stamp b26b89a (rendered SHA-256 8123a694,
23 leaves, 144 labels) sent to 16491 for verification and to 15977.

Pending mirror (only if 1.4 is revised anyway; 16767 asked for no re-stamp):
node-halves 712f75279a (NH-F15) adds a second insertion into 1.4's report
route. node-halves 3.9 calls `apply_sandbox_records` after step 2
(`record_remote_workspaces` commits) and before step 3 (`put`), answering 500
without `put` on failure, and redacts sandbox events right after 3.3's pane
redaction, before step 1. 1.4's "node-halves 3.3 later inserts one step before
step 1" is therefore incomplete. Edges unchanged (3.9 follows 1.4 through 3.3).

## Done in the working tree (2.6)

- Targets gained `src/gobby/hooks/session_coordinator.py::SessionCoordinator.complete_agent_run`.
- Granularity rewritten for nine acceptance items and three production files
  (2.6.8 grok or agy exit, 2.6.9 node run completion).
- Research context: the "normal exit already settles" bullet now explains why
  `complete_agent_run` fails every node run (counts come only from transcripts,
  which stay on the node), the hub-side turn lifecycle generation, the exit
  fields, why `_dead_terminal_reason` fails grok and agy, and two rejected
  alternatives (pane `idle.turn_in_flight`; skipping the no-activity check for
  every node run).

## O1 design still to write

Correction to the 2.6 research and Targets: the remote check lives in the
callers, not in `complete_agent_run` (SessionCoordinator has no machine id, and
`tests/hooks/test_session_coordinator.py` builds runs as MagicMocks). So:

- Replace the 2.6 Target `session_coordinator.py::SessionCoordinator.complete_agent_run`
  plus add `src/gobby/hooks/event_handlers/_session_end.py::handle_session_end`
  (check exact qualified name with gcode; it is a mixin method on EventHandlers).
  That makes four production files; update the Granularity count.
- `SessionCoordinator.complete_agent_run(session, *, attested_turns: int = 0)`:
  after the counts resolve, `turns_used = max(turns_used, attested_turns)`.
  Default 0 keeps every existing caller and test unchanged.
- `handle_session_end`: for a session whose `machine_id` is not
  `self._get_machine_id()`, call
  `complete_agent_run(session, attested_turns=self._turn_lifecycle.get(session.id).generation)`
  (0 when `_turn_lifecycle` is None); a local session keeps `complete_agent_run(session)`.
  Check `tests/hooks/test_session_end_handlers.py` around line 246 (the
  `foreign` identity case) for a `complete_agent_run.assert_called...` that the
  new keyword would break; if so, make that file a target.
- Reconciler: `RemoteRunReconciler` also takes `get_session_coordinator` and
  `get_session_manager` (AgentHealthMonitor's `_get_session_coordinator` and
  `_get_session_manager`), so agent_health.py changes only in `__init__`.
- `NODE_HOOKLESS_PROVIDERS = frozenset({"grok", "agy"})` in
  `remote_reconcile.py` (Decision 7). A grok or agy row with a `launched: true`
  exit whose `reason` is `exited` settles at once, before any `dead_reason`:
  terminal exits (`mark_exited` live or orphaned, `fail_pending` pending), run
  re-read, ended gets nothing more; active:
  - exit_code 0: read the child session, then in a thread
    `complete_agent_run(session, attested_turns=1)` (prompt on argv, so the
    launched one-shot ran one turn). Getter None or missing session: nothing,
    the run sweep fails it later.
  - other code: `cleanup_agent(run, terminal_payload="remote <provider> exited with code <n> on <machine>")`.
  - null code: `cleanup_agent(..., "remote <provider> terminal left the node's inventory without an exit code on <machine>")`.
  - `reason: cancelled` counts as absent for every provider (the cancel owner,
    2.4's terminate path, settles the run; the two-report wait gives it time).
  - A run complete_agent_run leaves active keeps its exited terminal, so the
    run sweep settles it (same outcome as the local `_unsettled_completions` retry).
- Edit the "Every other row is absent" bullet: only a claude, codex, or droid
  row's `launched: true` exit counts as absent.
- Consumers unchanged: `_session_end.py` callers? (if it is a target, not here);
  `src/gobby/agents/agent_health.py` `_dead_terminal_reason` keeps its call;
  tests `tests/hooks/test_session_coordinator.py`,
  `tests/agents/test_lifecycle_task_completion.py`,
  `tests/agents/watchdog/test_print_mode_exit.py`,
  `tests/hooks/test_clear_successor_seat.py`, and
  `tests/hooks/test_session_end_handlers.py` if not a target. Add them to the
  verification command.
- Acceptance: 2.6.3 says the `launched: true` wait applies to claude, codex,
  and droid. New 2.6.8 (`test_hookless_exit_settles_from_exit_code`): grok or
  agy launched with its prompt and exits: 0 completes through
  `complete_agent_run` with one attested turn and its terminal exits; nonzero
  and null fail with the code; `cancelled` waits; an ended run gets nothing.
  New 2.6.9 (`test_node_session_end_counts_forwarded_turns`): a node session
  end whose forwarded prompt-submit began a turn completes; one with no begun
  turn fails "no activity"; a closed task completes regardless; a local session
  end is unchanged. Both in `tests/hub/test_remote_reconcile.py`.
- Constraints "Hooks from node agents": "Session end settles the run as it
  does locally" becomes: session end completes the run, counting its forwarded
  turns (2.6), because the hub holds no node transcript; and "2.6 settles a run
  whose agent left no session end, and a grok or agy run from its exit".
- 5.1: fake for claude, codex, droid also pipes its prompt-submit
  (`BEFORE_AGENT`) envelope after session start; every fake exits 0 after its
  last turn. 5.1.2: completed through the forwarded session end, whose
  prompt-submit began a turn, for claude, codex, droid; through the exit report
  with exit code 0 for grok and agy (2.6). Research bullet at "Grok and agy
  runs settle through the node's exit report (2.6)" names 2.6.8.

## O2

Manifest schema has no review label (contract `labels`: covers labels only;
`coverage.py:544/608`, `manifest_parser.py:328`). Replace Decision 18's line
"The implementation of 1.4 and 2.5 needs a security review." with a pointer to
a new Constraints bullet **Security landing gate**: every candidate commit for
1.2, 1.4, 1.5, 2.2, 2.3, 2.4, 2.5 and 4.2 needs a Security Reviewer LAND before
it lands; the Orchestrator applies it at dispatch and landing because the
manifest carries no review label. Add one line before `Targets:` in each of the
eight sections: "Landing gate: a Security Reviewer LAND on the candidate commit
before it lands (Constraints)."

## O3

Every seeded test machine, the hub's included, is owned by `TEST_USER_ID`
(`tests/fixtures/postgres.py::postgres_canonical_seed`). Add to 1.5 a helper in
`tests/servers/conftest.py`: `authenticate_as(server, user_id)` sets
`authenticate` to `AuthDecision(allowed=True, user_id=user_id)` (break_glass
False) and `is_request_authenticated` True, plus `OTHER_USER_ID` (owns no
machine). Change 1.5's two exact conftest Targets to `tests/servers/conftest.py::*`
with a scope-reason (the two stubs change and `authenticate_as` joins them;
mixed exact/wildcard is invalid). State in 1.5 that no ownership acceptance runs
under the break-glass default. Name `authenticate_as` with `TEST_USER_ID` and
`OTHER_USER_ID` in 1.5.4, 2.5.16 (HTTP part), 3.3.6, 4.4.4, 4.7.5; 1.5.5 uses
real credentials with no stub; 1.5.6 explicit decisions; hub tests 2.3.6,
2.4.6, 2.5.16 (MCP part), 4.3.7 bind with `bind_caller` directly.

## O4

1.1: add "Its manifest entry carries `tdd: false`: no test-first work remains."
Tell 16491 (it planned tdd true for P1 to P4).

## Finish

Dated V1 repair entry (O1 to O4), validate bare, ocr rule, commit, send SHA to
16491 (with O4 tdd and O2 no-label notes) and 15977; Security 16011 re-reviews
O3 only. Consensus entry only after 16491 clears.
