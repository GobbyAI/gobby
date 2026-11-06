# Seat titles

Contract for the labels a seat shows: its pane, its runbook tab, and the
sidebar and roster labels derived for its session. It records Josh's
2026-10-25 rulings on task #25128. Stored session titles and manual names were
removed under task #25485. The test that pins this contract is
`tests/contracts/test_seat_titles_contract.py`.

## Josh's rulings

Recorded verbatim:

> Panes should be named after the Agent Definition. Titles shouldn't contain task info at all anymore.

> tab titles should be the name of the runbook (e.g. Dev Team - #task_ref)

> Unless they're static panes like Admin, Support, Etc.

> we don't have lanes anymore

## Rules

1. **Seat pane.** A seat launched from an agent definition takes that
   definition's display name as its pane title, exactly. The display name is
   the definition name with hyphens turned into spaces and each word
   title-cased: `dev-team-lead` shows as `Dev Team Lead`. The pane title carries
   no task reference, slug, role suffix, counter, or caller-supplied text.
2. **No stored titles or manual names.** A session has no stored title. The
   `sessions.title` and `sessions.title_source` columns are dropped by migration
   491, `/api/sessions` returns neither field, and no caller can set a manual
   name. Every display label is derived at read time from four inputs only: the
   session ref, the agent definition, the claimed task, and the status line. A
   claim, close, or clear changes the task line of a label; it never writes a
   stored name.
3. **Task line.** The task line names the claimed task and marks the seat that
   holds its claim. Other seats on the same task show the task without the mark.
   The roster marks the claim holder with `task.claim_holder`, and gclient
   renders that mark.
4. **No title arguments.** `spawn_agent`, `apply_agent_definition`,
   `apply_persona`, and `register_session` take no title argument. The
   `gobby-sessions:set_title` tool is removed.
5. **Runbook tab.** A runbook tab is `<Runbook> - #<task_ref>`, for example
   `Dev Team - #22604`. Only the tab carries the task reference. A runbook run
   with no task takes the bare runbook name, for example `Team Planning`. Seat
   panes inside a runbook take the definition names from rule 1.
6. **Static tabs.** Static tabs keep their fixed names, such as `Admin` and
   `Support`.
7. **No lanes.** The word "lane" is removed from seats, runbooks, policy, and
   launch surfaces. Spawn and resume take no lane argument, and placement is not
   keyed by lane. The bundled `refuse-task-outside-lane` rule is retired. Its
   helper and tests are gone with it.

## Enforcement

- Definition label: `src/gobby/sessions/display_labels.py`
  (`definition_display_name`), the single source of the definition label. The
  communications seat label and the spawn pane title derive from ref and
  definition through it. The tmux window fallback is the session ref alone
  (`_synthesize_fallback_title` in `src/gobby/sessions/tmux_window_naming.py`).
- Schema: `crates/gcore/assets/schema/migrations/491_drop_session_titles.sql`.
- Roster: `src/gobby/storage/attention.py` (`AttentionRosterRow.definition`,
  `task_claim_held`) and `src/gobby/servers/routes/attention_roster.py` (entry
  key `definition`, task key `claim_holder`).
- gclient: `crates/gclient/src/daemon/roster.rs` and
  `crates/gclient/src/app/sidebar_model.rs` derive the task line and the claim
  mark from the roster payload.
- Runbook placements: `src/gobby/install/shared/workflows/pipelines/`
  (`dev-team.yaml`, `plan-team.yaml`, `support.yaml`).
- Pinned by `tests/contracts/test_seat_titles_contract.py`,
  `tests/servers/test_attention_roster.py`, and
  `tests/storage/sessions/test_session_title_removed.py`.
