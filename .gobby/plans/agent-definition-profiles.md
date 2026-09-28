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
   runbook, or its model. The catalogue is therefore the twenty-two roster
   rows of 2026-09-27, which resolve to ten definitions: `assistant`,
   `program-director`, `lane-manager`, `developer` (the nine lane panes:
   lanes 1, 2, 3, 4, 5, 7, 8, 9 and rust-migration), `code-reviewer` (four
   rows), `researcher` (two rows), `archivist`, `log-monitor`, `plan-writer`,
   `plan-adversary`. `design-lead.md` is on disk but off the roster, so it
   is outside the catalogue; the Assistant owns its cleanup.
   Every seat carries tag `seat`. That tag is a template catalogue marker read
   by the 3.5 file-based test only: `sync_bundled_agents` writes installed
   rows with `tags=["gobby"]` and never propagates YAML tags, and no runtime
   consumer needs a seat tag on the row. There is no analyst seat: no pane
   runs one.
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
6. **Spawn and launch policy is enforced by rules.** All seats: block
   `gobby-agents:spawn_agent` and `gobby-agents:dispatch_batch`
   (`dispatch_batch` retires with `gobby build`, and leaves the list when
   the tool is deleted). #22895 (Josh, 2026-09-26) replaced the proposed
   `deploy_runbook` API family with ordinary pipelines tagged `runbook`, so
   no runbook tool is listed. Ordinary pipeline launch is explicit policy
   here: a pipeline's `spawn_agent` and `exec` steps run in the daemon's
   pipeline executor, outside the launching seat's `before_tool` rules, so
   a seat that launched a pipeline would bypass the spawn block. Every seat,
   the Program Director included with no carve-out, is therefore blocked from `gobby-workflows:run_pipeline` and from the
   exposed `gobby-workflows:pipeline:<name>` tools, and every seat's shell is
   blocked from `gobby agents spawn` and `gobby pipelines run`. Runbooks
   launch from Josh's CLI, web, gclient or cron surfaces (#22895). Plan Writer
   exception: `agent == "plan-enhancer-taskless"`, `isolation == "none"`, at most
   one pass per claimed planning task, tracked as a per-task ledger of task
   UUIDs in session variables (2.2), never as a session boolean: reclaiming
   the same task, `create_task(claim=true)`, a failed spawn, compaction, and
   a `/clear` successor all leave the ledger as it is (the successor
   inherits both ledgers with the claim, 2.2); only a claim of a different
   planning task makes a new pass eligible. The exception is the Plan Writer's alone: no
   other seat holds an enhancer or launch privilege, and Lane 7's earlier
   one-off enhancer pass is consumed history that grants nothing.
   `_common.md` names the exception. This is the
   explicit spawn authorization Josh's proposed flow needs; until it lands, no
   seat spawns.
7. **Step workflows authored for seats with a fixed loop or required skills:**
   `developer`, `plan-writer`, `plan-adversary`, `code-reviewer`,
   `log-monitor`, and `researcher`. `required_skills` are enforced only by a
   step gate (`dispatch/skill_composition.py:80` reads them from
   `step_workflow.variables`), so every seat that declares them opens with a
   `load_skills` step; the researcher's workflow is `load_skills` → `serve`
   and nothing more. Message-driven seats without required skills (assistant,
   program-director, lane-manager, archivist) carry no `step_workflow`; a
   single "serve" step adds nothing there. Interactive execution of step
   instances is #22903's.
8. **Tool restrictions are declared on the definition** (`blocked_tools`,
   `blocked_mcp_tools`). As-is they apply to spawned sessions only; #22903 applies
   them at activation. Read-only seats (`lane-manager`, `code-reviewer`,
   `log-monitor`, `researcher`, `plan-adversary`) use the runbooks list:
   `Edit`, `KillShell`, `MultiEdit`, `NotebookEdit`, `Write`, `apply_patch`,
   `edit_file`, `notebook_edit`, `replace`, `write_file`. Seats that write a
   bounded set of paths (`assistant`: `docs/` and `.gobby/roles/`;
   `archivist`: the desktop digest file) carry no write block at all:
   `_check_agent_tool_enforcement` applies `blocked_tools` unconditionally
   with no path exception, so a block plus a prose carve-out cannot both hold.
   Their write scope is a path-aware `before_tool` rule in the `roles` group
   (2.2) that blocks writes outside the allowed prefixes, the mechanism
   `worker-safety/block-docker-policy-edits.yaml` already uses
   (`canonical_tool_kind`, `canonical_file_paths`, a registered path helper).
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
13. **Rollout is a PD-owned restart, then sync, then activation.** 1.1 and
    3.2 change imported Python (`AgentDefinitionBody`, dispatch and expansion
    routing); `gobby-workflows:reload_cache` re-syncs definition rows and
    clears the pipeline cache but reloads no module, so a running daemon
    would keep dropping `version`, accepting `skills`, and routing to the
    deleted names. After the branch lands, the PD announces and runs the
    restart from the main checkout (global notice before and after, outside
    quiet hours), inspects the sync result for errors and shadowed rows, and
    only then do seats activate (4.1). `reload_cache` alone is enough for
    later data-only changes to seat YAML or rules.
14. **Josh's plan flow.** Encoded in `plan-writer.yaml` and
    `plan-adversary.yaml` as Josh restated it on 2026-09-26 (memory
    55b8c14e, confirmed by the PD on 2026-09-27): the Writer drafts and
    validates, spawns the one bounded enhancer pass (Decision 6) and
    presents its edits to the PD; the PD decides whether to implement each
    or put the product decision to Josh; the Writer edits and passes the
    candidate to the static-pane Adversary; the Adversary sends findings
    and the two reach consensus over `gobby-agents:send_message`, sending
    the PD any disagreement they cannot resolve; at consensus the Writer
    commits one dated consensus entry under V1 and sends the Adversary the
    SHA; the Adversary stamps M1 from those committed bytes with
    `gobby-plans:derive_plan_handoff_manifest` (complete routing decisions)
    and `gobby-plans:apply_plan_handoff_manifest` (the exact returned
    `source_plan_hash`, `rendered_plan_hash` and `manifest_digest`), confirms
    `gobby plans validate --mode expansion`, and sends the Writer and PD the
    digest and hashes; the claim holder commits the rendered bytes
    unchanged; the PD reviews and presents the plan to Josh through the
    Assistant; expansion follows only Josh's approval. This is the sequence
    the Adversary ran for #22904 on 2026-09-27, with no spawned run and no
    evidence binding. Any plan edit after derivation invalidates the hashes,
    so the Adversary derives again. Nobody hand-edits M1 (coverage contract,
    Manifest-on-Approval). The flow has no numbered review rounds: the
    evidence-round tools (`prepare_plan_review_round`,
    `derive_plan_review_manifest`, `append_plan_changelog_round`,
    `finalize_plan_review_evidence`, `apply_plan_review_manifest`) belong
    to the spawned `plan-adversary-taskless` reviewer and appear in neither
    seat.

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
- Production `.py` files stay under 1,000 lines. `condition_helpers.py` (995)
  cannot take the 2.2 helper, which lives in a new module;
  `safe_evaluator.py` (865) gains two registration lines and stays under the
  ceiling; `storage/definitions/agents.py` (740), `agent_models.py` (219),
  `sync.py` (299), and `skill_composition.py` (133) are clear.
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
- `src/gobby/storage/definitions/agents.py::parent_body`
- `tests/workflows/test_agent_definitions_v2.py::*` — scope-reason: cover the rejected key and stored version
- `tests/agents/test_agents_sync.py::*` — scope-reason: cover a stored body that still carries the retired skills map

**Granularity:** thirteen Target files, but one behavior: the schema change,
the ten YAML edits it forces, and the stored-body read path are one commit
and one test run; splitting the YAML edits would leave the bundle failing
validation between leaves, and splitting the read path would leave every
installed row unreadable between commits.

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

Stored rows: every pre-change `definition_json` carries the serialized default
`skills: {}` (`default_factory=dict`), and the ten bundled rows carry the
prose maps, so rejecting the key on the model alone would make every installed
row fail validation on read. `storage/definitions/agents.py::parent_body`
(`:78`) is the single read path that strips step-body keys before validation;
it also drops a `skills` key whose value is an empty map, so pre-change rows
resolve unchanged. A stored non-empty map is not hidden: before the schema
change lands, the leaf inventories rows outside bundled ownership that carry
one (the storage query over `definition_json` the executor confirms against
the JSONB column) and rewrites them through the storage update path with the
map removed; bundled rows are refreshed by sync drift on the first sync after
the YAML edits. YAML input (`sync.py` bundled path and
`validate_workflow_definition_data` import path) never passes through
`parent_body`, so any `skills` key on input is rejected.

Verification planned: `GOBBY_TEST_PROTECT=1 uv run pytest
tests/workflows/test_agent_definitions_v2.py tests/workflows/test_agent_models.py
tests/agents/test_agents_sync.py -q`; `uv run ruff check`, `uv run mypy` on the
changed files.

