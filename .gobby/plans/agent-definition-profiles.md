# Agent Definition Profiles: YAML Seats With Rules, Skills, And Step Workflows

Plan artifact: `.gobby/plans/agent-definition-profiles.md`

**Plan ID:** agent-definition-profiles

## Overview
`kind: framing`

Task #22902 (Plan YAML agent definitions with rules, skills, and step workflows)
under epic #22691 (Agent definitions and deploy_runbook: incremental planning and
delivery). Josh's standing sessions (Assistant, Program Director, lanes, reviewer,
researcher, archivist, monitor, planning council) run today on a prompt-only
workaround: `default.yaml` tells every session to read `.gobby/roles/_common.md`,
`roster.md`, and one role Markdown file. No rule, skill, tool restriction, or step
workflow is attached to a seat; nothing validates the role text; a new seat is a
Markdown file and a roster row.

This plan turns each seat into a bundled, sync-managed agent definition that
declares its prompt, rules, skills, tool restrictions, and (where the seat has a
fixed loop) a step workflow, and it migrates the role files onto those
definitions without breaking the open #22894 contract. It also settles the
definition schema questions the siblings depend on: the dead `skills` map, the
dropped `version` key, the seat naming rule, and where shared seat text lives.

Josh's execution-model ruling governs the target design: every session is an
interactive agent in a pane except one-shots, and every agent must be able to
execute a step workflow. Current gaps in activation are recorded as facts, not
non-goals.

## Decision Record
`kind: framing`

1. **Ownership: bundled global definitions.** Seat YAML lives under
   `src/gobby/install/shared/workflows/agents/` and syncs through
   `gobby.agents.sync.sync_bundled_agents` as global rows (`project_id IS NULL`,
   `source="installed"`, tag `gobby`). Reasons: bundled sync is the only path that
   runs at dev-mode daemon start, `gobby install`, `gobby sync`, and
   `gobby-workflows:reload_cache`; it owns drift refresh, orphan tombstones, and
   `has_template_update`; and #22899 Decision 14 honors `sandbox_profile` only on
   `_is_sync_managed_bundled_agent` rows and its verification expects a bundled
   definition from this plan. Rejected: project-scoped
   `.gobby/workflows/agents/*.yaml`. `sync_imported_workflows` runs only from
   `reload_cache`, never at startup; its `_upsert_agent` writes new rows as
   `source="installed"`, not `project`; exported YAML omits the `type: agent` key
   that import requires. Those defects are #22906's (deferred below). Josh's
   operating facts (quiet hours, Telegram routing, digest path) ride in bundled
   prompt text as input of record per #22691.
2. **The workspace panes are the catalogue, and seats take the noun.** Josh's
   ruling (this session): what is up in the Gobby workspace panes now is the
   runbook, or its model. The catalogue is therefore the fourteen roster rows,
   which resolve to ten definitions: `assistant`, `program-director`,
   `lane-manager`, `developer` (the five lane panes), `code-reviewer`,
   `researcher`, `archivist`, `log-monitor`, `plan-writer`, `plan-adversary`.
   Every seat carries tag `seat`. There is no analyst seat: no pane runs one.
   Where a stage one-shot owns the noun today (`researcher.yaml`,
   `plan-adversary.yaml`), the seat body replaces the one-shot body under the
   same name. Josh's standing ruling (memory 6102cd1d, 2026-09-24): `gobby
   build` and its stages are being retired, compatibility with them is not a
   design constraint, and old definitions are recoverable from git history. No
   `-seat` suffix, no renamed copy, no `deprecated/` copy. `planner`,
   `plan-enhancer`, `analyst`, `architect`, `product-manager`, and the two
   `-taskless` reviewers are untouched: nothing in the catalogue needs their
   nouns, and their removal is sequenced with build retirement.
   Lanes: Josh's second ruling (this session) is one developer profile that
   loads the skills the assigned task needs and is told which lane it occupies.
   `developer` is that definition. `backend-developer`, `frontend-developer`,
   and `fullstack-developer` are deleted (their bodies differ only in toolchain
   prose that 1.1 removes, one-line descriptions, and one claim-step label) and
   every routing constant that names them is retargeted to `developer` (3.2).
   Lane identity comes from what already exists: the roster row resolves to a
   lane role file that names the lane and its epic, and the PD and Lane Manager
   address work to the lane by name. Rejected: keeping the three as build-only
   one-shots beside `developer`; that preserves definitions only because build
   uses them, which the ruling forbids.
3. **`skills` map retired.** `AgentDefinitionBody.skills` has no reader in
   `src/gobby`; ten bundled YAMLs fill it with prose. Skill attachment has two
   live slots: `workflows.skill_selectors` (discovery allowlist, read by
   `resolve_skills_for_agent` and `apply_persona`) and
   `step_workflow.variables.required_skills` (must-load set, validated by
   `dispatch.skill_composition.inspect_skill_composition`). The key is rejected
   like `role`/`goal` and removed from the ten files. A bundled contract test
   resolves every seat's `required_skills` against bundled skills.
4. **`version` stored.** `version: StrictStr | None` joins the body inside
   `definition_json`; it was silently dropped by `extra="ignore"`. No new column:
   a column would trigger the schema-catalog carrier set. Pinning identity for
   #22903 is `storage.definitions._shared.compute_definition_hash`. A bundled
   contract test (3.5) is the validation gate for seats; it runs last in P3 so
   it sees the full catalogue.
5. **Shared seat text is a rule, not duplicated prompt.** `_common.md` and
   Josh's standing instructions become one `inject_context` rule in a new
   `roles` rule group, injected once per context epoch. Prompts are not templated
   (`prompt_for` returns raw text), so a rule is the only dedup mechanism.
6. **Spawn policy is enforced by rules.** All seats: block
   `gobby-agents:spawn_agent` and `gobby-agents:dispatch_batch`. Plan Writer
   exception: `agent == "plan-enhancer-taskless"`, `isolation == "none"`, at most
   one pass per claimed planning task. `_common.md` names the exception. This is
   the explicit spawn authorization Josh's proposed flow needs; until it lands,
   no seat spawns.
7. **Step workflows authored for seats with a fixed loop:** `developer`,
   `plan-writer`, `plan-adversary`, `code-reviewer`, `log-monitor`.
   Message-driven seats (assistant, program-director, lane-manager, archivist,
   researcher) carry no `step_workflow`; a single "serve" step adds nothing.
   Interactive execution of step instances is #22903's.
8. **Tool restrictions are declared on the definition** (`blocked_tools`,
   `blocked_mcp_tools`). As-is they apply to spawned sessions only; #22903 applies
   them at activation. Read-only seats use the runbooks list:
   `Edit`, `KillShell`, `MultiEdit`, `NotebookEdit`, `Write`, `apply_patch`,
   `edit_file`, `notebook_edit`, `replace`, `write_file`.
9. **No backend field.** Definitions never name a terminal backend. Workspace
   panes resolve native gterm; the only tmux fallback is `spawn_agent`'s
   `terminal_backend`, owned by #21565.
10. **Continuity is prose, not schema.** Compact-never-clear: assistant,
    program-director, lane-manager, log-monitor, archivist.
    Clear-between-deliverables: developer, researcher, code-reviewer,
    plan-writer, plan-adversary. No `continuity` field: nothing reads it.
11. **Migration is additive under open #22894.** `roster.md` format is pinned by
    `tests/workflows/test_default_agent_role_contract.py::_probe_documented_lookup`
    (regex `| <file>.md | gobby#N |`), and `default.yaml` must keep its lookup
    phrases. Each role file becomes a pointer: `apply_persona(agent=<seat>)`.
    Roster and `default.yaml` are unchanged.
12. **Seats inherit provider, model, and isolation.** `provider: inherit`, no
    `model`, `isolation: inherit`, `timeout: 0`, `surfaces: [spawn, persona]`
    with one prompt text under a YAML anchor used by both blocks. A pane gets
    its provider from Josh's launch line; a spawned seat gets the caller's.
