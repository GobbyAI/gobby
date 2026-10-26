# Seat titles

Contract for the titles a seat launch gives its pane, its runbook tab and its
session. It records Josh's 2026-10-25 rulings on task #25128. The test that
pins it is `tests/contracts/test_seat_titles_contract.py`.

## Josh's rulings

Recorded verbatim:

> Panes should be named after the Agent Definition. Titles shouldn't contain task info at all anymore.

> tab titles should be the name of the runbook (e.g. Dev Team - #task_ref)

> Unless they're static panes like Admin, Support, Etc.

> we don't have lanes anymore

## Rules

1. **Seat pane and session title.** A seat launched from an agent definition
   takes that definition's display name as both its pane title and its session
   title, exactly. The display name is the definition name with hyphens turned
   into spaces and each word title-cased: `dev-manager` shows as `Dev Manager`.
   Such a title carries no task reference, slug, role suffix, counter, or
   caller-supplied text.
2. **Session title precedence.** A manual title beats the definition title. The
   definition title beats an open claimed task's title and the provisional
   title. A task claim, close, or clear never writes a task title over a defined
   seat's definition title.
3. **Spawn title argument.** `spawn_agent`'s `title` is ignored for a seat that
   has an agent definition. It applies only to an agent without a definition.
4. **Runbook tab.** A runbook tab is `<Runbook> - #<task_ref>`, for example
   `Dev Team - #22604`. Only the tab carries the task reference. A runbook run
   with no task takes the bare runbook name, for example `Team Planning`. Seat
   panes inside a runbook take the definition names from rule 1.
5. **Static tabs.** Static tabs keep their fixed names, such as `Admin` and
   `Support`.
6. **No lanes.** The word "lane" is removed from seats, runbooks, policy, and
   launch surfaces. Spawn and resume take no lane argument, and placement is not
   keyed by lane. The bundled `refuse-task-outside-lane` rule is retired. Its
   helper and tests are gone with it.

## Enforcement

- Title precedence: `src/gobby/sessions/title_lifecycle.py`, with
  `definition_display_name` as the single source of the seat title.
- Launch: `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py` sets the
  definition title from the agent name.
- Runbook placements: `src/gobby/install/shared/workflows/pipelines/`
  (`devteam.yaml`, `team-planning.yaml`, `support.yaml`).
- Pinned by `tests/sessions/test_title_lifecycle.py`,
  `tests/agents/test_runbook_seat_titles.py`,
  `tests/mcp_proxy/tools/spawn_agent/test_seat_titles.py`, and
  `tests/contracts/test_seat_titles_contract.py`.