Consumers unchanged:
- `src/gobby/agents/sync.py` — no-edit-reason: it validates each bundled YAML as `AgentDefinitionBody` first (`:180`), which now rejects the key, and only then strips the dumped body through `parent_body`; its stored-row drift comparison (`:89`, `:104`) gains the empty-map drop for free.
- `src/gobby/workflows/imports.py` — no-edit-reason: it validates the imported body first (`:68`), so an imported `skills` key is rejected, and then strips the dumped body through `parent_body` unchanged.

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
- 1.1.4 - A stored body carrying `skills: {}` resolves after the change, and a
  stored non-empty map is surfaced by the inventory, not silently dropped.
  symbol: `src/gobby/storage/definitions/agents.py::parent_body`. test:
  `tests/agents/test_agents_sync.py::test_stored_body_with_empty_skills_map_resolves`.

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

### 2.2 Seat spawn and write policy with the Plan Writer enhancer exception [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/rules/roles/seat-spawn-policy.yaml`
- `src/gobby/install/shared/workflows/rules/roles/seat-write-scope.yaml`
- `src/gobby/workflows/condition_helpers_paths.py`
- `src/gobby/workflows/safe_evaluator.py::*` — scope-reason: register the write-scope path helpers in the condition namespace beside touches_docker_policy_path
- `src/gobby/hooks/event_handlers/_session_start/claims.py::preserve_task_claim_state`
- `.gobby/roles/_common.md`
- `tests/workflows/test_seat_rules.py`
- `tests/workflows/test_condition_helpers_paths.py`
- `tests/hooks/test_session_start_claims.py::*` — scope-reason: add the Plan Writer ledger successor-transfer test

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
and the same rule tests membership with `in`). No bundled rule blocks
`spawn_agent` for interactive sessions today; only
`worker-safety/no-agent-spawn-for-merge.yaml` (`agent_scope: [merge]`).

Ledger facts: `claim_task` answers a task the session already holds with
`already_claimed: True` (`mcp_proxy/tools/tasks/_lifecycle_claim.py:249`) and
`create_task(claim=true)` never passes through `claim_task`, so a claim event
cannot safely re-arm a boolean. The bound is two session-variable ledgers of
task ids, `plan_writer_planning_tasks` (planning tasks this session claimed)
and `plan_writer_enhancer_tasks` (planning tasks whose pass is spent); both
persist with the other session variables across compaction. Eligibility is a
claimed planning task present in the first ledger and absent from the second.
A `/clear` successor receives only what `preserve_task_claim_state`
(`hooks/event_handlers/_session_start/claims.py:53`) copies, today
`task_claimed` and `claimed_tasks`, so a cleared Plan Writer would keep its
claim, lose the spent ledger, and re-record the task on its next
`already_claimed` reclaim. This leaf extends that function to carry both
ledgers from the predecessor's variables onto the successor, merged as a
set union with any ledger the successor already holds, whether or not a
claim transfers. The one function serves every clear path (terminal session
start through `_session_start/materialize.py` and web chat through
`servers/websocket/chat/_session.py`). The authorization therefore lives as
long as the claim chain on the task. A fresh session that is no successor
starts with empty ledgers and can hold the task only after the prior claim
is released; that case is the stated boundary of this bound.

Write-scope facts: before-tool normalization annotates every write with
`event.data['canonical_tool_kind'] == 'write'`,
`event.data['canonical_file_paths']` (empty when the target is opaque), and
`event.data['canonical_repo_mutation']`
(`hooks/_normalization_canonical.py:898-945`,
`hooks/_path_scope.py::apply_path_scope_metadata`).
`worker-safety/block-docker-policy-edits.yaml` is the pattern: it fails closed
when `canonical_file_paths` is empty and otherwise calls the registered helper
`touches_docker_policy_path(event.data, tool_input)`
(`workflows/condition_helpers.py:198`, registered at `safe_evaluator.py:646`).
`condition_helpers.py` is 995 lines, so the new helpers live in a new module
`workflows/condition_helpers_paths.py`. Both take every path from
`condition_helpers._event_and_tool_paths`, resolve it against
`current_tool_cwd` and `current_project_root` from `hooks/_path_scope.py`
with `Path.resolve(strict=False)` (so `..` segments and symlinks resolve
before any check), and return False for an empty path set:
- `write_paths_within(event_data, tool_input, dirs) -> bool` is directory
  containment: True only when every resolved path equals or descends from
  one resolved directory in `dirs` (component-wise `Path.is_relative_to`,
  never a string prefix, so `docs-other/` is outside `docs/`; a relative
  directory is project-relative).
- `write_paths_match(event_data, tool_input, pattern) -> bool` is an exact
  file match: True only when every resolved absolute path fullmatches the
  regex `pattern`.

Size: `safe_evaluator.py` is 865 lines, so this leaf does not grow it. Move
the path-helper registration out of `safe_evaluator.py`: the new module
`src/gobby/workflows/condition_helpers_paths.py` exports
`PATH_CONDITION_HELPERS = {"touches_docker_policy_path": ...,
"write_paths_within": ..., "write_paths_match": ...}`, `safe_evaluator.py` drops its direct
`touches_docker_policy_path` import and dict entry (`:566`, `:646`) and
merges `**PATH_CONDITION_HELPERS` into the namespace in their place, so the
file ends the leaf no longer than it starts.

Rules in `seat-spawn-policy.yaml`, tags `[roles, seat, enforcement, gobby, default]`:

- `seat-no-spawn`: `event: before_tool`, priority 10, `when:` seat match (2.1
  condition) and seat is not `plan-writer`; effect `block` with `mcp_tools:
  ["gobby-agents:spawn_agent", "gobby-agents:dispatch_batch"]`
  (`dispatch_batch` is dropped from the list when it retires with `gobby
  build`), reason: seats do not spawn; the automated close reviewer is the
  only spawn path; send work to the PD.
- `seat-no-pipeline-launch`: `event: before_tool`, priority 10, `when:` seat
  match (every seat, the Plan Writer included) and
  `tool_input.get('server_name') == 'gobby-workflows'` and
  (`tool_input.get('tool_name') == 'run_pipeline'` or
  `str(tool_input.get('tool_name') or '').startswith('pipeline:')`); effect
  `block` with no tool filter, so the `when:` alone selects the call. The
  condition is needed because `block` `mcp_tools` matching is exact or
  `server:*` only (`engine/effects.py:390-399`), and the exposed tools are
  named `pipeline:<name>` (`_pipeline_exposed.py:71`). Reason: a pipeline's
  steps run outside seat rules; runbooks launch from Josh's CLI, web,
  gclient or cron surfaces; send the request to the PD.
- `seat-no-launch-cli`: `event: before_tool`, priority 10, `when:` seat match;
  effect `block` on the shell tools with a `command_pattern` matching
  `gobby agents spawn` and `gobby pipelines run` (optionally behind `uv
  run`), in the prefix grammar of
  `task-enforcement/block-gobby-tasks-cli.yaml`; same reason.
- `plan-writer-track-claim`: `event: after_tool`, `when:` seat is
  `plan-writer`, `event.data.get('mcp_server') == 'gobby-tasks'`,
  `event.data.get('mcp_tool') in ['claim_task', 'create_task']`, and the
  output reports success; effect `set_variable plan_writer_planning_tasks`
  with an expression that adds the output's task id (`task_id` from
  `claim_task`, `id` from `create_task`) to the ledger when absent. Neither
  payload carries a task category (`_lifecycle_claim.py:249` returns
  `success`, `task_id`, `title`; `_crud.py:361-366` returns `id`, `seq_num`,
  `ref`), so the rule does not filter on one: every task the Plan Writer
  claims is a planning task by seat contract, and the ledger records every
  successful claim. An `already_claimed` reclaim matches the rule and
  changes nothing.
- `plan-writer-enhancer-only`: `event: before_tool`, priority 10, `when:` seat is
  `plan-writer` and not (`arguments.agent == 'plan-enhancer-taskless'` and
  `arguments.isolation == 'none'` and
  `any(t in (variables.get('plan_writer_planning_tasks') or []) and t not in (variables.get('plan_writer_enhancer_tasks') or []) for t in (variables.get('claimed_tasks') or {}))`);
  effect `block` on `gobby-agents:spawn_agent` and
  `gobby-agents:dispatch_batch`, reason naming the exact allowed call and
  that one pass per claimed planning task is permitted.
- `plan-writer-enhancer-consumed`: `event: after_tool`, `when:` seat is
  `plan-writer` and `event.data.get('mcp_server') == 'gobby-agents'` and
  `event.data.get('mcp_tool') == 'spawn_agent'` and the output reports success;
  effect `set_variable plan_writer_enhancer_tasks` with an expression that adds
  every currently claimed id that is in `plan_writer_planning_tasks`. A failed
  spawn changes nothing. There is no re-arm rule: a consumed id never becomes
  eligible again, and only a claim of a different planning task admits a pass.

The ledger expressions run in `safe_evaluator`: the executor uses list
concatenation if the evaluator accepts it and otherwise a comma-joined string
with `split(',')` membership, and pins the chosen form in the tests.

Rules in `seat-write-scope.yaml`, same tags, `event: before_tool`, priority 15:

- `assistant-write-scope`: `when:` seat is `assistant` and
  `event.data.get('canonical_tool_kind') == 'write'` and
  `not write_paths_within(event.data, tool_input, ['docs/', '.gobby/roles/'])`;
  effect `block`, reason: the Assistant writes only under `docs/` and
  `.gobby/roles/`; name the target with a literal path.
- `archivist-write-scope`: same shape for `archivist` with
  `not write_paths_match(event.data, tool_input,
  r'/Users/josh/Desktop/gobby-digest-\d{4}-\d{2}-\d{2}\.md')`; reason:
  the Archivist writes only the dated desktop digest.