13. **Rollout without restart.** After merge the PD runs
    `gobby-workflows:reload_cache` from the main checkout; rule rows and agent rows
    are re-synced and the engine picks rule rows up without restart. Restart is
    the announced fallback.
14. **Writer/Enhancer/Adversary flow.** Encoded in `plan-writer.yaml` and
    `plan-adversary.yaml`: Writer drafts and validates; one bounded
    enhancer pass (Decision 6); PD evaluates enhancement choices for Josh; Writer
    revises; the static-pane Adversary reviews; Writer and Adversary resolve by
    `send_message`. The Adversary owns the manifest content: it captures the
    round with `gobby-plans:prepare_plan_review_round` (interactive session
    binding), reviews the immutable snapshot, derives the entries with
    `derive_plan_review_manifest(evidence_id, routing_decisions)`, runs
    `validate_plan_review_coverage`, finalizes with
    `finalize_plan_review_evidence`, and on consensus sends the PD the approval
    payload: routing decisions, server-derived manifest entries, manifest
    digest, coverage attestation. The PD writes M1 with the coordinator apply
    tool. `apply_plan_review_manifest` requires a `run_id` bound by
    `bind_evidence_run`, which only a spawned reviewer run has, so for a seat
    review the PD uses `derive_plan_handoff_manifest` then
    `apply_plan_handoff_manifest` with the Adversary's routing decisions and the
    returned hashes. Nobody hand-edits M1 (coverage contract, Manifest-on-Approval).
    Expansion waits for PD confirmation of Josh's approval.

## As-Is Facts
`kind: framing`

Current code, recorded so executors and reviewers separate them from the target:

- `apply_persona_impl` persists `_persona_name`, `_active_skill_names`,
  `_skill_format`, and the reinjection flags. It does not set `_agent_type`,
  merge `workflows.variables`, change active rules, apply tool blocks, or create a
  step instance. `hooks/event_handlers/_agent.py::_inject_agent_instructions_if_needed`
  prepends `prompts.persona` on the next before-agent event.
- Rule scoping: `RuleEngine._filter_by_agent_scope` reads `_agent_type` only;
  `_filter_by_active_rules` uses the definition's selectors when `_agent_type`
  resolves, else `_active_rule_names`. Persona sessions therefore keep the
  `default` agent's rule set. Rules can read `_persona_name` in `when:`.
- Step instances are created only by spawn paths
  (`session_activation.py::_ensure_step_instance` requires `is_spawned_agent` or
  an agent run); enforcement (`_get_step_for_session`) has no spawned check.
- Compaction keeps `_persona_name` and re-prepends the persona; `/clear`
  successors carry only claim state and drop `_persona_name`.
- `spawn_agent` seeds `_agent_type`, `_agent_rules` (no reader), and
  `workflows.variables`; `_agent_blocked_tools`/`_agent_blocked_mcp_tools` are
  enforced by `enforcement_checks.py::_check_agent_tool_enforcement`.
- Bundled sync validates leniently (`extra="ignore"`); imported YAML validates
  with `extra="forbid"` and requires `type: agent`.
- `.gobby/roles/` is read by prompt text only; no Python reads it.
- `gobby build` still resolves `research` to `researcher`, `planning` review to
  `plan-adversary`, and `development` to `backend-developer`
  (`registry/stages.yaml`, `dispatch/_rule_state.py`). After this plan those
  names resolve to seat bodies whose loops do not terminate, or to
  `developer`. Build stages that spawn them degrade; accepted under ruling
  6102cd1d, and not repaired here.

## Constraints
`kind: framing`

- No code in this planning task. Leaves below are the implementation.
- Keep `plan-adversary-taskless` and `plan-enhancer-taskless`;
  `prepare_plan_review_round` spawns them.
- Every seat validates against `AgentDefinitionBody`; every declared surface has
  a non-empty prompt block; `skills:` is absent; no key outside the model.
- Every `#NNNNN` in seat prompts that Josh reads carries a title. Seats say
  "Systems nominal" when healthy and give load numbers only on a breach.
- Quiet hours 04:45–06:45 CT: no restarts or cutovers; Game Goblins jobs never
  rerun. Only the PD, or someone Josh names, restarts the daemon, with global
  notices before and after; the Assistant alerts Josh both times.
- Decisions reach Josh as clickable links or buttons sent by the Assistant; ask
  once, never nag. Conditional sign-off: the "reply in the terminal" line applies
  only while Josh is at the desk.
- Production `.py` files stay under 1,000 lines; no targeted file is at the
  850-line trigger (`agent_models.py` 219, `sync.py` 299,
  `skill_composition.py` 133).
- Boundaries: #22902 owns what a definition declares and how the bundle is
  validated; #22903 owns how a session receives, rehydrates, and enforces it
  (including the `is_spawned` step gate and persona rule scoping); #22899 owns
  `sandbox_profile` and its guards; #22904 owns placed launch; #22895 owns
  runbook deployment. #22906 owns the persistence defects (deferred below).
  #22908 (Lane 3, filed by the PD) corrects the phantom `gobby:log-monitor-tick`
  reference in the live `monitor.md`; 3.3 writes the replacement prompt and
  4.1's pointer edit lands after or over it.
- Build compatibility is not a constraint (ruling 6102cd1d). The build path is
  left installed and is not deleted here; a leaf that touches
  `src/gobby/dispatch/` reads `src/gobby/dispatch/AGENTS.md` first.

## P1: Schema
`kind: framing`

**Goal:** the definition schema stops lying about skills and version.

### 1.1 Retire the skills map and store version [category: code]
`kind: deliverable`

Targets:
- `src/gobby/workflows/agent_models.py::*` — scope-reason: add `version`, reject `skills`, update the class docstring
- `src/gobby/install/shared/workflows/agents/analyst.yaml::*` — scope-reason: remove the retired top-level skills map
- `src/gobby/install/shared/workflows/agents/architect.yaml::*` — scope-reason: remove the retired top-level skills map
- `src/gobby/install/shared/workflows/agents/backend-developer.yaml::*` — scope-reason: remove the retired top-level skills map
- `src/gobby/install/shared/workflows/agents/frontend-developer.yaml::*` — scope-reason: remove the retired top-level skills map
- `src/gobby/install/shared/workflows/agents/fullstack-developer.yaml::*` — scope-reason: remove the retired top-level skills map
- `src/gobby/install/shared/workflows/agents/memory-curator.yaml::*` — scope-reason: remove the retired top-level skills map
- `src/gobby/install/shared/workflows/agents/product-manager.yaml::*` — scope-reason: remove the retired top-level skills map
- `src/gobby/install/shared/workflows/agents/researcher.yaml::*` — scope-reason: remove the retired top-level skills map
- `src/gobby/install/shared/workflows/agents/tech-writer.yaml::*` — scope-reason: remove the retired top-level skills map
- `src/gobby/install/shared/workflows/agents/triage-agent.yaml::*` — scope-reason: remove the retired top-level skills map
- `tests/workflows/test_agent_definitions_v2.py::*` — scope-reason: cover the rejected key and stored version

**Granularity:** eleven Target files, but one behavior: the schema change and the
ten YAML edits it forces are one commit and one test run; splitting the YAML
edits would leave the bundle failing validation between leaves.

**Research context:** `AgentDefinitionBody` (`agent_models.py:78-219`) declares
`skills: dict[str, list[str]]` with no reader anywhere under `src/gobby` (grep for
`.skills` and `"skills"` over agents, workflows, dispatch, hooks, mcp_proxy finds
only the model, YAML, and docs). `reject_legacy_step_keys` (`:137`) already rejects
`role`, `goal`, `personality`, `instructions`, `steps`, `step_variables`,
`exit_condition` with migration hints; add `skills` to that list with the hint
"use workflows.skill_selectors for discovery and
step_workflow.variables.required_skills for required loads". Add
`version: StrictStr | None = None` next to `description`. Ten YAMLs carry
`skills:` (list above; `researcher.yaml:17`, `analyst.yaml:17`,
`backend-developer.yaml:18` are representative); their `methodology`, `baseline`,
and `tool_allowlist` prose moves into the prompt or is deleted where the
`load_skill` step already names the skill. The HTTP request models
(`servers/routes/agents.py::CreateAgentDefinitionRequest`,
`UpdateAgentDefinitionRequest`) do not carry `skills`, and the web builder
(`AgentsTabActions.ts::buildAgentDefinitionBody`) sends `skill_selectors`, so no
consumer edit is needed there. `gobby.workflows.definitions.__getattr__`
re-exports the models unchanged. Existing tests:
`test_agent_definitions_v2.py::TestAgentDefinitionBodyModel::test_legacy_prompt_fields_are_rejected_with_migration_hint`
is the pattern to extend. `template_hashes.py` hashes the parent body, so the
bundled hash changes once; that is expected drift refreshed by sync.

