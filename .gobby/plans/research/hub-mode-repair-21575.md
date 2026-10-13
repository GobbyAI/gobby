# #21575 repair context: HM-F14 to HM-F18 (2026-10-12)

Base commit 991a6afb96. Plan: `.gobby/plans/hub-mode.md`. Adversary gobby#16491
accepted all five dispositions in principle. Orchestrator gobby#15977 ruled on
HM-F15 and HM-F16. No consensus until the committed repair is verified.

## Rulings

- HM-F16 (Josh, button 8c60196a, verbatim): "Cap at 100, configurable. We
  already have a value for this in the config store." 15977 applied it:
  - The key is `max_active_agents` (`src/gobby/config/app.py::DaemonConfig`,
    code default 20, ge=1, "Maximum active agents per project"). The live config
    store sets 100 (revision 151). The code default stays 20.
  - It also applies per machine. In 2.5 the hub enforces it under a per-machine
    lock before `create_pending`: it counts the target machine's pending, live
    and orphaned terminal rows, and at the ceiling it refuses with stage "none"
    and creates no rows. No new key. The per-project `agent_slot_cap_refusal`
    is unchanged.
  - Every report list keeps a fixed protocol bound of 1,000 (Rust and Python
    models). `max_active_agents` gains `le=1000`, so a full node always fits
    one complete report. Exits drain in batches of that bound. State `le=1000`
    and its reason in decision text. Reason: lowering a live-tied bound while
    runs are live would invalidate complete reports.
  - The config contracts (`tests/contracts/http/config_schema.json`,
    `crates/gcore/assets/config/runtime_config_contract.json`) carry no bounds,
    so `le=1000` edits only `app.py`.
- HM-F15 (15977): use the epoch barrier, with no DB migration. The plan must
  say `hub_epoch` is a fresh random UUID minted per hub process and never
  reused.

## Applied in the working tree (uncommitted)

1.1 only:
- Route codes add `stale_report` (409, body `{code, hub_epoch}`).
- The report body adds `hub_epoch`, `revision`, and `workspace` on exits.
- Node duties:
  - durable preparation record and removal retry (HM-F18);
  - cancel removal under that rule;
  - one report at a time, fresh, with a durable revision and no resends;
  - adopt the epoch from a refusal;
  - exits oldest first, at most 1,000 per report, a launched exit names its
    workspace;
  - #24148 test cases listed.

Also done:
- 1.1: corpus, models, 1.1.1.
- 1.4: depends on 2.1; research; route steps admit, record, put; inbox and
  record_remote_workspaces text; 1.4.3, 1.4.5 and 1.4.6.
- 2.6: workspace insertion removed; 2.6.2 deleted and 2.6.3-8 renumbered to
  2.6.2-7; Granularity; consecutive-report wording; research trimmed.

Also done: 2.2, 2.5, Constraints, Decisions 15, 16, 18, and the new 19, D1,
and V1. Validation passes, with one warning: the validator lists every
`DaemonConfig` consumer, and 2.5's research records the bounded reason.
Additions beyond the list below: the node refuses a spawn at 1,000 accepted
and live runs, because a remote session end exits the hub's terminal row
before the node's process exits. 2.2 also guards the web-chat lookup,
`resolve_session_start_identity`, and `_recover_compact_session`, and states
the test pin rule.

Adversary 16491 asks the HM-F16 text to name the key and its bounds. It also
asks that the text show reports stay complete when the value is configured
above 1,000 or lowered below the live count.
- `le=1000` refuses a value above 1,000.
- Lowering the value gates only new admissions. Runs already live were
  admitted under an earlier value of at most 1,000, so a report never exceeds
  1,000.

## Original edit list (1.1, 1.4 and 2.6 items are done)

- 1.1:
  - Corpus: add a `stale_report` refusal body and exits with a workspace.
  - Models: the refusal body type.
  - 1.1.1 text: name the ordering rule and the preparation record.