Tests with the real engine as 2.1 does: the enhancer bound covers a reclaim
that returns `already_claimed`, `create_task(claim=true)` of the same task, a
failed spawn, a `compact` session_start, a spawn followed by a `/clear`
successor that reclaims the same task and is refused, and a claim of
a second planning task; the write scope covers one allowed and one blocked
write per seat and an opaque write with no path.
`test_session_start_claims.py` pins the successor transfer: both ledgers
reach the successor, merge with an existing successor ledger, and transfer
when no claim does.

Consumers unchanged:
- `src/gobby/hooks/event_handlers/_session_start/materialize.py` — no-edit-reason: it calls `preserve_task_claim_state` with the predecessor's variables and gains the ledger transfer with no signature change.
- `src/gobby/servers/websocket/chat/_session.py` — no-edit-reason: the web-chat clear path calls the same function unchanged and gains the transfer.
- `tests/servers/websocket/chat/test_clear_session.py` — no-edit-reason: it asserts claim transfer, which is unchanged; the ledger keys are absent in its fixtures.

`_common.md` third bullet becomes: "Do not spawn agents or launch
pipelines. The automated task-close reviewer and the Plan Writer's single
`plan-enhancer-taskless` pass per plan, one per claimed planning task (Josh,
2026-09-26; rule `plan-writer-enhancer-only`), are the only permitted spawn
paths."

**Acceptance:**

- 2.2.1 - A non-writer seat's `spawn_agent` and `dispatch_batch` calls are
  blocked with the seat reason; every seat, the Plan Writer included, is
  blocked from `gobby-workflows:run_pipeline`, an exposed
  `gobby-workflows:pipeline:<name>` tool, and a shell `gobby agents spawn`
  or `uv run gobby pipelines run`, while `gobby-workflows:set_variable`
  stays allowed. file:
  `src/gobby/install/shared/workflows/rules/roles/seat-spawn-policy.yaml`. test:
  `tests/workflows/test_seat_rules.py::test_seats_cannot_spawn`.
- 2.2.2 - The Plan Writer may spawn `plan-enhancer-taskless` with `isolation:
  none` once per claimed planning task: a reclaim answered with
  `already_claimed`, a `create_task(claim=true)` of the same task, a failed
  spawn, a compact, and a `/clear` successor leave the ledgers unchanged; a
  second attempt for the same task (including from a `/clear` successor
  that reclaims it), a different agent, another isolation, or no claimed
  task is blocked; claiming a different planning task admits
  exactly one more pass. test:
  `tests/workflows/test_seat_rules.py::test_plan_writer_enhancer_pass_is_per_task`.
- 2.2.3 - The shared role rules name the exception. behavior:
  "plan-writer-enhancer-only" in `.gobby/roles/_common.md`.
- 2.2.4 - The Assistant's write under `docs/` is allowed and under `src/`,
  `docs-other/`, or `docs/../src/` blocked; the Archivist's write to
  `/Users/josh/Desktop/gobby-digest-2026-09-27.md` is allowed, and an
  adjacent desktop file, an undated or suffixed digest name, and any write
  inside the checkout are blocked; a write with no resolvable path is
  blocked for both. file:
  `src/gobby/install/shared/workflows/rules/roles/seat-write-scope.yaml`. test:
  `tests/workflows/test_seat_rules.py::test_seat_write_scope_is_path_aware`.
- 2.2.5 - `write_paths_within` is component-wise directory containment and
  `write_paths_match` an exact fullmatch, both after resolving relative
  paths, `..` traversal, and symlinks against the project root and tool
  cwd; a symlink under an allowed directory that resolves outside it is
  refused, and both are callable from a rule condition. symbol:
  `src/gobby/workflows/condition_helpers_paths.py::write_paths_within`. test:
  `tests/workflows/test_condition_helpers_paths.py::test_write_path_helpers_resolve_before_matching`.
- 2.2.6 - A `/clear` successor inherits `plan_writer_planning_tasks` and
  `plan_writer_enhancer_tasks` from its predecessor. symbol:
  `src/gobby/hooks/event_handlers/_session_start/claims.py::preserve_task_claim_state`.
  test:
  `tests/hooks/test_session_start_claims.py::test_clear_successor_inherits_plan_writer_ledgers`.

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
roster and task state).

Step state: transitions read the merged variables with instance variables
last (`engine/enforcement_completion.py::_process_step_after_tool`), so a flag
declared in `step_workflow.variables` can be flipped only by
`gobby-workflows:set_variable(scope="step")`
(`mcp_proxy/tools/workflows/_variables.py:84`) or by a step handler; the
top-level proxy `set_variable` is session-scoped (`stdio_tools.py:338`) and is
ignored for declared instance names
(`tests/workflows/test_step_runtime_transitions.py::test_native_set_variable_does_not_shadow_workflow_local_variable`).
Every seat prompt therefore names `gobby-workflows:set_variable(scope="step")`
for the flags it sets by hand, every step whose flags the seat sets lists
`gobby-workflows:set_variable` in its MCP allowlist, and `on_mcp_success`
handlers (`WorkflowStep.on_mcp_success`, `definitions.py:586`; entries
`{server, tool, when?, action: set_variable, variable, value}` evaluated with
`vars`, `tool_input`, `tool_output`, where `tool_input` carries the MCP
arguments promoted to its top level by
`enforcement_checks.py::_step_handler_tool_input`, so
`tool_input.get('content')` reads a `send_message` argument directly) live on
the step whose allowlist carries the tool they watch. Every seat with `required_skills` opens with a
`load_skills` step (allowed MCP: `gobby-skills:get_skill`,
`gobby-skills:get_skill_file`; transition when
`all(skill_loaded(skill) for skill in vars.required_skills)`, the
`backend-developer.yaml:174-217` gate) because the required set is enforced
only by a step gate (Decision 7). Every loop seat carries a correlation
variable (the task, candidate, window, or evidence it is working) and resets
its flags in the handler of the call that ends the unit of work, so two
consecutive units run through the loop without a manual reset; 3.5 pins that
with engine tests.

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
verbatim; half-hour status per lane; relays each installed watchdog
`ALARM[load]` (one-minute load above 24) to the PD once; heavy-slot
admission holds at five-minute load 24; keeps the roster and role files
current; read-only DB helper; one-line Telegram confirmation),
`program-director.md` (coordination only, code only to close gaps in lane
candidates; files and delegates tasks; reviews candidates with the Code
Reviewer, lands, restarts and cuts over with notices; +0.0.1 crate patch
bump with `Cargo.lock` on every binary release; confirms each merge and
removes only clean, merged, inactive worktrees; updates the Archivist on
every land, bounce, reroute, park, task, restart; drives the planning queue
through Josh's plan flow (Decision 14); answers Josh in its own session;
decisions as buttons via the Assistant), `lane-manager.md` (routes
PD-assigned work in PD order; heavy-slot admission below five-minute load
24; ACKs HOLD/RESUME; one `wait_for_coordination` idle subscription per
active lane; `send_message(wake=true)` for actions; event lines to the
Assistant `LANE= EVENT=STARTED|CANDIDATE|BOUNCE|CLOSED TASK=#NNNNN
TASK_TITLE= RUN= WT= COMMIT= NOTE=`; verdict blockers to the PD; no
review, land, restart, or code).
Selectors: all three add `tag:worker-safety` exclusions that block interactive
destructive shell (`no-destructive-shell-interactive`) stay via `tag:default`;
program-director additionally includes `name:no-force-push-interactive`,
`name:no-destructive-git-interactive`; lane-manager carries the Decision 8
read-only `blocked_tools` list; assistant carries no write block, and its
`docs/` and `.gobby/roles/` scope is enforced by `assistant-write-scope`
(2.2), which the prompt names. Continuity: compact, never clear. No
`step_workflow` and no `required_skills` (Decision 7).

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
- `docs/contracts/plan-coverage.md`
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

**Research context:** Nine roster sessions (lanes 1, 2, 3, 4, 5, 7, 8, 9 and
rust-migration) share one flow: claim → implement → validate → commit →
submit to the PD and the assigned Reviewer; the lane's
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
skills need no new gate. The seat reorders the shared workflow so the
required gate comes first, as every seat with `required_skills` does
(3.5): `load_required_skills` becomes the opening `load_skills` step, and
a `route_skills` step sits between `claim` and `load_additional_skills`
(allowed MCP: `gobby-tasks:get_task`,
`gobby-workflows:set_variable`) has the seat read the claimed task, map its
paths and `implementation_domain` through the prompt's routing table, merge
any skills the task description names, write `additional_skills` with
`gobby-workflows:set_variable(scope="step")`, then set `skills_routed`; its
`on_mcp_success` for `gobby-tasks:get_task` with `when:
tool_output.get('id') == vars.assigned_task_id` binds `assigned_task_ref`
from `tool_output.get('ref')`, the `#NNNNN` form the event line carries
(`task_summary_payload`, `tasks/_formatters.py:147-149`, returns `ref`,
`id`, and `seq_num` for the brief card), because `claim_task` returns only
the UUID;
transition to `load_additional_skills` when `vars.skills_routed and
vars.assigned_task_ref`. Routing table
in the prompt: task touches `web/` → `impeccable` (the design contract in
`AGENTS.md` requires it) and `typescript`; touches `crates/` → `rust`
(`AGENTS.md` requires it before editing Rust); Python only → nothing beyond
the required set; docs only → `tech-writer`. All four exist under the bundled
skills directory.