Verification planned: `GOBBY_TEST_PROTECT=1 uv run pytest
tests/workflows/test_agent_definitions_v2.py tests/workflows/test_agent_models.py
tests/agents/test_agents_sync.py -q`; `uv run ruff check`, `uv run mypy` on the
changed file.

**Acceptance:**

- 1.1.1 - A body with a top-level `skills` key is rejected with a migration hint
  naming `workflows.skill_selectors` and `step_workflow.variables.required_skills`.
  symbol: `AgentDefinitionBody.reject_legacy_step_keys`. test:
  `tests/workflows/test_agent_definitions_v2.py::test_skills_map_is_rejected_with_migration_hint`.
- 1.1.2 - `version` round-trips through `model_validate` and `model_dump` and is
  present in `definition_json` after bundled sync. file:
  `src/gobby/workflows/agent_models.py`. test:
  `tests/workflows/test_agent_definitions_v2.py::test_version_is_stored_in_body`.
- 1.1.3 - No bundled agent YAML contains a top-level `skills` key and every bundled
  YAML still validates against the body. test:
  `tests/workflows/test_agent_definitions_v2.py::test_bundled_agents_carry_no_skills_map`.

## P2: Shared Seat Rules
`kind: framing`

**Goal:** shared seat conduct and spawn policy are enforced rules, keyed on the
seat whether it arrived by persona or by spawn.

### 2.1 Roles rule group with shared seat guidance [category: config] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/rules/roles/inject-seat-common.yaml`
- `src/gobby/install/shared/workflows/rules/roles/reset-seat-common-on-context-loss.yaml`
- `src/gobby/install/shared/workflows/rules/AGENTS.md`
- `tests/workflows/test_seat_rules.py`

**Research context:** Rule grammar is `RuleDefinitionBody`
(`workflows/definitions.py:441`): `event`, `when`, `agent_scope`, `audience`,
`effects`. `inject_context` with a `template` is used by
`context-handoff/inject-user-profile.yaml`; once-per-epoch state uses a
`set_variable` effect and a reset on `session_start` with
`event.data.get('source') in ['clear', 'compact']`
(`skill-discovery/reset-skill-injection.yaml`, priority 8). Seat identity must
match both activation paths, so the condition is
`variables.get('_agent_type') in SEATS or variables.get('_persona_name') in SEATS`
with the Decision 2 catalogue inline; `agent_scope` alone would miss persona
sessions (As-Is Facts). File tags `[roles, seat, enforcement, gobby, default]`;
the `default` tag is required so the interactive `default` agent's selector
(`tag:default`) loads it as-is, and seat definitions also select `tag:roles`
(`selectors.py::_match_rule` matches `tag:` against file-level rule tags and
`group:` against a per-rule `group` field, which these rules do not set).

Rule `inject-seat-common`: `event: turn_start`, priority 20, `when:` seat match
and `not variables.get('_seat_common_injected')`; effects: `inject_context` with
the shared text, then `set_variable _seat_common_injected: true`. The shared
text is `_common.md`'s eight rules rewritten for definitions (find your seat by
`_persona_name`/`_agent_type`, stay inside it, out-of-role work goes to the PD,
messages only through `send_message`, no spawn except the automated close
reviewer and the Plan Writer's bounded enhancer pass, restart authority and
notices, quiet hours, titled task refs, "Systems nominal", combine aligned
tasks, decisions as buttons via the Assistant, ask once) plus Josh's standing
instructions from #22691: restart alerts immediately before and after with the
outcome, decisions as links, plans sent as attachments, no Telegram echo,
conditional sign-off, definitions never name a backend.

Rule `reset-seat-common-on-context-loss`: `event: session_start`, priority 8,
same source condition as `reset-skill-injection`, effect `set_variable
_seat_common_injected: false`.

`rules/AGENTS.md` gains a `roles` row in the group table. Test with the real
engine as `tests/workflows/test_agent_scope_rules.py` does (`_insert_rule`,
`_make_event`, `RuleDefinitionManager`): a `default` session with
`_persona_name: plan-writer` receives the injection once, not on the next turn,
and again after a `compact` session_start; a session with neither variable
receives nothing.

**Acceptance:**

- 2.1.1 - A persona-bound seat receives the shared seat guidance once per context
  epoch. file:
  `src/gobby/install/shared/workflows/rules/roles/inject-seat-common.yaml`. test:
  `tests/workflows/test_seat_rules.py::test_seat_common_injected_once_per_epoch`.
- 2.1.2 - A spawned seat (`_agent_type` set, no `_persona_name`) receives the same
  guidance; a non-seat session receives none. test:
  `tests/workflows/test_seat_rules.py::test_seat_common_matches_spawned_and_skips_non_seats`.
- 2.1.3 - Context loss re-arms the injection. file:
  `src/gobby/install/shared/workflows/rules/roles/reset-seat-common-on-context-loss.yaml`.
  test: `tests/workflows/test_seat_rules.py::test_seat_common_rearms_after_compact`.
- 2.1.4 - The rule group is documented. behavior: "`roles` row" in
  `src/gobby/install/shared/workflows/rules/AGENTS.md`.

### 2.2 Seat spawn policy with the Plan Writer enhancer exception [category: config] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/rules/roles/seat-spawn-policy.yaml`
- `.gobby/roles/_common.md`
- `tests/workflows/test_seat_rules.py`

**Research context:** `before_tool` rules read the proxy call as `tool_input`
with `server_name`, `tool_name`, and `arguments`
(`enforcement_checks.py:401-435`; rule example
`context-handoff/block-autonomous-clear-session.yaml` uses
`((tool_input.get('arguments') or tool_input) or {}).get('clear_session')`).
Block effects name MCP tools with `mcp_tools:`. `after_tool` MCP events expose
`event.data.get('mcp_server')`, `event.data.get('mcp_tool')`, and
`event.data.get('tool_output')`
(`task-enforcement/disclose-claimed-task-extra-skills.yaml`). `set_variable`
accepts a literal or an expression (`engine/effects.py::_apply_set_variable`;
`auto-task/inject-autonomous-mode.yaml:38` uses an expression). `claimed_tasks`
is a mapping keyed by task id (`hooks/event_handlers/_session_start/claims.py:61-77`,
and the same rule tests membership with `in`), so the bound uses a boolean
rather than indexing. No bundled rule blocks `spawn_agent` for interactive
sessions today; only `worker-safety/no-agent-spawn-for-merge.yaml`
(`agent_scope: [merge]`).

Rules in one file, tags `[roles, seat, enforcement, gobby, default]`:

- `seat-no-spawn`: `event: before_tool`, priority 10, `when:` seat match (2.1
  condition) and seat is not `plan-writer`; effect `block` with `mcp_tools:
  ["gobby-agents:spawn_agent", "gobby-agents:dispatch_batch"]`, reason: seats do
  not spawn; the automated close reviewer is the only spawn path; send work to the
  PD.
- `plan-writer-enhancer-only`: `event: before_tool`, priority 10, `when:` seat is
  `plan-writer` and not (`arguments.agent == 'plan-enhancer-taskless'` and
  `arguments.isolation == 'none'` and `variables.get('task_claimed')` and
  `not variables.get('plan_writer_enhancer_pass_used')`); effect `block` on
  `gobby-agents:spawn_agent` and `gobby-agents:dispatch_batch`, reason naming the
  exact allowed call and that one pass per claimed planning task is permitted.