- 1.4:
  - Heading `(depends: 1.1, 2.1)`, so node rows never reach hub loops before
    2.1.5.
  - Research: the unique indexes `idx_worktrees_path` (machine_id,
    worktree_path), `idx_clones_path` (machine_id, clone_path) and
    `idx_worktrees_branch` (project_id, branch_name, machine_id). Move the
    workspace-row facts here from 2.6 (columns, `agent_session_id` FK
    `ON DELETE SET NULL`, no task `worktree_path` artifact, `list_worktrees`
    machine scoping).
  - Implementation, report route order:
    1. identity checks, then parse;
    2. under the inbox lock, refuse 409 `stale_report` when the epoch is not
       current or the revision is at or below the machine's high-water, else
       reserve the high-water;
    3. one DB transaction records the workspace rows from `agents[].workspace`
       and `exits[].workspace`. An entry counts only when its `terminal_id` row
       is on the key's machine and names `run_id`. Each row is an `active`
       worktrees or clones row with a new id, the terminal's project and
       machine, the run's task and child session, path, branch and base,
       `ON CONFLICT DO NOTHING`;
    4. commit, then `put`, then `{}`.
  - `put` keeps the highest-revision snapshot and merges exits.
  - Update the in-memory note: workspace rows are durable before 200, and lost
    exits fall to absence.
  - Acceptance: update 1.4.3. Add 1.4.5 (ordering: out of order, duplicate,
    null or old epoch after a restarted inbox, out-of-order completion keeps
    the newer snapshot, refused before any write) and 1.4.6 (workspace rows
    from a live entry and a launched exit before 200, idempotent,
    foreign-terminal or run mismatch skipped, a row survives an inbox reset,
    no task artifact, `list_worktrees()` excludes it, two reports of 1,000
    and 1 exits both reach `take`).
- 2.2 (HM-F17):
  - A key-authenticated hook with no `input_data.machine_id` also gets 403
    `hook_machine_mismatch`.
  - New file `src/gobby/hooks/session_binding.py` with one predicate: a row
    binds when the event's machine is empty or the hub's own
    (`get_machine_id()`), or equals the row's `machine_id` (reuse
    `gobby.sessions.machine_scope.is_local_machine_owner`).
  - A foreign row counts as a miss in:
    - `flow.py::handle_session_start`, at both `get` lookups, before the
      external_id update at line 221;
    - `session_lookup.py::SessionLookupService._resolve_metadata_platform_session`
      (header, start and non-start);
    - `SessionLookupService.resolve` (external-id result, before mutations and
      enrichment);
    - `project_context.py`: `_project_id_from_session`, `_cached_session` and
      `_project_id_from_existing_session`. The explicit-project and cwd paths
      are unchanged.
  - A remote miss reaches `register_session`, and `require_local_machine_id`
    refuses it.
  - Facts:
    - `agy.py` and `grok.py` do not call `_copy_platform_session_metadata`,
      so a metadata flag would fail open.
    - `sessions.machine_id` is NOT NULL.
    - `tests/hooks/test_session_start_handlers.py:833` and `:905` use a
      pre-created row on "unknown-machine" with a local event, which stays
      unchanged.
  - Acceptance adds a foreign-row refusal for: terminal-context id,
    external_id-as-id, header on start and non-start, non-start external id,
    and a project from a foreign row with no explicit project. It also covers
    the same-machine remote pre-created path.
  - Update Constraints lines 248-252 and Decision 16.
- 2.5: the per-machine ceiling (see the rulings), plus `src/gobby/config/app.py::DaemonConfig` `le=1000`.
- 2.6: drop the workspace insertion (it moved to 1.4); delete 2.6.2 and renumber 2.6.3-8 to 2.6.2-7; update Granularity. "Consecutive reports" means passes that each take a newly accepted report.
- Decision 15: the 1.4 route inserts the row before 200, from a live entry or a launched exit. Decision 18: the report bullet. Add le=1000 decision text.
- D1 text: the row comes from 1.4.
- V1: a dated entry for HM-F14 to HM-F18 and the rulings.
- Then validate, run ocr, commit, and send the SHA to 16491.