`developer.yaml` is `backend-developer.yaml` with: `name: developer`, `tags:
[gobby, seat]`, Decision 12 provider, model, and isolation, `rule_selectors.include:
["tag:default", "tag:worker-safety", "tag:roles"]`, one prompt text for both
surfaces (lane purpose; lane identity from the role file and PD or Lane Manager
messages; the flow claim → implement → validate → commit → submit a CANDIDATE
to the PD and the assigned Reviewer with the exact commit and tests, in the
event-line grammar `LANE= EVENT=CANDIDATE TASK=#NNNNN TASK_TITLE= RUN= WT=
COMMIT= NOTE=`; HOLD/GO discipline; found work on the lane's surface is the
lane's; heavy commands and close reviews only in a Lane Manager slot; no
restarts or binary promotion unless the PD says so; work in the lane's
worktree; the skill routing table; then the
`## Platform Context` and `## Skills` baseline sections and the continuity
policy, clear between tasks), steps `load_skills` → `claim` →
`route_skills` → `load_additional_skills` → `implement` → `submit`, where
`implement` is reachable only through both load gates,
and `step_workflow.variables` extended with `assigned_task_id: null`,
`assigned_task_ref: null`, `skills_routed: false`, `candidate_sent: false`,
`bounced: false`. Loop
wiring: `claim` keeps its `gobby-tasks:claim_task` success handler for
`task_claimed` and adds one that binds `assigned_task_id` from
`tool_output.get('task_id')`, and allows
`gobby-agents:wait_for_coordination` for the idle wait on the PD or Lane
Manager; `submit` (allowed MCP: `send_message`,
`gobby-agents:wait_for_coordination`, `close_task`, `link_commit`,
`gobby-workflows:set_variable`) has an
`on_mcp_success` for `gobby-agents:send_message` with `when:
'EVENT=CANDIDATE' in str(tool_input.get('content')) and
str(vars.assigned_task_ref) in str(tool_input.get('content'))` that sets
`candidate_sent`; a BOUNCE from the reviewer is recorded by the seat as
`bounced` (step scope) and `submit` transitions to `implement` when
`vars.bounced`, where the seat clears `bounced` and `implementation_complete`
before rework; a `gobby-tasks:close_task` success handler resets
`task_claimed`, `implementation_complete`, `additional_skills_loaded`,
`skills_routed`, `candidate_sent`, `bounced`, `assigned_task_id`, and
`assigned_task_ref`, and `submit` transitions to `claim` when
`not vars.task_claimed`. No
`exit_condition`. The claim step keeps the
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
reference texts describe the routing in prose, and
`docs/contracts/plan-coverage.md:507-515` (Category and TDD Policy) maps the
implementation domains to the three names and becomes the single `developer`
mapping. All become `developer`;
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
  skill routing table, and declares the load skills → claim → route skills
  → load additional skills → implement → submit loop whose close-task
  handler resets it to `claim` for the next task. behavior: "EVENT=CANDIDATE" in
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
`step_workflow.variables: {candidate_received: false, verdict_ready: false,
candidate_task: null}`; steps `load_skills` → `await` (allowed MCP:
`send_message`, `gobby-agents:wait_for_coordination`, sessions read,
`gobby-workflows:set_variable`; the seat
records the PD's CANDIDATE line by setting `candidate_task` to its `TASK=`
value and `candidate_received` at step scope; transition to `review` when
`vars.candidate_received`) → `review` (read-only tools plus `gcode` and `git
diff`, `gobby-workflows:set_variable`; transition to `verdict` when
`vars.verdict_ready`) → `verdict` (allowed MCP: `send_message`,
`gobby-workflows:set_variable`; the message carries `EVENT=CANDIDATE_VERDICT
TASK=#NNNNN ... VERDICT=LAND|BOUNCE` with HIGH/MEDIUM/LOW findings to PD and
author lane; `on_mcp_success` for `gobby-agents:send_message` with `when:
'EVENT=CANDIDATE_VERDICT' in str(tool_input.get('content')) and
str(vars.candidate_task) in str(tool_input.get('content'))` resets
`candidate_received`, `verdict_ready`, and `candidate_task`; transition to
`await` when `not vars.candidate_received`). The prompt carries the
role file's check of each merge against the current `0.5.0` head.
Continuity: clear between verdicts.

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
the seat's shape (`inherit`, no model, no claim step). The seat body carries
`required_skills: [research, restraint, brevity]` and therefore the two-step
workflow `load_skills` → `serve` (allowed MCP: research, memory, sessions,
and plans reads plus `send_message` and
`gobby-agents:wait_for_coordination`; no transition out, no `exit_condition`),
the Decision 7 exception.
`trajectory-monitor.yaml` is a review-stage reviewer, not a log monitor.
Role text: `archivist.md` (sole writer of
`/Users/josh/Desktop/gobby-digest-<date>.md` as a standing duty with no held
task; "Lane queues" lists only active and parked queues from PD updates;
carries forward unresolved context; load and daemon status only from PD
evidence; flags gaps to PD), `monitor.md` (runs `gobby:log-monitor-tick` every 10
minutes; "Systems nominal" or `EVENT=ALARM` with evidence to PD, copy to
Assistant; performance verdicts only from matched windows; nominal line per
runbooks: `Systems nominal | window HH:MM-HH:MM | <N> warnings in <K> families,
all mapped`), `researcher.md` (research only; findings to PD; decisions for Josh
via the Assistant; asks PD before killing processes). Selectors: archivist and
log-monitor use `tag:roles`, `tag:context-handoff`, `tag:memory-lifecycle`,
`tag:worker-safety`, and `tag:default`; researcher adds
`tag:task-skill-gates`. code-reviewer, log-monitor, and researcher carry the
Decision 8 read-only list; archivist carries no write block, and its desktop
digest scope is enforced by `archivist-write-scope` (2.2), which the prompt
names. `required_skills`: log-monitor
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
`step_workflow.variables: {tick_done: false, tick_window: null}`;
`load_skills` → `tick` (allowed MCP: skills, sessions read,
`gobby-agents:wait_for_coordination`, `gobby-workflows:set_variable`; the
cadence is `wait_for_coordination(owner_session=<PD>, reply=true,
timeout=600)`, whose expiry starts the next tick and whose PD reply starts
it early; the seat sets `tick_window` to the
`HH:MM-HH:MM` window it examined and `tick_done`, both at step scope;
transition to `report` when `vars.tick_done`) → `report` (allowed MCP:
`send_message`, `gobby-agents:wait_for_coordination`,
`gobby-workflows:set_variable`; `on_mcp_success` for
`gobby-agents:send_message` with `when: 'Systems nominal' in
str(tool_input.get('content')) or 'EVENT=ALARM' in
str(tool_input.get('content'))` resets `tick_done` and `tick_window`;
transition to `tick` when `not vars.tick_done`), no exit; a status reply to
the PD that is neither line leaves the step where it is. Continuity: archivist and log-monitor compact,
never clear; researcher and code-reviewer clear after each report or verdict.
`sandbox_profile: research` on `researcher` is deferred (D2) until #22899's
field lands.

**Acceptance:**

- 3.3.1 - `code-reviewer` validates, is `seat`-tagged, read-only, names only
  bundled skills (`code-review`, `restraint`), and declares the await → review
  → verdict loop with the verdict grammar. file:
  `src/gobby/install/shared/workflows/agents/code-reviewer.yaml`.
- 3.3.2 - `archivist` validates, is `seat`-tagged, carries no write block,
  names `archivist-write-scope`, and carries the digest contract. file:
  `src/gobby/install/shared/workflows/agents/archivist.yaml`.
- 3.3.3 - `log-monitor` validates, is `seat`-tagged, declares the tick → report
  loop, carries the nominal and ALARM grammar, and names no
  `gobby:log-monitor-tick` skill. behavior: "Systems nominal | window" in
  `src/gobby/install/shared/workflows/agents/log-monitor.yaml`.
- 3.3.4 - `researcher` validates as a seat (`seat`-tagged, read-only, inherited
  provider, `load_skills` → `serve`, no claim step), and the discovery-agent
  tests no longer list it.
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
- `tests/mcp_proxy/test_stage_review_schema.py::*` — scope-reason: keep both tests; only the final YAML text-parity check reads the seat body
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
wiring that still holds: `load_skills` targets
`gobby:references/plan/review.md` and `proportionality` before review,
rejection is findings-only with no plan-file edits, plan edits are the
Writer's, `apply_plan_review_repairs` is blocked, the identity guard
precedes manifest derivation, and `kill_agent` is absent. It drops the
spawned-run evidence contract (Decision 14): its only plan mutation is M1
through the handoff-manifest tools in its `stamp` step. Retargets by module
(the seat has steps `load_skills` → `await` → `review` → `stamp`;
`plan-adversary-taskless` has `load_skill` → `review` with per-skill
`*_loaded` variables and no `terminate`). One rule settles every
prompt-contract test not named below: a test whose asserted phrase names a
spawned-run or evidence-round artifact (`approve_review`, `round_result`,
`evidence_id`, `derive_plan_review_manifest`, `apply_plan_review_manifest`,
`validate_plan_file`, yolo, needs-human or force-approve paths) is deleted
with the stage body (ruling 6102cd1d), and any other runs against the seat
body.