- `plan-writer-enhancer-consumed`: `event: after_tool`, `when:` seat is
  `plan-writer` and `event.data.get('mcp_server') == 'gobby-agents'` and
  `event.data.get('mcp_tool') == 'spawn_agent'` and the output reports success;
  effect `set_variable plan_writer_enhancer_pass_used: true`.
- `plan-writer-enhancer-rearm`: `event: after_tool`, `when:` seat is
  `plan-writer` and `event.data.get('mcp_server') == 'gobby-tasks'` and
  `event.data.get('mcp_tool') == 'claim_task'` and the output carries a
  `task_id`; effect `set_variable plan_writer_enhancer_pass_used: false`. A new
  claimed planning task therefore re-arms exactly one pass.

`_common.md` third bullet becomes: "Do not spawn agents. The automated
task-close reviewer is the only permitted spawn path, plus the Plan Writer's
single `plan-enhancer-taskless` pass per claimed planning task (rule
`plan-writer-enhancer-only`)."

**Acceptance:**

- 2.2.1 - A non-writer seat's `spawn_agent` and `dispatch_batch` calls are blocked
  with the seat reason. file:
  `src/gobby/install/shared/workflows/rules/roles/seat-spawn-policy.yaml`. test:
  `tests/workflows/test_seat_rules.py::test_seats_cannot_spawn`.
- 2.2.2 - The Plan Writer may spawn `plan-enhancer-taskless` with `isolation:
  none` once per claimed planning task; a second attempt for the same task, a
  different agent, another isolation, or no claimed task is blocked. test:
  `tests/workflows/test_seat_rules.py::test_plan_writer_enhancer_pass_is_bounded`.
- 2.2.3 - The shared role rules name the exception. behavior:
  "plan-writer-enhancer-only" in `.gobby/roles/_common.md`.

## P3: Seat Definitions
`kind: framing`

**Goal:** every standing seat is a bundled definition whose prompt is the role
text of record, with rules, skills, tool restrictions, and step workflows
declared. Common shape for every seat (Decisions 8, 9, 12): `name`, `version:
"1.0"`, `description`, `tags: [gobby, seat]`, `surfaces: [spawn, persona]`,
`provider: inherit`, `isolation: inherit`, `timeout: 0`, `prompts:` with
`agent: &seat |` and `persona: *seat`, `workflows.rule_selectors.include:
["tag:default", "tag:roles", ...]`, `blocked_mcp_tools:
["gobby-agents:kill_agent"]`, and `workflows.variables: {}` unless stated. Each
prompt opens with the role's purpose, then its message contract, then continuity
policy (Decision 10), then the two baseline sections every seat inherits from
`default.yaml` verbatim (`## Platform Context` and `## Skills`), because
`apply_persona` replaces the default persona rather than layering on it. Role
text comes from the current `.gobby/roles/*.md` files and the runbooks plan §3.2
persona highlights; session refs and epic numbers stay out of prompts (they are
roster and task state). Step transitions use variables the seat sets with the
proxy `set_variable` tool or that a step's `on_mcp_success` handler sets after a
named MCP call (`WorkflowStep.on_mcp_success`, `definitions.py:560`).

### 3.1 Coordination seats: assistant, program-director, lane-manager [category: config] (depends: 2.2)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/agents/assistant.yaml`
- `src/gobby/install/shared/workflows/agents/program-director.yaml`
- `src/gobby/install/shared/workflows/agents/lane-manager.yaml`

**Research context:** `comms-agent.yaml` (persona only, `blocked_tools` at
`:46`, `blocked_mcp_tools` at `:62`) is the nearest existing shape for a
message-routing seat; keep it, the assistant is a distinct definition. Role text
of record: `.gobby/roles/assistant.md` (routes only; relays Josh's words to the PD
verbatim; half-hour status per lane; load-breach message at 5-minute load ≥16 on
two checks; read-only DB helper; one-line Telegram confirmation),
`program-director.md` (coordination only; files and delegates tasks; reviews
candidates with the Code Reviewer, lands, restarts with notices; updates the
Archivist on every land, bounce, reroute, park, task, restart; answers Josh in
its own session; decisions as buttons via the Assistant), `lane-manager.md`
(routes PD-assigned work in PD order; ACKs HOLD/RESUME; event lines
`LANE= EVENT=STARTED|CANDIDATE|BOUNCE|CLOSED TASK=#NNNNN TASK_TITLE= RUN= WT=
COMMIT= NOTE=`; verdict blockers to PD; no review, land, restart, or code).
Selectors: all three add `tag:worker-safety` exclusions that block interactive
destructive shell (`no-destructive-shell-interactive`) stay via `tag:default`;
program-director additionally includes `name:no-force-push-interactive`,
`name:no-destructive-git-interactive`; assistant and lane-manager carry the
Decision 8 read-only `blocked_tools` list with the assistant's carve-out for
`.md` writes under `docs/` and `.gobby/roles/` stated in prose (enforcement of
that carve-out is #22903's activation). Continuity: compact, never clear. No
`step_workflow` (Decision 7).

**Acceptance:**

- 3.1.1 - `assistant` validates, is `seat`-tagged, carries the routing-only
  contract, status cadence, restart alerts, and one-line Telegram confirmation.
  file: `src/gobby/install/shared/workflows/agents/assistant.yaml`.
- 3.1.2 - `program-director` validates, is `seat`-tagged, and carries the
  coordination-only contract, restart authority with notices, Archivist updates,
  and decision routing. file:
  `src/gobby/install/shared/workflows/agents/program-director.yaml`.
- 3.1.3 - `lane-manager` validates, is `seat`-tagged, read-only, and carries the
  event-line grammar and HOLD/RESUME behavior. file:
  `src/gobby/install/shared/workflows/agents/lane-manager.yaml`.
- 3.1.4 - All three select `tag:roles` and carry the `seat` tag. behavior:
  "tag:roles" in `src/gobby/install/shared/workflows/agents/lane-manager.yaml`.

### 3.2 One developer definition with task-routed skills [category: code] (depends: 2.2)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/agents/developer.yaml`
- `src/gobby/install/shared/workflows/agents/backend-developer.yaml::*` — scope-reason: delete; its body seeds developer.yaml
- `src/gobby/install/shared/workflows/agents/frontend-developer.yaml::*` — scope-reason: delete; differs from backend-developer only in prose
- `src/gobby/install/shared/workflows/agents/fullstack-developer.yaml::*` — scope-reason: delete; differs from backend-developer only in prose
- `src/gobby/dispatch/_rule_state.py::*` — scope-reason: retarget the default-agent fallback string to developer
- `src/gobby/dispatch/prompts.py::*` — scope-reason: collapse the three developer prompt-builder map entries to developer
- `src/gobby/dispatch/AGENTS.md`
- `src/gobby/install/shared/registry/stages.yaml::*` — scope-reason: retarget the development stage default_agent to developer
- `src/gobby/install/shared/prompts/expansion/system.md`
- `src/gobby/install/shared/skills/gobby/references/plan/expansion.md`
- `src/gobby/tasks/prompts/expand-task-tdd.md`
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py::*` — scope-reason: retarget the default agent name and its docstring
- `src/gobby/plans/manifest_emitter.py::*` — scope-reason: retarget the category-to-agent map and fallback
- `src/gobby/tasks/categories.py::*` — scope-reason: map the backend, frontend, and fullstack domains to developer
- `src/gobby/tasks/expansion/_common.py::*` — scope-reason: retarget the default agent, category map, audit marker text, and the frontend/backend heuristic
- `tests/workflows/test_workflows_agent_definitions.py::*` — scope-reason: replace the three developer names in the runtime-mapping and claim-step lists with developer
- `tests/adapters/test_claude_code_mcp_output.py::*` — scope-reason: rename retired developer names to developer
- `tests/agents/test_agents_sync.py::*` — scope-reason: rename retired developer names to developer
- `tests/agents/test_backend_ingress.py::*` — scope-reason: rename retired developer names to developer
- `tests/agents/test_lifecycle_task_completion.py::*` — scope-reason: rename retired developer names to developer
- `tests/agents/test_run_completion.py::*` — scope-reason: rename retired developer names to developer
- `tests/agents/test_spawn_executor_providers.py::*` — scope-reason: rename retired developer names to developer
- `tests/build/test_dispatcher_stage_wake.py::*` — scope-reason: rename retired developer names to developer
- `tests/build/test_observability.py::*` — scope-reason: rename retired developer names to developer
- `tests/build/test_resume_lifecycle.py::*` — scope-reason: rename retired developer names to developer
- `tests/build_pipeline/test_build_pipeline_build_profiles.py::*` — scope-reason: rename retired developer names to developer
- `tests/build_pipeline/test_build_pipeline_cascade.py::*` — scope-reason: rename retired developer names to developer
- `tests/build_pipeline/test_build_pipeline_service.py::*` — scope-reason: rename retired developer names to developer
- `tests/build_pipeline/test_build_resolves_manifest.py::*` — scope-reason: rename retired developer names to developer
- `tests/build_pipeline/test_controls.py::*` — scope-reason: rename retired developer names to developer
- `tests/cli/test_cli_build.py::*` — scope-reason: rename retired developer names to developer
- `tests/dispatch/test_daemon_resume.py::*` — scope-reason: rename retired developer names to developer
- `tests/dispatch/test_dispatch_actions.py::*` — scope-reason: rename retired developer names to developer
- `tests/dispatch/test_dispatch_prompts.py::*` — scope-reason: rename retired developer names to developer
- `tests/dispatch/test_dispatcher.py::*` — scope-reason: rename retired developer names to developer
- `tests/dispatch/test_rules.py::*` — scope-reason: rename retired developer names to developer
- `tests/dispatch/test_spawn_forwarding.py::*` — scope-reason: rename retired developer names to developer
- `tests/e2e/test_build_dispatcher_autonomy.py::*` — scope-reason: rename retired developer names to developer
- `tests/hooks/test_agent_events_coverage.py::*` — scope-reason: rename retired developer names to developer
- `tests/hooks/test_provider_launch_guard.py::*` — scope-reason: rename retired developer names to developer
- `tests/hooks/test_session_coordinator.py::*` — scope-reason: rename retired developer names to developer
- `tests/mcp_proxy/test_mcp_proxy_stdio.py::*` — scope-reason: rename retired developer names to developer
- `tests/mcp_proxy/tools/spawn_agent/test_error_handling.py::*` — scope-reason: rename retired developer names to developer
- `tests/mcp_proxy/tools/spawn_agent/test_initial_variables.py::*` — scope-reason: rename retired developer names to developer
- `tests/tasks/test_expansion_service_compile_plan_12725.py::*` — scope-reason: it imports AGENT_BY_IMPLEMENTATION_DOMAIN; assert every domain routes to developer
- `tests/fixtures/plans/expansion-compile-regression.md`
- `tests/fixtures/plans/manifest-routing-bridge.md`

**Granularity:** one behavior, one commit: a definition rename is only complete
when every constant and test that names the old definitions moves with it.
Most test edits are the same string replacement; the leaf stays one unit
because a half-renamed tree fails the bundle tests between commits.

**Research context:** Five roster sessions (lanes 1–4, rust-migration) share
one flow: claim → implement → validate → commit → submit to PD; the lane's
epic is task state. Josh's rulings (this session): one developer profile that
loads the skills the assigned task needs, told which lane it occupies. A diff
of the three developer YAMLs shows they differ only in `description`, the
`skills:` prose that 1.1 deletes, one sentence of `prompts.persona`, the
toolchain paragraph in `prompts.agent`, the backend fallback audit-marker
paragraph, and the claim-step `description` label. The shared workflow is
`claim` → `load_required_skills` → `load_additional_skills` → `implement` →
`terminate` with `required_skills:
[gobby:references/development/obligations.md, restraint,
gobby:references/tasks/overview.md]`, and `load_additional_skills`
(`backend-developer.yaml:174-217`) already gates on
`all(skill_loaded(skill) for skill in vars.additional_skills)`, so task-routed
skills need no new mechanism: the prompt tells the developer to read the
claimed task and set `additional_skills` with the proxy `set_variable` before
that step runs. Routing table in the prompt: task touches `web/` → `impeccable`
(the design contract in `AGENTS.md` requires it) and `typescript`; touches
`crates/` → `rust` (`AGENTS.md` requires it before editing Rust); Python only →
nothing beyond the required set; docs only → `tech-writer`. All four exist
under the bundled skills directory.

`developer.yaml` is `backend-developer.yaml` with: `name: developer`, `tags:
[gobby, seat]`, Decision 12 provider, model, and isolation, `rule_selectors.include:
["tag:default", "tag:worker-safety", "tag:roles"]`, one prompt text for both
surfaces (lane purpose; lane identity from the role file and PD or Lane Manager
messages; the flow claim → implement → validate → commit → submit a CANDIDATE
through the Lane Manager event line `LANE= EVENT=CANDIDATE TASK=#NNNNN
TASK_TITLE= RUN= WT= COMMIT= NOTE=`; HOLD/GO discipline; found work on the
lane's surface is the lane's; no restarts or binary promotion unless the PD
says so; work in the lane's worktree; the skill routing table; then the
`## Platform Context` and `## Skills` baseline sections and the continuity
policy, clear between tasks), and the `terminate` step replaced by `submit`
(allowed MCP: `send_message`, `close_task`, `link_commit`; `on_mcp_success`
for `gobby-agents:send_message` resets `task_claimed`,
`implementation_complete`, `additional_skills_loaded`; transition to `claim`
when `not vars.task_claimed`). No `exit_condition`. The claim step keeps the
"Spawned agents normally arrive" phrases that
`test_workflows_agent_definitions.py::test_claim_guidance_accounts_for_spawn_preclaim`
pins, with `developer` replacing the three names in that list and in
`test_build_smoke_agent_runtime_mappings` (which pins provider and model, so
its `developer` row asserts `inherit` and no model).

Routing constants: `dispatch/_rule_state.py:182` falls back to
`"backend-developer"`; `dispatch/prompts.py:345-351` maps all three names to
`_developer`; `registry/stages.yaml:57` sets `default_agent:
backend-developer`; `tasks/categories.py:12-14` maps the backend, frontend,
and fullstack domains to the three names; `tasks/expansion/_common.py:29,65-68,
116,199-202,318` carries the default, the category map, the audit-marker text,
and a frontend-versus-backend score heuristic that collapses to `developer`;
`plans/manifest_emitter.py:64-70` has the same map; `spawn_agent/_factory.py:
627-757` defaults `agent` to `backend-developer`; the three prompt and
reference texts describe the routing in prose. All become `developer`;
`implementation_domain` stays on tasks as a hint the developer reads for its
skill table. The `assigned_agent` parameter description in the task CRUD tool
module quotes the old name as an example only; that module sits at the
monolith ceiling, so the example string stays until a task that decomposes
the module, and 3.2.2 is scoped to routing. Thirty test modules name the old definitions; run
each after the rename, and keep fixture strings that only pass through a name.
`web/` tests carry the old name as fixture data only and are untouched.

Verification planned: `GOBBY_TEST_PROTECT=1 uv run pytest
tests/workflows/test_workflows_agent_definitions.py tests/dispatch
tests/build_pipeline tests/agents/test_agents_sync.py
tests/tasks/test_expansion_service_compile_plan_12725.py -q`, then the V1
commands.

**Acceptance:**

- 3.2.1 - `developer` validates, is `seat`-tagged, selects `tag:roles`, carries
  the lane flow with the CANDIDATE event line, HOLD/GO discipline, and the
  skill routing table, and declares the claim → load skills → implement →
  submit loop. behavior: "EVENT=CANDIDATE" in
  `src/gobby/install/shared/workflows/agents/developer.yaml`.
- 3.2.2 - `backend-developer.yaml`, `frontend-developer.yaml`, and
  `fullstack-developer.yaml` no longer exist and no routing constant, stage
  binding, prompt builder, or expansion prompt under `src/gobby` names them.
  behavior: "developer" replaces the three names in
  `src/gobby/tasks/categories.py`.