- `test_plan_adversary_loads_plan_review.py`: the eight `load_skill` tests
  (`test_has_load_skill_step_between_claim_and_review` through
  `test_transition_gates_on_both_skills_loaded`) retarget to
  `plan-adversary-taskless`, and the seat's `load_skills` gate is covered by
  3.5; `test_claim_step_uses_normal_delegated_claim`,
  `test_claim_step_treats_closed_assigned_task_as_claim_complete`,
  `test_review_step_completes_on_closed_task_review_error`,
  `test_terminate_step_only_allows_end_agent_run`, and
  `test_review_step_leaves_end_agent_run_to_engine_gate` are deleted (no seat
  or taskless equivalent); `test_review_step_completes_on_review_rejection`
  retargets to `plan-adversary-taskless`; the remaining prompt-contract
  tests follow the rule above.
- `test_plan_adversary_manifest.py`:
  `test_staged_verdict_proceeds_directly_to_normal_agent_completion`,
  `test_instructions_describe_typed_manifest_handoff_on_approval`,
  `test_instructions_require_canonical_round_result`,
  `test_manifest_emission_precedes_review_approval`,
  `test_instructions_delegate_manifest_application`, and
  `test_review_step_does_not_block_review_approval` are deleted;
  `test_review_step_blocks_coordinator_owned_plan_writes` reads the seat's
  `review` step and asserts it blocks `apply_plan_review_manifest`,
  `derive_plan_handoff_manifest`, `apply_plan_handoff_manifest`,
  `finalize_plan_review_evidence`, and
  `checkpoint_plan_review_lesson_mint`; the two identity-guard tests assert
  the guard precedes `derive_plan_handoff_manifest`;
  `test_instructions_forbid_direct_plan_edits`,
  `test_repairs_are_advisory_to_the_planner`, and
  `test_apply_plan_review_repairs_is_blocked` run against the seat body.
- `test_plan_adversary_self_check.py`: `test_retry_capped_at_three` runs
  against the seat body; `test_self_check_delegates_to_manifest_apply`
  becomes a seat test that the prompt applies M1 only with
  `apply_plan_handoff_manifest` and the returned hashes and says "Never
  edit the plan file"; `test_pre_verdict_parsing_delegated_upstream` and
  the five yolo, needs-human, and force-approve tests are deleted.
- `test_plan_adversary_internal_research_definition.py`:
  `test_adversary_agents_pin_the_reviewer_model` covers
  `plan-adversary-taskless` only; every other test keeps both names.
- `test_stage_review_schema.py`: keep `test_reject_review_uses_shared_finding_schema`
  and `test_finding_schema_parity_with_adversary_contracts`, changing only the
  final YAML text-parity check to read the seat body.
- `test_plan_adversary_no_edits_on_reject.py`,
  `test_plan_adversary_rejection.py`, `test_plan_skill_grammar.py`: run against
  the seat body with only the loader changed, under the rule above.
- `test_discovery_agents.py:210` and `test_workflows_agent_definitions.py:151`
  read the seat body (inherited provider, no model).
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
gobby:references/plan/coverage.md]`; `step_workflow.variables: {plan_task:
null, draft_validated: false, enhancement_reviewed: false, candidate_sent:
false, consensus: false}`, each set by the seat at step scope unless a
handler is named below; steps `load_skills` → `draft` (allowed MCP: tasks,
plans, `gobby-workflows:set_variable`; `on_mcp_success` for
`gobby-tasks:claim_task` binds `plan_task` from `tool_output.get('task_id')`;
the seat sets `draft_validated` after `uv run gobby plans validate` passes;
transition to `enhance` when `vars.draft_validated`) → `enhance` (allowed MCP
includes `gobby-agents:spawn_agent` guarded by 2.2, `wait_for_agent`,
`gobby-agents:wait_for_coordination`, and `send_message`; the seat presents the enhancer's edits to the PD and sets
`enhancement_reviewed` when the PD's dispositions arrive; transition to
`revise` when `vars.enhancement_reviewed`) → `revise` (allowed MCP: plans,
`gobby-tasks:link_commit`, `send_message`, `gobby-workflows:set_variable`;
the seat applies the PD's dispositions and revalidates; `on_mcp_success`
for `gobby-agents:send_message` with `when: 'EVENT=CANDIDATE' in
str(tool_input.get('content'))` sets `candidate_sent`; transition to
`adversary` when `vars.candidate_sent`) → `adversary` (allowed MCP:
`send_message`, `wait_for_coordination`, plans read, `link_commit`,
`gobby-workflows:set_variable`; the seat edits for each finding, converses
with the Adversary until consensus, and sends the PD any disagreement they
cannot resolve; at consensus it commits the dated V1 consensus entry, sends
the Adversary the SHA, and sets `consensus`; transition to `handoff` when
`vars.consensus`) → `handoff` (allowed MCP: `send_message`,
`wait_for_coordination`, `gobby-plans:update_plan_hash`, `link_commit`,
`close_task`, `gobby-workflows:set_variable`; the seat commits the
Adversary-applied M1 bytes unchanged after checking the reported hash,
sends the PD the artifact SHA, SHA256 and validation, and waits for the
PD's release; for a registered plan it refreshes the row hash and coverage
manifest with `gobby-plans:update_plan_hash` and commits the manifest
(memory 5538e963: the close reviewer bounces a registered plan whose
manifest still carries the registration-time hash, and
`regenerate_coverage_manifest` refuses until the hash moves), then closes
the task; `on_mcp_success` for `gobby-tasks:close_task` resets every flag
and `plan_task`; transition to `draft` when `not vars.plan_task`). No
`exit_condition`. Continuity: clear between plans. The seat never expands
or dispatches; expansion follows the PD's confirmation of Josh's approval.

`plan-adversary`: read-only; `required_skills: [restraint, proportionality,
gobby:references/plan/review.md, gobby:references/plan/coverage.md]`.
Step enforcement admits every internal read-only MCP tool before it reads
`allowed_mcp_tools`, and only an explicit `blocked_mcp_tools` entry wins
(`_check_step_tool_enforcement_locked`); `derive_plan_handoff_manifest` is
registered `read_only=True` (`plans/review_evidence.py:330-342`), so an
allowlist omission denies nothing. The definition's `blocked_mcp_tools`
therefore adds, beside `gobby-agents:kill_agent`, every evidence-round tool
the seat never uses: `gobby-plans:prepare_plan_review_round`,
`gobby-plans:get_plan_review_snapshot`, `gobby-plans:bind_evidence_run`,
`gobby-plans:derive_plan_review_manifest`,
`gobby-plans:validate_plan_review_coverage`,
`gobby-plans:append_plan_changelog_round`,
`gobby-plans:finalize_plan_review_evidence`,
`gobby-plans:apply_plan_review_manifest`,
`gobby-plans:apply_plan_review_repairs`, and
`gobby-plans:checkpoint_plan_review_lesson_mint`; and `load_skills` and
`await` each carry step `blocked_mcp_tools` with
`gobby-plans:derive_plan_handoff_manifest` and
`gobby-plans:apply_plan_handoff_manifest`, as `review` does;
`step_workflow.variables: {candidate_sha: null, consensus: false}`; steps
`load_skills` → `await` (allowed MCP: `send_message`,
`wait_for_coordination`, plans read, `gobby-workflows:set_variable`; the
seat binds `candidate_sha` to the commit the Writer's candidate message
names; transition to `review` when `vars.candidate_sha`) → `review`
(allowed MCP: `send_message`, `wait_for_coordination`, plans read,
`gobby-workflows:set_variable`; blocked MCP:
`gobby-plans:derive_plan_handoff_manifest` and
`gobby-plans:apply_plan_handoff_manifest`; findings with stable ids,
blocking or nit, category from the review vocabulary, sent to the Writer;
the seat rebinds `candidate_sha` on each revised candidate, sends the PD any
disagreement the two cannot resolve, and sets `consensus` after it verifies
the Writer's committed consensus entry and rebinds `candidate_sha` to that
commit; transition to `stamp` when `vars.consensus`) → `stamp` (allowed
MCP: `gobby-plans:derive_plan_handoff_manifest`,
`gobby-plans:apply_plan_handoff_manifest`, `send_message`,
`gobby-agents:wait_for_coordination`, `gobby-workflows:set_variable`; the
seat waits here for the Writer's commit of the rendered bytes; the handoff
tools are allowed in no other step; the seat verifies the committed bytes' hash and base validation,
derives with complete routing decisions, applies with the exact returned
hashes and digest, runs `uv run gobby plans validate <plan> -p <project>
--mode expansion`, and sends the Writer and PD the manifest digest, entry
count, rendered hash and validation result; `on_mcp_success` for
`gobby-agents:send_message` with `when: 'manifest_digest' in
str(tool_input.get('content')) and str(vars.candidate_sha) in
str(tool_input.get('content'))` resets `candidate_sha` and `consensus`;
transition to `await` when `not vars.candidate_sha`). A refused derive or
apply is reported to the Writer and PD with the exact error and the step
stays in `stamp`; an edit after derivation means deriving again. Prompt
states Decision 14: the seat owns M1 and never hand-edits the plan, and it
never expands or dispatches.

**Acceptance:**

- 3.4.1 - `plan-writer` validates, is `seat`-tagged, and declares the
  load skills → draft → enhance → revise → adversary → handoff loop with
  the bounded enhancer pass, PD dispositions, the consensus commit, and the
  PD approval gate, reset by its close-task handler. file:
  `src/gobby/install/shared/workflows/agents/plan-writer.yaml`.
- 3.4.2 - `plan-adversary` validates as a seat, is read-only, and declares the
  load skills → await → review → stamp loop keyed on `candidate_sha`; the
  handoff-manifest tools are explicitly blocked in `load_skills`, `await`,
  and `review` and allowed in `stamp`, and every evidence-round tool is in
  the definition's `blocked_mcp_tools`. file:
  `src/gobby/install/shared/workflows/agents/plan-adversary.yaml`.
- 3.4.3 - The review-contract wiring tests pass against the seat body: review
  and proportionality skills load before review, rejection is findings-only,
  and M1 is applied only through the handoff-manifest tools. test:
  `tests/agents/test_plan_adversary_no_edits_on_reject.py::test_instructions_forbid_plan_edits_on_rejection`.
- 3.4.4 - `planner`, `plan-enhancer`, `analyst`, and both `-taskless`
  definitions are byte-identical before and after this deliverable except for
  the 1.1 `skills` removal, and the taskless reviewer contract still holds.
  test:
  `tests/agents/test_plan_adversary_internal_research_definition.py::test_taskless_review_status_allows_protocol_failure_without_verdict`.
- 3.4.5 - Both planning seats' prompts and steps carry Josh's plan flow
  (Decision 14): enhancer edits to the PD, Writer–Adversary consensus over
  `send_message`, the Adversary's handoff-manifest stamp from the committed
  consensus bytes, PD review, and Josh's approval before expansion; neither
  names an evidence-round tool. behavior: "derive_plan_handoff_manifest" in
  `src/gobby/install/shared/workflows/agents/plan-adversary.yaml`.

### 3.5 Seat bundle contract test [category: test] (depends: 3.1, 3.2, 3.3, 3.4)
`kind: deliverable`

Targets:
- `tests/workflows/test_seat_definitions.py`
- `tests/workflows/test_seat_step_loops.py`

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
read-only seats (`lane-manager`, `code-reviewer`, `log-monitor`,
`researcher`, `plan-adversary`) carry the Decision 8 `blocked_tools` list;
`assistant` and `archivist` carry no write tool in `blocked_tools` and their
prompts name `assistant-write-scope` and `archivist-write-scope`; every seat
with `required_skills` has a step workflow whose first step is `load_skills`
gated on `all(skill_loaded(skill) for skill in vars.required_skills)`; seats
with a step workflow (`developer`, `plan-writer`, `plan-adversary`,
`code-reviewer`, `log-monitor`, `researcher`) declare no `exit_condition`;
every step whose transition reads a flag the seat sets by hand lists
`gobby-workflows:set_variable` in its MCP allowlist; every step that
waits on another session (developer `claim` and `submit`, code-reviewer
`await`, log-monitor `tick` and `report`, researcher `serve`, plan-writer
`enhance`, `adversary` and `handoff`, plan-adversary `await`, `review` and
`stamp`) lists `gobby-agents:wait_for_coordination`; `plan-adversary`
blocks the two handoff-manifest tools in every step but `stamp` and lists
the evidence-round tools in its definition `blocked_mcp_tools`; every seat prompt
contains the `## Platform Context` and `## Skills` baseline sections; every
seat prompt mentions `gobby-agents:send_message`; the `developer` prompt
contains `EVENT=CANDIDATE` and names `impeccable`, `rust`, `typescript`, and
`tech-writer` in its routing table. The contract test runs against the
checkout, not the database, so it needs no daemon.