- 3.2.3 - Every task category and implementation domain that routed to one of
  the three now routes to `developer`, and the default-agent fallback is
  `developer`. symbol: `src/gobby/tasks/categories.py::AGENT_BY_IMPLEMENTATION_DOMAIN`.
  test:
  `tests/tasks/test_expansion_service_compile_plan_12725.py::test_domains_route_to_developer`.
- 3.2.4 - The bundle smoke test and claim-step test pass with `developer` in
  place of the three names. test:
  `tests/workflows/test_workflows_agent_definitions.py::test_build_smoke_agent_runtime_mappings`.

### 3.3 Review and observation seats: code-reviewer, archivist, log-monitor, researcher [category: config] (depends: 2.2, 3.2)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/agents/code-reviewer.yaml`
- `src/gobby/install/shared/workflows/agents/archivist.yaml`
- `src/gobby/install/shared/workflows/agents/log-monitor.yaml`
- `src/gobby/install/shared/workflows/agents/researcher.yaml::*` — scope-reason: replace the research-stage one-shot body with the seat body
- `tests/agents/test_discovery_agents.py::*` — scope-reason: remove researcher from the discovery-agent spec
- `tests/workflows/test_workflows_agent_definitions.py::*` — scope-reason: the researcher row asserts the seat's inherited provider and no claim step

**Research context:** `code-reviewer`: read-only `blocked_tools`; selectors add
`tag:review-learning`; `required_skills: [code-review, restraint]`;
`step_workflow.variables: {candidate_received: false, verdict_ready: false}`;
steps `await` (allowed MCP: `send_message`, sessions read, the proxy
`set_variable`; transition to `review` when `vars.candidate_received`, which
the seat sets from the PD's CANDIDATE message), `review` (read-only tools plus
`gcode` and `git diff`; transition to `verdict` when `vars.verdict_ready`),
`verdict` (allowed MCP: `send_message`, `set_variable`; the message carries
`EVENT=CANDIDATE_VERDICT ... VERDICT=LAND|BOUNCE` with HIGH/MEDIUM/LOW findings
to PD and author lane; the seat then resets both variables and the step
transitions to `await`). Continuity: clear between verdicts.

`researcher.yaml` is today the discovery one-shot bound to the `research`
stage (`stages.yaml:16`) with a claim → load_skill → draft workflow that ends
the run (`exit_condition: "vars.handoff_ready"`, `provider: codex`, pinned
model). Per Decision 2 the seat body replaces it under the same name; the old
body stays in git history. `tests/agents/test_discovery_agents.py:20-45` lists
`researcher` in `DISCOVERY_AGENTS` (stage `research`, skill `research`,
section "Research Findings") and every parametrized test there pins the stage
one-shot's claim step, marker, and MCP allowlist: remove the `researcher`
entry; `analyst`, `architect`, and `product-manager` stay.
`test_workflows_agent_definitions.py:157` pins `researcher` to codex and a
model, and `:224` lists it among agents with a claim step; both rows change to
the seat's shape (`inherit`, no model, no claim step).
`trajectory-monitor.yaml` is a review-stage reviewer, not a log monitor.
Role text: `archivist.md` (sole writer of
`/Users/josh/Desktop/gobby-digest-<date>.md`, "Lane queues" section from PD
updates; flags gaps to PD), `monitor.md` (runs `gobby:log-monitor-tick` every 10
minutes; "Systems nominal" or `EVENT=ALARM` with evidence to PD, copy to
Assistant; performance verdicts only from matched windows; nominal line per
runbooks: `Systems nominal | window HH:MM-HH:MM | <N> warnings in <K> families,
all mapped`), `researcher.md` (research only; findings to PD; decisions for Josh
via the Assistant; asks PD before killing processes). Selectors: archivist and
log-monitor use `tag:roles`, `tag:context-handoff`, `tag:memory-lifecycle`,
`tag:worker-safety`, and `tag:default`; researcher adds
`tag:task-skill-gates`. All four read-only; the archivist's digest write is
outside the checkout and stated in prose. `required_skills`: log-monitor
`[brevity, gobby:references/observability/diagnostics.md,
gobby:references/observability/metrics.md]`; researcher
`[research, restraint, brevity]`. `monitor.md` names `gobby:log-monitor-tick`,
which exists nowhere (not a bundled skill directory, not a file under
`src/gobby/install/shared/skills/gobby/references/observability/`, not in the
installed skill list, not under `.gobby/skills/`); the PD filed #22908 for the
live role file, and the `log-monitor` prompt carries the tick procedure as an
explicit checklist (log families under `~/.gobby/logs/`, warning counts per
family, load only on a breach, matched windows for performance verdicts) with
no skill reference for it. `log-monitor` step workflow:
`step_workflow.variables: {tick_done: false}`; `tick` (allowed MCP: skills,
sessions read, the proxy `set_variable`, `send_message`; transition to
`report` when `vars.tick_done`) → `report` (`on_mcp_success` for
`gobby-agents:send_message` sets `tick_done: false`; transition to `tick` when
`not vars.tick_done`), no exit. Continuity: archivist and log-monitor compact,
never clear; researcher and code-reviewer clear after each report or verdict.
`sandbox_profile: research` on `researcher` is deferred (D2) until #22899's
field lands.

**Acceptance:**

- 3.3.1 - `code-reviewer` validates, is `seat`-tagged, read-only, names only
  bundled skills (`code-review`, `restraint`), and declares the await → review
  → verdict loop with the verdict grammar. file:
  `src/gobby/install/shared/workflows/agents/code-reviewer.yaml`.
- 3.3.2 - `archivist` validates, is `seat`-tagged, read-only inside the
  checkout, and carries the digest contract. file:
  `src/gobby/install/shared/workflows/agents/archivist.yaml`.
- 3.3.3 - `log-monitor` validates, is `seat`-tagged, declares the tick → report
  loop, carries the nominal and ALARM grammar, and names no
  `gobby:log-monitor-tick` skill. behavior: "Systems nominal | window" in
  `src/gobby/install/shared/workflows/agents/log-monitor.yaml`.
- 3.3.4 - `researcher` validates as a seat (`seat`-tagged, read-only, inherited
  provider, no claim step), and the discovery-agent tests no longer list it.
  file: `src/gobby/install/shared/workflows/agents/researcher.yaml`. test:
  `tests/agents/test_discovery_agents.py::test_discovery_agent_yaml_validates_and_is_enabled`.

### 3.4 Planning council seats and the review flow [category: config] (depends: 2.2, 3.3)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/agents/plan-writer.yaml`
- `src/gobby/install/shared/workflows/agents/plan-adversary.yaml::*` — scope-reason: replace the planning-stage reviewer body with the seat body
- `tests/agents/test_plan_adversary_loads_plan_review.py::*` — scope-reason: run against the seat body; retarget claim and terminate assertions
- `tests/agents/test_plan_adversary_manifest.py::*` — scope-reason: run against the seat body; retarget staged-verdict assertions
- `tests/agents/test_plan_adversary_no_edits_on_reject.py::*` — scope-reason: run against the seat body
- `tests/agents/test_plan_adversary_self_check.py::*` — scope-reason: run against the seat body
- `tests/agents/test_plan_adversary_internal_research_definition.py::*` — scope-reason: the reviewer-model pin and spawn-hook assertions cover the taskless reviewer only
- `tests/mcp_proxy/test_stage_review_schema.py::*` — scope-reason: the staged review schema is a build contract; retarget to plan-adversary-taskless or delete
- `tests/skills/test_plan_adversary_rejection.py::*` — scope-reason: run against the seat body
- `tests/skills/test_plan_skill_grammar.py::*` — scope-reason: run against the seat body
- `tests/agents/test_discovery_agents.py::*` — scope-reason: the plan-adversary task-skill-gate exclusion assertion reads the seat body
- `tests/workflows/test_workflows_agent_definitions.py::*` — scope-reason: the plan-adversary row asserts the seat's inherited provider

**Research context:** `planner`, `plan-enhancer`, `analyst`, and the two
taskless reviewers stay untouched (Decision 2, Constraints).
`plan-adversary.yaml` (23 KB, `version: "1.3"`, codex, pinned model, steps
`claim` → `load_skill` → `review` → `terminate`, `exit_condition:
"current_step == 'terminate'"`) is the planning-stage reviewer `stages.yaml:38`
binds; the seat body replaces it under the same name. Eight test modules load
that file by path or name (list above). The seat keeps the review-contract
wiring most of them pin: `load_skill` targets
`gobby:references/plan/review.md` and `proportionality` before review,
rejection is findings-only with no plan-file edits, plan edits are attributed
to the writer, manifest application is delegated and
`apply_plan_review_repairs` is blocked, the identity guard precedes manifest
derivation, the self-check retry is capped at three, and `kill_agent` is
absent. Assertions on `claim`, `terminate`, `end_agent_run`, the staged
verdict path, and the pinned reviewer model are stage mechanics: run each
module against the seat body, keep what passes, retarget the rest to
`plan-adversary-taskless` (steps `load_skill` → `review` only)
where it has the step, and delete assertions with no seat or taskless
equivalent. `test_discovery_agents.py:210` and
`test_workflows_agent_definitions.py:151` also read `plan-adversary`.
Skill references that exist today: `gobby:references/plan/drafting.md`,
`gobby:references/plan/coverage.md`, `gobby:references/plan/review.md`,
`gobby:references/plan/enhancement.md`, `restraint`, `proportionality`. Role
text: `plan-writer.md`, `plan-adversary.md`, and Josh's proposed flow (#22902
description). Lessons injection rules
(`review-learning/inject-planner-lessons.yaml`,
`inject-plan-reviewer-lessons.yaml`) key on `agent_scope` names; add the seat
names to those scopes only if the PD wants lessons for seats (not in this plan;
memory review-lessons-value shows empty pools).