Loop tests: `tests/workflows/test_step_runtime_transitions.py` drives bundled
workflows through the real engine against a temporary `HubDatabase`
(`_setup_workflow`, `_after_mcp_tool`, `_before_mcp_set_variable`,
`_after_set_variable`); `test_seat_step_loops.py` follows it and, for each
loop seat, calls the real `wait_for_coordination` shape at its idle step
and runs two consecutive units of work: `developer` is refused
`implement` tools until both load gates pass, claims two tasks
in turn and `assigned_task_id` and `assigned_task_ref` rebind to the second
after the first
`close_task`; `code-reviewer` receives two candidates and the verdict reset
fires only on the message naming `candidate_task`; `plan-adversary` is
refused `derive_plan_handoff_manifest` in `load_skills`, `await`, and
`review`, is admitted to derive and apply in `stamp`, and stamps
two candidates with distinct `candidate_sha` values and the stamp report
resets only on the message naming the bound SHA; `plan-writer` takes one
plan from `draft` through `handoff` to close and binds a second
`plan_task`, and a findings message leaves it in `adversary`; `log-monitor`
reports two windows and a non-report message leaves `tick_done` set.

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
- 3.5.5 - Every loop seat admits `wait_for_coordination` at idle and runs
  two consecutive units of work through the real step engine with its
  correlation variable rebound and its flags reset by the named handler; a
  non-matching message leaves the step unchanged; `developer` refuses
  implementation before both skill sets load, and `plan-adversary` refuses
  handoff derivation outside `stamp`.
  test: `tests/workflows/test_seat_step_loops.py::test_loop_seats_reset_between_units`.

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
- `.gobby/roles/lane-5-functional.md`
- `.gobby/roles/lane-7-front-door.md`
- `.gobby/roles/lane-8-communications.md`
- `.gobby/roles/lane-9-memory.md`
- `.gobby/roles/rust-migration.md`
- `.gobby/roles/code-reviewer.md`
- `.gobby/roles/researcher.md`
- `.gobby/roles/archivist.md`
- `.gobby/roles/monitor.md`
- `.gobby/roles/plan-writer.md`
- `.gobby/roles/plan-adversary.md`

**Granularity:** eighteen files, one mechanical edit pattern, one commit; the
roster test and the persona receipt verify all rows together.

**Research context:** #22894 is open (blocked by #22870) and its acceptance
requires the roster rows, `_common.md`, one role file per session, and the
`default.yaml` lookup phrases (`test_default_agent_role_contract.py::test_default_profile_describes_all_lookup_guards`
pins the phrases; `_probe_documented_lookup` pins the roster row regex). Neither
file changes here. Each role file keeps its heading and becomes: "Definition:
`<seat>`. Call `gobby-agents:apply_persona(agent="<seat>")` once, then follow
the injected persona. Lane epics and session refs are task and roster state."
The nine lane files (`lane-1-gclient.md`, `lane-2-stability.md`,
`lane-3-hooks.md`, `lane-4-backlog.md`, `lane-5-functional.md`,
`lane-7-front-door.md`, `lane-8-communications.md`, `lane-9-memory.md`,
`rust-migration.md`) all point at `developer` and keep the lane line each
carries at edit time (lane name, and its epic with title where the file
names one): that line is how a lane is told which lane it occupies
(Decision 2), and the PD re-points a lane by editing it. `design-lead.md`
is off the roster and is not edited.
`researcher.md` points at `researcher`, `monitor.md` at `log-monitor`,
`plan-adversary.md` at `plan-adversary`. `monitor.md` is also #22908's target
(Lane 3 removes the phantom skill reference); whichever lands second rebases
onto the other, and the pointer form wins because it carries no skill
reference. `apply_persona` persists `_persona_name`, which survives compaction
and re-injects the persona; the 2.x rules key on it. Retiring the roster and
role files is #22903's (binding) work.

Rollout (Decision 13): after the branch lands, the PD announces the restart
globally, restarts the daemon from the main checkout outside quiet hours,
announces completion, inspects the startup sync result for errors and
shadowed rows, and confirms `gobby agents show <seat>` prints the seat row
for one seat; only then does each live session call `apply_persona` for its
seat on its next turn and report the receipt to the PD, as done for #22894.
`reload_cache` alone does not reload the 1.1, 2.2, and 3.2 Python.

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
GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/test_agent_definitions_v2.py tests/workflows/test_agent_models.py tests/workflows/test_workflows_agent_definitions.py tests/workflows/test_seat_definitions.py tests/workflows/test_seat_step_loops.py tests/workflows/test_seat_rules.py tests/workflows/test_condition_helpers_paths.py tests/workflows/test_step_runtime_transitions.py tests/workflows/test_default_agent_role_contract.py tests/workflows/test_retired_bundled_definitions.py tests/agents/test_agents_sync.py tests/agents/test_discovery_agents.py tests/agents/test_plan_adversary_loads_plan_review.py tests/agents/test_plan_adversary_manifest.py tests/agents/test_plan_adversary_no_edits_on_reject.py tests/agents/test_plan_adversary_self_check.py tests/agents/test_plan_adversary_internal_research_definition.py tests/skills/test_plan_adversary_rejection.py tests/skills/test_plan_skill_grammar.py tests/mcp_proxy/test_stage_review_schema.py tests/dispatch tests/build_pipeline tests/tasks/test_expansion_service_compile_plan_12725.py -q
uv run ruff format --check src/ && uv run ruff check src/ && uv run mypy src/
uv run gobby plans validate .gobby/plans/agent-definition-profiles.md -p /Users/josh/Projects/gobby
```

Live check after the PD-owned restart (Decision 13, 4.1): `gobby agents show
plan-writer` prints the seat row with `version: "1.0"`; the Plan Writer
session calls `apply_persona(agent="plan-writer")` and on its next turn sees
the seat prompt plus the shared seat guidance once; a deliberate
`spawn_agent(agent="default")` from that session is blocked by
`plan-writer-enhancer-only`; a `gobby-workflows:run_pipeline` call from that
session is blocked by `seat-no-pipeline-launch`; an Assistant write under
`src/` is blocked by
`assistant-write-scope`; `gobby agents show backend-developer` reports no
such definition. Do not run the full pytest suite. The restart is announced
globally before and after, outside quiet hours, and no seat activates before
it.

Round 1 (Adversary gobby#14579, message 5c6cf6a7, snapshot 7e975ca8): needs_review, 8 blocking and 3 nits. Writer dispositions, each accepted; the repairs landed in 91a3be7558.

- PA-001 accept. A session boolean cannot be a per-task ledger; already_claimed reclaims and create_task(claim=true) never re-arm it. Repair: two session-variable ledgers of task UUIDs (plan_writer_planning_tasks, plan_writer_enhancer_tasks), track-claim and consumed handlers, no re-arm rule, engine tests for reclaim, create-and-claim, failed spawn, compact, and task switch (Decision 6, 2.2, acceptance 2.2.2).
- PA-002 accept. Instance variables shadow session writes. Repair: every hand-set flag uses gobby-workflows:set_variable(scope="step") named in the step's MCP allowlist, handlers for tool-driven flags, 3.5 invariant (P3 intro, every seat).
- PA-003 accept. Repair: every seat with required_skills opens with a load_skills step gated on all(skill_loaded(...)); researcher gets load_skills -> serve; developer gains route_skills before the load gate so task-derived skills are merged before the empty-list transition (Decision 7, P3 intro, 3.2, 3.3, 3.5).
- PA-004 accept. Repair: each loop seat carries a correlation variable (assigned_task_id/assigned_task_ref, candidate_task, tick_window, evidence_id, plan_task), resets in the handler of the call that ends the unit, handlers live on the step that runs the tool, message handlers match the event and id; 3.5 engine tests run two consecutive units per seat (3.2, 3.3, 3.4, acceptance 3.5.5).
- PA-005 accept. Repair: Decision 14 and 3.4 specify the append, acknowledge, finalize, then repair or apply handshake with refusal handling and fresh-round behavior; Josh's approval precedes expansion. The static-seat runtime gap (dispatch_run_id, bind_evidence_run) is recorded as D3 on #22912, which has since landed and is being exercised by this very append.
- PA-006 accept. Repair: assistant and archivist carry no write block; their scope is a path-aware before_tool rule (seat-write-scope.yaml) using a registered write_paths_within helper in a new condition_helpers_paths.py, registration moved out of safe_evaluator.py to respect its size; tests for allowed, blocked, and opaque writes (Decision 8, 2.2, 3.1, 3.3).
- PA-007 accept. Repair: storage/definitions/agents.py::parent_body drops an empty stored skills map on read; non-empty stored maps outside bundled ownership are inventoried and rewritten before the schema cutover; bundled rows refresh by sync drift; sync.py and imports.py listed as consumers unchanged with reasons (1.1, acceptance 1.1.4).
- PA-008 accept. Repair: rollout is a PD-owned restart from the main checkout after the branch lands, then sync inspection, then activation; reload_cache only for later data-only changes; V1 live checks for version, retired-name routing, and persona receipt (Decision 13, 4.1, V1).
- PA-009 accept (nit). Decision 2 now states the seat tag is a template catalogue marker read by the 3.5 file test only; sync writes tags=[gobby].
- PA-010 accept (nit). 3.4 keeps both API tests in test_stage_review_schema.py, changes only the final YAML text-parity check, and names the exact assertions to retarget or delete per module.
- PA-011 accept (nit). docs/contracts/plan-coverage.md joins the 3.2 Targets so Category and TDD Policy maps to the single developer definition.

No disagreements to escalate. This record is kept as history; the 2026-09-27 refresh below retires the round flow it assumed.

```json plan-review-round
{"evidence_id":"954b3556-9531-49b2-86cf-8f576146bf25","plan_hash":"7e975ca8a25544c45c1117da4ed61c47a0c83dc55f953ae97f9b142f83fcf78c","round_number":1,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"fc21ffce3ce7a7a8e747f54be5963d4a76bf65f37e13747fc277f5936e71b807","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":0,"emitted_findings":11,"total":11},"evidence_id":"954b3556-9531-49b2-86cf-8f576146bf25","lanes":[{"candidate_count":2,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":3,"lane_id":"repository_blast_radius","status":"delegated-verified"},{"candidate_count":6,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":10,"manifest_digest":"741daeaf1505adf20aefbb12a14bf8ae16f79b7a877c6968788c5ef4514a8ec4","status":"valid"},"source_digest":"183135622ef42f9a5d21c6d6f9e47c4cffe2951547a5e36e284ee0d66a7c5b44","version":1},"findings":[{"category":"unhandled-edge","check_key":"enhancer-budget-bound-to-task","description":"The budget is a session boolean reset by every successful claim_task response containing task_id. claim_task returns success=true, task_id, already_claimed=true when the same session reclaims the same task (src/gobby/mcp_proxy/tools/tasks/_lifecycle_claim.py). Thus claim A → enhancer → claim A grants another pass. task_claimed only means the claim map is nonempty; it does not prove category=planning. create_task(claim=true) and context loss also have no defined budget behavior.","finding_id":"PA-001","fix":"Bind enhancer eligibility and consumption to the canonical claimed planning-task UUID. Define same-task reclaim, create-and-claim, failed spawn, context loss, and task switch behavior; never reset consumption solely because claim_task succeeded. Specify the exact event payload access and add engine tests for each case.","location":"§2.2 enhancer-only, consumed, and rearm rules","prevention":"Test the full allowance lifecycle with real claim response shapes, including already_claimed and non-planning claims.","root_cause":"A session flag is being used as a per-task authorization ledger.","section_id":"2.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"step-state-write-scope","description":"The specified proxy top-level set_variable writes session scope only (stdio_tools.py::set_variable). The engine evaluates transitions with instance.variables overriding session variables and deliberately does not copy a native write over an existing instance key (enforcement_completion.py::_process_step_after_tool). candidate_received, verdict_ready, tick_done, consensus, and additional_skills are declared in the step instance, so the proposed manual writes cannot advance those gates.","finding_id":"PA-002","fix":"Specify gobby-workflows:set_variable with scope='step' for workflow-local state, with its schema-discovery and MCP allowlist entries, or use correlated on_mcp_success handlers. Reserve top-level set_variable for genuinely session-scoped state. Sweep every seat and add real-engine transition tests that use the exact public call shape.","location":"§3.2–3.4 manually set step variables","prevention":"Exercise transitions through the public variable tool instead of mutating instance fixtures directly.","root_cause":"Session state and workflow-instance state are separate stores with instance precedence.","section_id":"3.4","severity":"blocking"},{"category":"missing-requirement","check_key":"required-skills-have-reachable-load-path","description":"§3.4 promises a load_skill gate before review, but its actual await→review→resolve specification has no load_skill step and its await/review MCP allowlists omit the skill loaders. Code-reviewer likewise declares required skills without a load gate. Researcher has no step_workflow yet is assigned required_skills, whose only supported storage/read location is step_workflow.variables (dispatch/skill_composition.py). Developer starts with additional_skills=[] and the engine chains transitions, so it can leave the load gate before task-derived skills are populated.","finding_id":"PA-003","fix":"Give each seat an explicit supported skill attachment and loading contract. Add a reachable, tracked skill-loading gate for workflow seats; decide how the workflow-free researcher must load its three skills without introducing an ignored field. Populate/merge developer task and domain skills before evaluating the empty-list transition, preserving task-supplied TDD skills. Test refused review/implementation before complete loads and successful admission afterward.","location":"§3.2 task skill routing; §3.3 researcher/reviewer skills; §3.4 load_skill contract; §3.5 checks","prevention":"Validate executable admission behavior, including first use and task changes, rather than only resolving skill names on disk.","root_cause":"Declaring a list of required skills does not execute or enforce loading it.","section_id":"3.4","severity":"blocking"},{"category":"unhandled-edge","check_key":"persistent-loops-reset-and-correlate","description":"The proposed persistent loops do not define complete round/task resets. The Adversary allows finalize_plan_review_evidence in review but attaches its round_prepared reset only to resolve, whose handlers cannot see that earlier completion. verdict_ready/consensus stay true across returns to await; the engine immediately follows successive true transitions. Writer likewise leaves draft_validated/enhancement_reviewed/candidate_sent/consensus stale. Developer resets task_claimed on any send_message in submit, without matching the candidate/task or rebinding assigned_task_id for the next claim; inherited completion handlers compare against assigned_task_id. Several idle steps also omit the event-driven wait tools.","finding_id":"PA-004","fix":"Specify state ownership, initialization, matching task/evidence IDs, reset timing, and allowed wait tools for every reusable loop. Place each success handler in the step that executes its tool; condition message completion on the intended recipient/event/task. For the developer, bind the new claimed UUID and define candidate/bounce/close flow. Add engine tests for two consecutive tasks/rounds, rejection then revision, unrelated messages, failed tools, and idle wait.","location":"§3.2 submit loop; §3.3 observation/reviewer loops; §3.4 Writer/Adversary loops","prevention":"Run each proposed loop twice with real handler and transition evaluation; verify that stale flags cannot skip a phase.","root_cause":"One-shot step fragments were converted to long-lived loops without closing their lifecycle.","section_id":"3.4","severity":"blocking"},{"category":"bad-sequencing","check_key":"interactive-review-checkpoint-before-finalize","description":"The static-seat flow calls finalize_plan_review_evidence without first creating a durable canonical V1 checkpoint. Interactive evidence finalization calls require_durable_checkpoint, which raises missing_v1_checkpoint unless append_plan_changelog_round has written the matching canonical round (review_checkpoint_service.py:98). The seat is correctly forbidden to write the plan, but no Writer/PD append-and-ack step is specified for either verdict.","finding_id":"PA-005","fix":"Specify the existing-tool handshake: Adversary sends canonical result; coordinator records individual finding dispositions and appends the canonical round; coordinator acknowledges the checkpoint; the designated owner finalizes evidence; only then may Writer repair/revise or PD proceed toward approved manifest application. Include failure/retry and fresh-round behavior, and retain explicit Josh approval before expansion.","location":"Decision 14 and §3.4 evidence finalization protocol","prevention":"Exercise one rejected and one approved static-seat round through append/finalize without a spawned run_id.","root_cause":"The protocol omitted a durable checkpoint required by the existing interactive evidence API.","section_id":"3.4","severity":"blocking"},{"category":"missing-requirement","check_key":"write-exceptions-representable-in-definition","description":"Assistant and Archivist must write permitted files, but both are required to carry a global block on Write/Edit/apply_patch and other write tools. _check_agent_tool_enforcement enforces those blocks unconditionally before later exemptions; it has no path exception. Prose cannot make the allowed digest/docs write work for the declared spawn surface, and the plan assigns future exception enforcement to activation without defining the declarative policy that activation could consume.","finding_id":"PA-006","fix":"Choose and specify a compatible declaration: use an existing path-aware rule/capability for the allowed writes and avoid contradictory global tool blocks, or define a concrete jointly owned activation contract and defer those seats' enforced rollout until it lands. Carry the assistant/archivist allowed and denied paths into acceptance tests; do not require both an unconditional block and a successful exception.","location":"Decision 8; §3.1 assistant carve-out; §3.3 archivist digest; §3.5 blanket blocklist","prevention":"Test one permitted and one forbidden write for each seat with an exception, on both supported activation surfaces.","root_cause":"Tool-wide denial and path-scoped write authority are being treated as interchangeable.","section_id":"3.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"persisted-skill-key-migration","description":"Current AgentDefinitionBody.model_dump includes skills (even its default empty map), and bundled sync writes that body into definition_json. _body_from_row validates stored bodies directly. Rejecting any skills key makes old stored bodies invalid until rewritten; sync only refreshes managed bundled rows and explicitly skips unmanaged/shadowing rows. Removing ten YAML maps does not specify treatment of persisted custom/project/soft-deleted bodies. D1 covers other persistence defects and does not assign this new schema migration.","finding_id":"PA-007","fix":"Inventory affected persisted definition and snapshot forms and specify their one-time migration or explicit retirement, preserving user-owned content and the chosen prose migration policy. Add the actual migration owner/Targets and tests proving an old stored body can resolve after cutover, while new submitted skills keys are rejected. If a separate prerequisite owns it, record a concrete typed dependency and gate the schema cutover.","location":"§1.1 schema retirement and D1 persistence boundary","prevention":"Test the schema change against pre-change serialized bodies, including unmanaged definitions, rather than only new YAML.","root_cause":"Input-schema rejection was planned without the storage transition for data serialized by the old model.","section_id":"1.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"rollout-reloads-python-code-before-sync","description":"The plan changes Python model and routing code but says reload_cache makes rollout live without a restart. reload_cache re-syncs definitions and clears the pipeline cache; it does not reload AgentDefinitionBody, dispatch, or expansion modules. A daemon holding the old model still drops version and accepts the retired skills field, and retains old routing after the YAMLs have been deleted. Checking one displayed seat does not establish a coherent runtime cutover.","finding_id":"PA-008","fix":"Require a coordinated PD-owned daemon restart/cutover after the Python changes are installed, unless an already-completed fresh process is explicitly verified. Sync under that process, inspect sync errors/shadowed rows, then activate the seats. Keep reload_cache-only rollout for subsequent data-only changes. Add bounded live checks for stored version, retired-name routing, and the intended persona/rule receipt.","location":"Decision 13; §4.1 rollout; V1 live verification","prevention":"Separate Python process activation from registry/cache refresh in rollout acceptance.","root_cause":"Refreshing stored configuration was mistaken for reloading imported Python code.","section_id":"4.1","severity":"blocking"},{"category":"traceability","check_key":"seat-tag-runtime-vs-template-contract","description":"The seat tag is currently template-only: AgentDefinitionBody ignores tags and sync_bundled_agents creates/restores rows with tags=['gobby']; _build_agent_update_fields does not propagate YAML tags. The new file-based test passes while installed rows are not seat-tagged.","finding_id":"PA-009","fix":"State explicitly that seat is a template catalogue marker if no runtime consumer needs it. If installed seat metadata is intended, target sync tag propagation and test create/update/restore while preserving ownership tags.","location":"Decision 2; §3.5 seat-tagged catalogue test","prevention":"Distinguish raw YAML metadata checks from assertions about installed rows.","root_cause":"File-only contract tests do not cover the sync projection.","section_id":"3.5","severity":"nit"},{"category":"weak-testability","check_key":"test-retarget-keeps-independent-api-contract","description":"tests/mcp_proxy/test_stage_review_schema.py is not entirely a retired agent-shape test. test_reject_review_uses_shared_finding_schema tests the live reject_review API and never reads agent YAML; test_finding_schema_parity_with_adversary_contracts mostly tests that same API and only its final check concatenates the two YAML texts. The broad 'retarget to plan-adversary-taskless or delete' instruction is avoidably ambiguous.","finding_id":"PA-010","fix":"Keep both API schema tests; adjust only the staged-file textual parity check if needed, and name the exact obsolete step/model assertions to remove or retarget in the eight wiring modules. Add seat-specific counterparts for retained behavior rather than weakening assertions to fit the new YAML.","location":"§3.4 retarget-or-delete test instructions","prevention":"Classify each assertion by its behavior owner before retiring a definition.","root_cause":"Module-level labels conflate agent-shape checks with independent live API contracts.","section_id":"3.4","severity":"nit"},{"category":"traceability","check_key":"canonical-routing-doc-updated","description":"The mandatory Plan-Coverage Contract still states that backend/frontend/fullstack route to the three definitions this plan deletes (docs/contracts/plan-coverage.md:507-515). Neither 3.2 nor 4.2 owns that canonical documentation consumer.","finding_id":"PA-011","fix":"Add docs/contracts/plan-coverage.md to the routing migration or a dependent docs deliverable and update Category/TDD Policy to the single-developer mapping while retaining implementation_domain semantics.","location":"§3.2 routing migration and §4.2 documentation Targets","prevention":"Include canonical contracts in the literal consumer sweep.","root_cause":"The rename inventory covered runtime/prompt consumers but missed the governing contract.","section_id":"4.2","severity":"nit"}],"verdict":"needs_review"},"session_id":"3ce7b2cd-1a6c-4946-893c-cc4d5ac0fd9e"}
```

- 2026-09-27: refresh under the PD (gobby#14610), who drives the planning
  queue (Josh, 2026-09-27). Flow: Decision 14 and 3.4 now encode Josh's plan
  flow (memory 55b8c14e), with the Adversary stamping M1 through
  `derive_plan_handoff_manifest` and `apply_plan_handoff_manifest` as it did
  for #22904. The evidence-round checkpoint handshake and D3 are removed
  (#22912 closed). Policy: the `deploy_runbook` and
  `retry_runbook_deployment` blocks are gone (#22895 replaced that API with
  ordinary pipelines). Seats are now blocked from launching pipelines and
  from the `gobby agents spawn` and `gobby pipelines run` shell commands
  (Decision 6, 2.2.1). Roles: the catalogue and 4.1 follow the 22-row
  roster, and 3.1 to 3.3 follow the current role files. The enhancer pass
  for this task was already spent, so this refresh runs no new pass.
- 2026-09-27: PD dispositions (gobby#14610) on `34257c1f47`. (a) The enhancer
  exception stays scoped to the Plan Writer; Lane 7's historical one-off
  pass is consumed and confers no standing launch privilege. (b) Pipeline
  launch is blocked for every seat, the Program Director included, with no
  carve-out; Josh's operator CLI, web, gclient and cron surfaces are
  unchanged. Decision 6 states both. The PD's full artifact review is still
  pending.
- 2026-09-27: Adversary findings (gobby#14579) on `cfddccd461`, all accepted
  and repaired. PA-001: a `/clear` successor inherits both enhancer ledgers
  through `preserve_task_claim_state` (Decision 6, 2.2, 2.2.2, 2.2.6).
  PA-003: `developer` opens with `load_skills`, then claim, route skills
  and the additional-skills gate, so the 3.5 invariant holds (3.2,
  3.2.1). PA-004: every idle step allows
  `gobby-agents:wait_for_coordination`, including the log-monitor cadence
  and the Adversary's `stamp` (3.2 to 3.5). PA-012: read-only tools
  bypass allowlists, so `plan-adversary` blocks the handoff tools in
  every step but `stamp` and blocks every evidence-round tool
  definition-wide (3.4, 3.4.2, 3.5). PA-013: `write_paths_within` is
  component-wise directory containment and the Archivist uses
  `write_paths_match` on the dated digest name, both after symlink and
  traversal resolution (2.2, 2.2.4, 2.2.5).