`plan-writer`: not read-only (edits `.gobby/plans/*.md` only; state in prose,
enforcement is #22903's); selectors `tag:default`, `tag:roles`,
`tag:plan-mode`; `variables: {plan_mode: true}` as `planner.yaml:118` does;
`required_skills: [restraint, gobby:references/plan/drafting.md,
gobby:references/plan/coverage.md]`; steps `draft` (transition to `enhance`
when `vars.draft_validated`, set after `uv run gobby plans validate` passes),
`enhance` (allowed MCP includes `gobby-agents:spawn_agent` guarded by 2.2 and
`wait_for_agent`; transition to `revise` when `vars.enhancement_reviewed`),
`revise` (transition to `adversary` when `vars.candidate_sent`), `adversary`
(allowed MCP: `send_message`, `wait_for_coordination`, plans read; transition to
`revise` on returned findings, to `handoff` when `vars.consensus`), `handoff`
(sends the candidate plus disagreements to PD; transition to `draft` when a new
planning task is claimed). No `exit_condition`. Continuity: clear between plans.

`plan-adversary`: read-only; `required_skills: [restraint, proportionality,
gobby:references/plan/review.md, gobby:references/plan/coverage.md]`;
`step_workflow.variables: {round_prepared: false, verdict_ready: false,
consensus: false}`; steps `await` (allowed MCP: `send_message`, plans read,
`prepare_plan_review_round`; `on_mcp_success` for `prepare_plan_review_round`
sets `round_prepared: true`; transition to `review` when `vars.round_prepared`)
→ `review` (allowed MCP: `get_plan_review_snapshot`, `derive_plan_review_manifest`,
`validate_plan_review_coverage`, `finalize_plan_review_evidence`,
`send_message`, the proxy `set_variable`; findings with stable ids, blocking or
nit, category from the review vocabulary; transition to `resolve` when
`vars.verdict_ready`) → `resolve` (loops with the Writer by `send_message` until
`vars.consensus` or a recorded disagreement; on consensus sends the PD the
approval payload from Decision 14; `on_mcp_success` for
`finalize_plan_review_evidence` resets `round_prepared`; transition to `await`).
Prompt states Decision 14: the seat derives and returns the manifest content
and never writes M1; the PD applies it.

**Acceptance:**

- 3.4.1 - `plan-writer` validates, is `seat`-tagged, and declares the
  draft → enhance → revise → adversary → handoff loop with the bounded enhancer
  pass and PD approval gate. file:
  `src/gobby/install/shared/workflows/agents/plan-writer.yaml`.
- 3.4.2 - `plan-adversary` validates as a seat, is read-only, and declares the
  await → review → resolve loop that never writes the manifest. file:
  `src/gobby/install/shared/workflows/agents/plan-adversary.yaml`.
- 3.4.3 - The review-contract wiring tests pass against the seat body: review
  and proportionality skills load before review, rejection is findings-only,
  manifest application is delegated. test:
  `tests/agents/test_plan_adversary_no_edits_on_reject.py::test_instructions_forbid_plan_edits_on_rejection`.
- 3.4.4 - `planner`, `plan-enhancer`, `analyst`, and both `-taskless`
  definitions are byte-identical before and after this deliverable except for
  the 1.1 `skills` removal, and the taskless reviewer contract still holds.
  test:
  `tests/agents/test_plan_adversary_internal_research_definition.py::test_taskless_review_status_allows_protocol_failure_without_verdict`.

### 3.5 Seat bundle contract test [category: test] (depends: 3.1, 3.2, 3.3, 3.4)
`kind: deliverable`

Target: `tests/workflows/test_seat_definitions.py`

**Research context:** `tests/workflows/test_workflows_agent_definitions.py`
already loads every bundled YAML (`AGENTS_DIR = get_bundled_agents_path()`,
`SKILLS_DIR = get_bundled_skills_path()`, `_load_yaml`, `_agent`, `_step`) and
checks step-level invariants; follow its helpers in a new module so the checks
stay data-driven over every `seat`-tagged file. Skill resolution: a
`required_skills` entry is either a bundled skill directory name under
`SKILLS_DIR` or `<skill>:<path>` where the path exists under that skill
(`skills/instruction_requirements.py::parse_instruction_requirement` is the
grammar). Seat invariants to pin: `surfaces == [spawn, persona]`;
`prompts.persona == prompts.agent`; `provider == inherit`; no `model`;
`isolation == inherit`; `timeout == 0`; every top-level key is either an
`AgentDefinitionBody` field or one of the sync metadata keys `tags`, `priority`,
`type` (`validate_workflow_definition_data` strips exactly `priority`, `tags`,
`type`, `version`, and `version` is a body field after 1.1), which also rejects
`terminal_backend`, `backend`, and `sandbox`; `rule_selectors.include` contains
`tag:roles`; every `step_workflow.variables.required_skills` entry resolves;
read-only seats (`assistant`, `lane-manager`, `code-reviewer`, `archivist`,
`log-monitor`, `researcher`, `plan-adversary`) carry the Decision 8
`blocked_tools` list; seats with a step workflow (`developer`, `plan-writer`,
`plan-adversary`, `code-reviewer`, `log-monitor`) declare no `exit_condition`;
every seat prompt contains the `## Platform Context` and `## Skills` baseline
sections; every seat prompt mentions `gobby-agents:send_message`; the
`developer` prompt contains `EVENT=CANDIDATE` and names `impeccable`, `rust`,
`typescript`, and `tech-writer` in its routing table. The test runs against the
checkout, not the database, so it needs no daemon.

**Acceptance:**

- 3.5.1 - Every `seat`-tagged bundled definition satisfies the invariants above.
  test: `tests/workflows/test_seat_definitions.py::test_seat_definitions_share_the_seat_contract`.
- 3.5.2 - Every `required_skills` entry in every bundled definition resolves to a
  bundled skill or skill file. test:
  `tests/workflows/test_seat_definitions.py::test_required_skills_resolve_to_bundled_skills`.
- 3.5.3 - The set of `seat`-tagged names equals the ten-name catalogue in
  Decision 2, so an added or removed seat is a deliberate test edit. test:
  `tests/workflows/test_seat_definitions.py::test_seat_catalogue_is_exact`.
- 3.5.4 - The `developer` routing table names only bundled skills and the lane
  event line. test:
  `tests/workflows/test_seat_definitions.py::test_developer_routes_skills_by_task`.

## P4: Migration And Documentation
`kind: framing`

**Goal:** live sessions bind to their definitions through the existing
`apply_persona` tool without breaking #22894, and the docs describe the schema
that now exists.

### 4.1 Role files point at seat definitions [category: config] (depends: 3.5)
`kind: deliverable`

Targets:
- `.gobby/roles/assistant.md`
- `.gobby/roles/program-director.md`
- `.gobby/roles/lane-manager.md`
- `.gobby/roles/lane-1-gclient.md`
- `.gobby/roles/lane-2-stability.md`
- `.gobby/roles/lane-3-hooks.md`
- `.gobby/roles/lane-4-backlog.md`
- `.gobby/roles/rust-migration.md`
- `.gobby/roles/code-reviewer.md`
- `.gobby/roles/researcher.md`
- `.gobby/roles/archivist.md`
- `.gobby/roles/monitor.md`
- `.gobby/roles/plan-writer.md`
- `.gobby/roles/plan-adversary.md`

**Granularity:** fourteen files, one mechanical edit pattern, one commit; the
roster test and the persona receipt verify all rows together.

**Research context:** #22894 is open (blocked by #22870) and its acceptance
requires the roster rows, `_common.md`, one role file per session, and the
`default.yaml` lookup phrases (`test_default_agent_role_contract.py::test_default_profile_describes_all_lookup_guards`
pins the phrases; `_probe_documented_lookup` pins the roster row regex). Neither
file changes here. Each role file keeps its heading and becomes: "Definition:
`<seat>`. Call `gobby-agents:apply_persona(agent="<seat>")` once, then follow
the injected persona. Lane epics and session refs are task and roster state."
The five lane files (`lane-1-gclient.md` #22773, `lane-2-stability.md`
#22882, `lane-3-hooks.md` #22881, `lane-4-backlog.md` #22880,
`rust-migration.md`) all point at `developer` and keep one line naming the
lane and its epic with its title: that line is how a lane is told which lane
it occupies (Decision 2), and the PD re-points a lane by editing it.
`researcher.md` points at `researcher`, `monitor.md` at `log-monitor`,
`plan-adversary.md` at `plan-adversary`. `monitor.md` is also #22908's target
(Lane 3 removes the phantom skill reference); whichever lands second rebases
onto the other, and the pointer form wins because it carries no skill
reference. `apply_persona` persists `_persona_name`, which survives compaction
and re-injects the persona; the 2.x rules key on it. Retiring the roster and
role files is #22903's (binding) work.

Rollout (Decision 13): after merge, the PD calls `gobby-workflows:reload_cache`
from the main checkout, confirms `gobby agents show <seat>` for one seat, then
each live session calls `apply_persona` for its seat on its next turn and reports
the receipt to the PD, as done for #22894.

**Acceptance:**

- 4.1.1 - Every role file names its seat definition and the `apply_persona` call;
  the roster regex and `default.yaml` phrases are unchanged. file:
  `.gobby/roles/plan-writer.md`. test:
  `tests/workflows/test_default_agent_role_contract.py::test_default_profile_describes_all_lookup_guards`.
- 4.1.2 - A mapped session that calls `apply_persona` for its seat receives that
  seat's prompt and the shared seat guidance on its next turn. behavior:
  "persona receipt from one live seat recorded in the close summary of the
  implementing leaf" in `.gobby/roles/roster.md`.

### 4.2 Guide updates [category: docs] (depends: 1.1)
`kind: deliverable`

Targets:
- `docs/guides/agents.md`
- `docs/guides/workflows-overview.md`

**Research context:** `docs/guides/agents.md:96` documents `skills` as
"Metadata for baseline and allow-listed skill families" (now rejected);
`docs/guides/workflows-overview.md:54-60` still lists `role`, `goal`,
`personality`, `instructions` as persona fields (rejected since the schema
cutover). `agents.md` has no versioning section and no seat catalogue.

Edits: remove the `skills` row and add a `version` row to the Definition Shape
table; add a "Seats" section stating Decisions 1, 2, 5–10, 12, the ten-name
catalogue, the single `developer` definition with its task-routed skill table,
and the build degradation recorded in As-Is Facts; add a "Versioning"
paragraph: `version` is a human marker inside
`definition_json`, the content hash (`compute_definition_hash`) is the identity
activation pins, and bundled drift is refreshed by sync; fix the
`workflows-overview.md` field list to `prompts.persona`/`prompts.agent`.

**Acceptance:**

- 4.2.1 - The agents guide documents `version`, omits `skills`, and lists the seat
  catalogue with its shared shape. behavior: "Seats" section in
  `docs/guides/agents.md`.
- 4.2.2 - The workflows overview names the current prompt fields. behavior:
  "`prompts.persona`" in `docs/guides/workflows-overview.md`.

## D1 Definition persistence defects (depends: 1.1)
`kind: deferred`

Three defects found while planning, consolidated by the PD into #22906
(export/import `type`, reinstall ownership, HTTP mode/sandbox create parity)
under #22691 with `needs-planning`. Schema decisions here that unblock it:
`AgentDefinitionBody` stays `extra="ignore"` for sync-managed rows; HTTP request
models must not carry fields the body lacks (`mode`, `default_workflow`,
`sandbox_config`, `lifecycle_variables`, `default_variables`); exported YAML
must carry `type: agent` so `sync_imported_definition` accepts it; reinstall may
hard-delete only rows that satisfy `_is_sync_managed_bundled_agent`.

```yaml
deferral:
  task_ref: "#22906"
  reason: "Persistence defects outside the seat-definition scope; #22902 forbids code and the PD filed them as a dependent planning task."
  owner: "program-director"
  original_acceptance_items:
    - 1.1.2
```

## D2 Research sandbox profile on researcher (depends: 3.3)
`kind: deferred`

#22899 adds `sandbox_profile` to `AgentDefinitionBody` and honors it only on
sync-managed bundled rows. Once that field exists, `researcher.yaml` gains
`sandbox_profile: research` and a non-empty `workflows.rules` list (its Decision
6 guard), and the seat contract test asserts it. Until then the key would be
silently ignored and would fail the 3.5 "no key outside the model" check.

```yaml
deferral:
  task_ref: "TBD-after-22899"
  reason: "External prerequisite: the sandbox_profile field and guards are delivered by the #22899 plan's leaves."
  owner: "program-director"
  original_acceptance_items:
    - 3.3.4
```

## V1: Verification
`kind: verification`

Run after the final edit of each leaf and again before the PD lands the branch:

```bash
GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/test_agent_definitions_v2.py tests/workflows/test_agent_models.py tests/workflows/test_workflows_agent_definitions.py tests/workflows/test_seat_definitions.py tests/workflows/test_seat_rules.py tests/workflows/test_default_agent_role_contract.py tests/workflows/test_retired_bundled_definitions.py tests/agents/test_agents_sync.py tests/agents/test_discovery_agents.py tests/agents/test_plan_adversary_loads_plan_review.py tests/agents/test_plan_adversary_manifest.py tests/agents/test_plan_adversary_no_edits_on_reject.py tests/agents/test_plan_adversary_self_check.py tests/agents/test_plan_adversary_internal_research_definition.py tests/skills/test_plan_adversary_rejection.py tests/skills/test_plan_skill_grammar.py tests/dispatch tests/build_pipeline tests/tasks/test_expansion_service_compile_plan_12725.py -q
uv run ruff format --check src/ && uv run ruff check src/ && uv run mypy src/
uv run gobby plans validate .gobby/plans/agent-definition-profiles.md -p /Users/josh/Projects/gobby
```

Live check after `reload_cache` from the main checkout: `gobby agents show
plan-writer` prints the seat row with `version: "1.0"`; the Plan Writer session
calls `apply_persona(agent="plan-writer")` and on its next turn sees the seat
prompt plus the shared seat guidance once; a deliberate
`spawn_agent(agent="default")` from that session is blocked by
`plan-writer-enhancer-only`. Do not run the full pytest suite. No daemon restart
is required; if one is, it is announced globally before and after.
