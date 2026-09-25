# Per-agent sandbox profiles for spawn_agent

**Plan ID:** agent-sandbox-profiles

Plan artifact: `.gobby/plans/agent-sandbox-profiles.md`

Planning task: #22899 Plan per-agent sandbox profiles for spawn_agent (item 2 of the re-scoped
#22691 incremental epic). Author: Researcher gobby#14550. Decisions confirmed by Josh in the
#14550 terminal on 2026-09-25.

## Overview
`kind: framing`

Every spawned agent gets the same sandbox today. `spawn_agent_impl`
(`src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`, near line 393) takes
`apply_write_grant(agent_sandbox_config(daemon_config), write_grant)` under the comment "Daemon-owned
agent sandboxes inherit from config-store defaults only". The only per-spawn setting is the
external write grant (`extra_write_paths` plus `write_paths_reason`). The project adds only
`sandbox.extra_write_paths` from `.gobby/project.json`. A research agent therefore cannot reach
the web, because SRT allows network access only through a domain allowlist and the default
allowlist holds just the provider API domains.

This plan adds named sandbox profiles to the daemon config. An agent definition selects one
profile by name, the spawn and resume paths resolve it, and one bundled profile, `research`, is
unsandboxed (`enabled: false`). An unsandboxed profile is a sandbox off switch, so the plan also
closes the two escape routes that the #22808 runbooks council found for definition-carried sandbox
exemptions (PD send-back S1 and writer position (aa) in
`.gobby/plans/runbooks-council-round2.md`, R8). Only the bundled template sync can write the
selector, and a sandboxed caller cannot launch an unsandboxed child.

Out of scope, and owned elsewhere:
- The research agent's definition and its restricting rules belong to #22902 (plan YAML agent
  definitions with rules, skills and step workflows).
- Activation belongs to #22903 (`apply_agent_definition`).
- Placed launch belongs to #22904 (placed agent launch backend and gclient integration).

This plan delivers the mechanism, the `research` profile and the guards those tasks rely on.

## Decision Record
`kind: framing`

Josh confirmed items 1-6 in the planning interview and accepted defaults 7-12. Items 13-15 carry
over the PD's S1 ruling and position (aa) from the #22808 council. They restate Josh's standing
concern that agents must not be able to use a sandbox off switch.

1. **Where profiles live.** Profiles are named entries under `agent_sandbox.profiles.<name>` in
   the daemon config store. Each entry uses the `DaemonOwnedSandboxConfig` field set. Agent
   definitions only reference a profile by name. Rejected: inline sandbox blocks in definitions,
   which is the retired #6202 shape and spreads policy across rows; also rejected was combining
   inline blocks with profiles.
2. **Widening.** Profiles are written by the operator in the config store, so creating a profile is
   the authority. A profile may grant more than the default, up to `enabled: false`. Rejected:
   narrow-only profiles, which would force a research allowlist onto every agent; also rejected
   was per-spawn approval for widening, which adds mechanism.
3. **Research web access.** The bundled `research` profile is `enabled: false`. SRT cannot express
   open web access. Its schema rejects a bare `*` and TLD wildcards in `allowedDomains`
   (`~/.gobby/tools/srt/0.0.76/.../dist/sandbox/sandbox-config.js:49`: "Overly broad patterns like
   "*.com" or "*" are not allowed for security reasons"). Josh chose an unsandboxed profile that is
   "heavily restricted with rules" over a curated domain bundle.
4. **Who selects.** Only the agent definition selects, through the new
   `AgentDefinitionBody.sandbox_profile`. `spawn_agent` gains no profile parameter.
5. **Resume.** The run records the profile name, and resume resolves the name again, so profile
   edits, tightening in particular, take effect. If the named profile has been removed, the resume
   is refused. This replaces today's replay of the stored `sandbox_config` snapshot.
6. **Rules ownership.** #22902 designs the researcher's rules. This plan adds only a guard: a
   definition may select an `enabled: false` profile only when `workflows.rules` is non-empty.
7. **Unknown profile names fail closed.** When a definition names a profile that does not exist,
   the spawn is refused. It never falls back to the default policy.
8. **No profile means today's behavior.** A definition without `sandbox_profile` uses the top-level
   `agent_sandbox` fields as today. No migration is needed (0.5.0 has not shipped).
9. **Ask runtime profiles are unchanged.** Ask managed runtime profiles keep their own
   `SandboxConfig` and still take precedence (`AskRuntimeProfile.sandbox_config` from
   `ask_sandbox_config`).
10. **Grants and project paths still stack.** The per-spawn external write grant and the project's
    `sandbox.extra_write_paths` still apply on top of the resolved profile, through
    `apply_write_grant` and `sandbox_config_for_spawn`.
11. **Fallback agents resolve their own profile.** Every agent in a fallback chain uses its own
    definition's profile, because resolution keys on the spawned body.
12. **The selector survives sync.** `AgentDefinitionBody` uses `extra="ignore"`, so a misspelled
    key disappears silently. A sync test proves that a bundled template's `sandbox_profile`
    survives into the stored row.
13. **Only the sync writes the selector** (S1 items 1 and 3). Every `AgentDefinitionManager` write
    entry point except the sync ones refuses an incoming body that carries a non-null
    `sandbox_profile`. It also refuses any non-sync write to a row whose stored body carries one.
    Covered entry points: create, update, toggle, restore, duplicate, upsert, set_step_workflow,
    and moves between project and global. The selector can then arrive only through a reviewed
    merge into `src/gobby/install/shared/workflows/agents/`. The worktree-daemon guard already
    stops a branch from publishing templates.
14. **Only sync-owned rows are honored** (S1 item 2). At spawn, a body carrying `sandbox_profile`
    is honored only when its stored row satisfies the existing sync-ownership predicate
    `_is_sync_managed_bundled_agent` (global row, `source` installed, tag `gobby`), made public.
    Any other row with the selector is refused, which is clearer than silently ignoring it.
15. **No unsandboxed child for a sandboxed caller** (position (aa)). If the proxy-authenticated
    `caller_session_id` resolves to a session with `sandbox_enabled` true, it cannot spawn a
    definition whose resolved profile has `enabled: false`. A spawn with no authenticated caller
    session is refused for an `enabled: false` profile, whatever the parent argument says (fail
    closed). This covers the HTTP spawn route, which parents on a launcher session. Josh launches
    research agents from his unsandboxed pane sessions through the MCP proxy.

## Constraints
`kind: framing`

- **Monolith ceiling.** `spawn_agent/_implementation.py` is 964 lines and
  `agents/resume_executor.py` is 972 lines, measured on 2026-09-25. Both go through new modules
  (`src/gobby/agents/sandbox_profiles.py` and `src/gobby/agents/resume_sandbox.py`) and must not
  grow. `src/gobby/cli/daemon.py` (991 lines) stays untouched. Its SRT preflight checks only the
  top-level `enabled` flags. When every default is off but a profile is on, SRT failures surface
  at `prepare_sandbox_launch` and the spawn fails closed. This is accepted, not planned work.
- **SRT only.** Profiles honor the managed SRT contract for every CLI. `backend` inside a profile
  stays `srt`. `provider-native` remains the documented rollout and debug override
  (`docs/guides/sandboxing.md`) and gets no new branch. Under SRT, `mode` has no effect
  (`compute_sandbox_paths` and `render_srt_settings` never read it) and `allow_network: true` is
  refused by `prepare_sandbox_launch`. Profiles inherit both behaviors unchanged.
- **Config carriers.** Any `.py` change under `src/gobby/config/` must regenerate
  `crates/gcore/assets/config/runtime_config_contract.json` and
  `web/src/api/runtimeConfigCodecVectors.gen.ts` with `scripts/generate_runtime_config_contract.py`
  (`--stdout` and `--stdout-web`). The tests in `tests/config/test_runtime_config_contract.py` check
  both byte for byte.
- **Found work routed, not planned.** HTTP `update_definition` (`src/gobby/servers/routes/agents.py`)
  still maps a legacy `sandbox_config` onto `body["sandbox"]`, a key that `AgentDefinitionBody`
  drops through `extra="ignore"`. The route is 902 lines. The dead mapping is reported to the PD
  with this plan rather than folded in, and 1.2's storage guard makes it harmless for
  `sandbox_profile`.
- **Why the Jan-2026 design is not revived.** #6198 (Sandbox Configuration Injection for Spawned
  Agents), #6201 (Phase 3: Spawn Integration), #6202 (Phase 4: Agent Definition Support) and #6327
  (Add sandbox config to meeseeks-box spawn calls) put per-CLI native sandbox blocks into agent
  YAML and `spawn_agent` parameters.
  - #9023 (commits `4ccee27970` and `d24fb48994`, 2026-02-23) dropped the definition fields. See
    `docs/plans/completed/workflows-v2.md`.
  - #11868 (commit `02ddab6a65`, 2026-04-17) removed the tool parameters and moved policy to
    daemon-owned runtimes "so every CLI launch goes through one policy path" (CHANGELOG).
  - This plan keeps that single path. Definitions select an operator-authored policy by name.
    They never carry policy, and callers never override it.

## P1: Profiles, selector and write guard
`kind: framing`

1.1 adds the config shape and 1.2 adds the definition selector with its storage guard. The two
are independent. 1.3 and 1.4 consume both.

### 1.1 Named agent sandbox profiles in the daemon config [category: config]
`kind: deliverable`

Targets:
- `src/gobby/config/daemon_sandbox.py::DaemonOwnedSandboxConfig`
- `src/gobby/config/app.py::*` — scope-reason: retype only the `agent_sandbox` field of `DaemonConfig`; every other field and consumer keeps its type
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerated wholesale by `scripts/generate_runtime_config_contract.py --stdout`
- `web/src/api/runtimeConfigCodecVectors.gen.ts::*` — scope-reason: regenerated wholesale by `scripts/generate_runtime_config_contract.py --stdout-web`
- `tests/config/test_daemon_sandbox.py::*` — scope-reason: add the two new profile tests beside the existing ones

Consumers unchanged:
- `src/gobby/agents/sandbox.py` — no-edit-reason: `daemon_owned_sandbox_config` reads the base fields through `getattr`, and the new subclass keeps them.
- `src/gobby/cli/daemon.py` — no-edit-reason: the startup preflight reads only `agent_sandbox.enabled` and `web_chat_sandbox.enabled` (see Constraints).

**Research context:**
- `DaemonOwnedSandboxConfig` (`src/gobby/config/daemon_sandbox.py`) has 13 fields and no
  validators: `enabled`, `backend`, `mode`, `allow_network`, the four extra path lists,
  `allowed_domains`, `denied_domains`, `allow_git_network`, `allow_package_registries` and
  `allow_unix_sockets`.
- `DaemonConfig.agent_sandbox` (`src/gobby/config/app.py`, near line 276) builds it with
  `backend="srt", allow_network=False`. `web_chat_sandbox` uses the same class and must not gain
  profiles.
- The config store is DB-first and uses flattened dotted keys (`src/gobby/storage/config_store.py`).
  A `dict[str, Model]` field has precedent: `endpoints: dict[str, GenerationEndpointConfig]` in
  `src/gobby/config/ai.py` and `hubs: dict[str, HubConfig]` in `src/gobby/config/skills.py`, so
  `agent_sandbox.profiles.research.enabled` stays editable through `/api/config/values`.
- `runner_init/config_subscribers.py` routes `agent_sandbox` edits to "agent launch, operation
  ConfigRuntime.capture", so profile edits apply to the next spawn with no restart.

**Approach:**
- Add a new class `AgentSandboxConfig(DaemonOwnedSandboxConfig)` in `daemon_sandbox.py` with
  `profiles: dict[str, DaemonOwnedSandboxConfig]`. The default is
  `{"research": DaemonOwnedSandboxConfig(enabled=False)}`, described as "Unsandboxed profile for
  research agents; restrict with definition rules". Export it in `__all__`.
- A validator refuses profile names that are not `^[a-z][a-z0-9-]*$` and any profile whose
  `backend` is not `srt`.
- Retype `DaemonConfig.agent_sandbox` to `AgentSandboxConfig`, keeping its current default
  arguments.
- Regenerate both carriers.
- Rejected: a separate top-level `agent_sandbox_profiles` key, because it splits one policy
  surface in two.

Verification: `uv run python scripts/generate_runtime_config_contract.py --stdout`, compared
against the asset;
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/config/test_daemon_sandbox.py tests/config/test_runtime_config_contract.py tests/config/test_app_config.py -q`.

**Acceptance:**

- 1.1.1 - `AgentSandboxConfig` carries a `profiles` map whose default contains an unsandboxed `research` profile, and `DaemonConfig.agent_sandbox` is typed as `AgentSandboxConfig`. symbol: `AgentSandboxConfig`. test: `tests/config/test_daemon_sandbox.py::test_agent_sandbox_default_research_profile_is_unsandboxed`.
- 1.1.2 - Invalid profile names and non-`srt` profile backends are refused at config validation. test: `tests/config/test_daemon_sandbox.py::test_agent_sandbox_profile_validation_refuses_bad_name_and_backend`.
- 1.1.3 - The runtime config contract and web codec vectors match fresh generator output. test: `tests/config/test_runtime_config_contract.py::test_checked_in_contract_matches_registry`. test: `tests/config/test_runtime_config_contract.py::test_checked_in_web_codec_vectors_match_registry`.

### 1.2 Definition selector and sync-only write guard [category: code]
`kind: deliverable`

Targets:
- `src/gobby/workflows/agent_models.py::AgentDefinitionBody`
- `src/gobby/storage/definitions/agents.py::*` — scope-reason: one guard covers every non-sync write entry point of `AgentDefinitionManager` (create, update, _write_update, toggle_enabled, restore, duplicate, upsert_with_steps, set_step_workflow, move_to_project, move_to_global)
- `src/gobby/agents/sync.py::*` — scope-reason: make the sync-ownership predicate public and move the new-row branch of `sync_bundled_agents` onto `upsert_from_sync`
- `tests/storage/definitions/test_agents_manager.py::*` — scope-reason: add the write-guard tests beside the existing manager tests
- `tests/agents/test_agents_sync.py::*` — scope-reason: add the selector sync tests to `TestSyncBundledAgents`

Consumers unchanged:
- `src/gobby/ask/agents.py` — no-edit-reason: Ask builds bodies with no `sandbox_profile`; the new optional field defaults to None.
- `src/gobby/mcp_proxy/tools/spawn_agent/_step_state.py` — no-edit-reason: reads step fields only.
- `src/gobby/servers/routes/agents.py` — no-edit-reason: its writes end in `AgentDefinitionManager`, where the guard refuses the selector (the legacy `sandbox_config` mapping is routed to the PD, see Constraints).
- `src/gobby/workflows/definitions.py` — no-edit-reason: validates bodies; an absent optional field changes nothing.
- `src/gobby/workflows/imports.py` — no-edit-reason: writes through the manager, which carries the guard.
- `src/gobby/workflows/step_instances.py` — no-edit-reason: reads step fields only.
- `docs/evidence/wiki-bakeoff-code-2026-09/test_ask_cohort.py` — no-edit-reason: archived evidence, not a live consumer.
- `tests/agents/test_merge_orchestrator_contract.py` — no-edit-reason: builds bodies without the optional field.
- `tests/mcp_proxy/tools/skills/test_list_skills.py` — no-edit-reason: builds bodies without the optional field.
- `tests/mcp_proxy/tools/spawn_agent/test_initial_variables.py` — no-edit-reason: builds bodies without the optional field.
- `tests/workflows/test_agent_models.py` — no-edit-reason: existing model tests stay valid with a defaulted optional field.
- `tests/workflows/test_agent_workflow_completion.py` — no-edit-reason: builds bodies without the optional field.
- `tests/workflows/test_handler_route_lint.py` — no-edit-reason: builds bodies without the optional field.
- `tests/workflows/test_step_enforcement.py` — no-edit-reason: builds bodies without the optional field.
- `tests/workflows/test_step_instances.py` — no-edit-reason: builds bodies without the optional field.
- `tests/workflows/test_step_snapshot_semantics.py` — no-edit-reason: builds bodies without the optional field.
- `tests/workflows/test_workflows_dry_run.py` — no-edit-reason: builds bodies without the optional field.

**Research context:**
- `AgentDefinitionBody` (`src/gobby/workflows/agent_models.py`) has no sandbox field today and
  sets `model_config = ConfigDict(extra="ignore")`. `workflows: AgentWorkflows` carries
  `rules: list[str]`. `rule_selectors` is required and always present, so it cannot serve as the
  "has rules" signal.
- Definition writes all end in `AgentDefinitionManager`
  (`src/gobby/storage/definitions/agents.py`). This was verified by the #22808 council on
  2026-09-24 (R8 verification):
  - `gobby-workflows:create_agent_definition` (`mcp_proxy/tools/workflows/_agents.py`) calls
    `upsert_with_steps(source="installed", tags=["user"])`.
  - HTTP `update_definition` calls `manager.update`. `import_definition` calls
    `upsert_with_steps(create_only=True)`. `_upsert_agent` in `workflows/imports.py` calls
    `update` or `upsert_with_steps`. `toggle_enabled` and `restore` are HTTP routes.
  - `gobby agents` has no create or update command.
  - The executor re-runs `gcode grep -w upsert_with_steps src/`, the same grep for `.update(` over the HTTP agents routes module,
    and `gcode grep -w create_agent_definition src/` before editing, and adds any new write path
    to the guard.
- Sync writes already go through separate entry points. `update_from_sync` calls
  `_write_update(from_sync=True)`, which locks the live row through `_lock_live_row`.
  `upsert_from_sync` also exists. `sync_bundled_agents` (`src/gobby/agents/sync.py`) creates new
  rows through `upsert_with_steps` near line 258; that is its one non-sync write.
  `_is_sync_managed_bundled_agent` decides ownership (global row, `source == "installed"`,
  `"gobby"` in tags).
- The HTTP request models accept `tags`, so the tag alone can be forged. The guard makes the
  marker sound: once the guard is in place, only sync entry points can store `sandbox_profile`,
  and the first start after the cutover re-writes or sweeps every `gobby`-tagged row.

**Approach:**
- Add `sandbox_profile: StrictStr | None = None` to `AgentDefinitionBody`, described as "Named
  agent_sandbox profile; bundled templates only".
- In `storage/definitions/agents.py`, add one private guard,
  `_refuse_sandbox_profile_write(incoming_body, current_row)`. It raises
  `ValueError("sandbox_profile is sync-owned")` when the incoming parent body carries a non-null
  `sandbox_profile`, or when the locked current row's body carries one. Call it from every
  non-sync entry point in the scope-reason, inside the same transaction as the row lock. The
  `from_sync=True` path and `upsert_from_sync` skip it.
- Rename `_is_sync_managed_bundled_agent` to public `is_sync_managed_bundled_agent`, updating its
  only consumer, `sync.py`.
- Switch the new-row branch in `sync_bundled_agents` from `upsert_with_steps` to
  `upsert_from_sync`.
- Whole-row immutability for selector-carrying rows follows position (x). A rename, a description
  edit, a rule patch, a restore or a re-tag of such a row is refused outside the sync. The row
  changes through its template and a restart.
- Rejected: per-route checks (every route ends in the manager); comparing the row with the
  bundled YAML at launch time (a second mechanism, and it fails closed on an unsynced template
  edit).

Verification:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/storage/definitions/test_agents_manager.py tests/agents/test_agents_sync.py tests/servers/routes/test_agents_routes.py tests/mcp_proxy/tools/test_workflows_agents.py -q`
(the last path is included if present; the executor locates the MCP definition-tool tests with
`gcode grep -lw create_agent_definition tests/`).

**Acceptance:**

- 1.2.1 - `AgentDefinitionBody` accepts an optional `sandbox_profile`, and a bundled template's value survives sync into the stored row. symbol: `AgentDefinitionBody`. test: `tests/agents/test_agents_sync.py::TestSyncBundledAgents::test_sync_preserves_sandbox_profile`.
- 1.2.2 - Every non-sync write that brings in a body with `sandbox_profile` is refused: create, update, upsert_with_steps, duplicate. test: `tests/storage/definitions/test_agents_manager.py::test_non_sync_writes_refuse_sandbox_profile`.
- 1.2.3 - A row carrying `sandbox_profile` is immutable outside the sync: update, toggle, restore, set_step_workflow and the moves all refuse. test: `tests/storage/definitions/test_agents_manager.py::test_selector_row_is_immutable_outside_sync`.
- 1.2.4 - The sync creates, updates and restores selector-carrying rows through sync entry points only, and the predicate is public. symbol: `is_sync_managed_bundled_agent`. test: `tests/agents/test_agents_sync.py::TestSyncBundledAgents::test_sync_new_row_uses_sync_entry_point`.

## P2: Resolution at spawn and resume
`kind: framing`

1.3 resolves profiles at spawn and records the name, and 1.4 resolves the name again at resume.
Both keep the two large files from growing by moving the sandbox choice into new modules.

### 1.3 Resolve the profile at spawn [category: code] (depends: 1.1, 1.2)
`kind: deliverable`

Targets:
- `src/gobby/agents/sandbox_profiles.py`
- `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py::spawn_agent_impl`
- `tests/agents/test_sandbox_profiles.py`
- `tests/mcp_proxy/tools/spawn_agent/test_sandbox_profile_spawn.py`

Consumers unchanged:
- `src/gobby/servers/routes/agent_spawn.py` — no-edit-reason: it passes no `caller_session_id`, so the resolver refuses `enabled: false` profiles there (Decision 15) with no route change.
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py` — no-edit-reason: it already passes `caller_session_id`, `agent_body` and `agent_lookup_name` to `spawn_agent_impl`.
- `src/gobby/ask/agents.py` — no-edit-reason: passes a managed runtime profile, whose branch stays first and untouched.
- `src/gobby/dispatch/spawn.py` — no-edit-reason: same call signature; bundled definitions without a selector keep today's policy.
- `src/gobby/feedback/agent.py` — no-edit-reason: same call signature; the feedback reviewer definition has no selector.
- `src/gobby/scheduler/executor.py` — no-edit-reason: same call signature; no caller session, so it cannot launch an `enabled: false` profile, which is intended.
- `tests/agents/test_backend_ingress.py` — no-edit-reason: exercises no selector.
- `tests/agents/test_local_context_setup.py` — no-edit-reason: exercises no selector.
- `tests/mcp_proxy/tools/spawn_agent/test_error_handling.py` — no-edit-reason: exercises no selector.
- `tests/mcp_proxy/tools/spawn_agent/test_factory.py` — no-edit-reason: exercises no selector.
- `tests/mcp_proxy/tools/spawn_agent/test_initial_variables.py` — no-edit-reason: exercises no selector.
- `tests/mcp_proxy/tools/test_agents_spawn_tools.py` — no-edit-reason: exercises no selector.
- `tests/mcp_proxy/tools/test_spawn_agent_impl_provider.py` — no-edit-reason: exercises no selector.
- `tests/tasks/test_plan_gate.py` — no-edit-reason: exercises no selector.
- `tests/workflows/test_step_snapshot_semantics.py` — no-edit-reason: exercises no selector.

**Research context:**
- The current choice is in `spawn_agent_impl` near line 393:
  `effective_sandbox_config = managed_runtime_profile.sandbox_config if managed_runtime_profile is not None else apply_write_grant(agent_sandbox_config(daemon_config), write_grant)`.
  `spawn_agent_impl` already receives `caller_session_id` (used by `authorize_write_grant`),
  `session_manager`, `db`, `agent_body` and `agent_lookup_name`.
- The grant is stored as `resume_metadata[GRANT_KEY] = write_grant` near line 636. The profile
  name is stored the same way.
- `agent_sandbox_config` and `daemon_owned_sandbox_config` live in `src/gobby/agents/sandbox.py`.
  `Session.sandbox_enabled` (`src/gobby/storage/session_models.py`) records whether a session runs
  sandboxed.
- `AgentDefinitionManager.get_by_name(name, project_id=...)` returns the row. Resolution keys on
  `agent_body.name`, so fallback agents (`_factory.py`, near lines 483-515) resolve their own
  profile.
- `sandbox_config_for_spawn` (`src/gobby/agents/spawn_cache_policy.py`) later merges project and
  cache write paths and forces `allow_package_registries=True` for enabled configs. It is
  unchanged.

**Approach:**
- The new module `src/gobby/agents/sandbox_profiles.py` owns the selection moved out of
  `_implementation.py`. It defines `SandboxProfileError(ValueError)` with stable codes
  (`sandbox_profile_unknown`, `sandbox_profile_unmanaged_row`, `sandbox_profile_requires_rules`,
  `sandbox_profile_caller_sandboxed`) and
  `resolve_agent_sandbox(daemon_config, agent_body, *, db, project_id, caller_session, write_grant) -> tuple[SandboxConfig, str | None]`.
  In order:
  1. With no `agent_body.sandbox_profile`, return `apply_write_grant(agent_sandbox_config(daemon_config), write_grant)` and `None`.
  2. Look up the profile in `daemon_config.agent_sandbox.profiles`. If it is missing, raise `sandbox_profile_unknown` (Decision 7).
  3. Load the row by `agent_body.name`. If it is missing or not `is_sync_managed_bundled_agent`, or its stored `sandbox_profile` differs from the body's, raise `sandbox_profile_unmanaged_row` (Decision 14).
  4. If the profile has `enabled: false` and `agent_body.workflows.rules` is empty, raise `sandbox_profile_requires_rules` (Decision 6).
  5. If the profile has `enabled: false` and `caller_session` is `None` or has `sandbox_enabled` true, raise `sandbox_profile_caller_sandboxed` (Decision 15).
  6. Return `apply_write_grant(daemon_owned_sandbox_config(profile), write_grant)` and the name.
- `spawn_agent_impl` replaces its non-managed branch with one call to `resolve_agent_sandbox`,
  mapping `SandboxProfileError` to `{"success": False, "error": "<code>:<detail>"}` before any run
  is reserved. It stores `resume_metadata["sandbox_profile"] = name` next to the grant.
  `caller_session` comes from `session_manager.get(caller_session_id)` and never from the
  `parent_session_id` argument. The proxy injects a parent only when the call carries none, so a
  supplied parent is chosen by the caller.
- The net line change in `_implementation.py` must be zero or less. It stays below 964 lines
  because the selection expression and the grant-application branch move to
  `sandbox_profiles.py`.
- The managed-runtime-profile branch stays first and untouched (Decision 9).

**Acceptance:**

- 1.3.1 - Without a selector the spawn gets exactly today's `agent_sandbox` policy plus the write grant; with a known selector it gets the named profile. symbol: `resolve_agent_sandbox`. test: `tests/agents/test_sandbox_profiles.py::test_no_selector_uses_default_and_selector_uses_profile`.
- 1.3.2 - An unknown profile name refuses the spawn and never falls back. test: `tests/agents/test_sandbox_profiles.py::test_unknown_profile_refuses`.
- 1.3.3 - A selector on a row that is not sync-managed, including a user-tagged row inserted directly by the test, is refused. test: `tests/agents/test_sandbox_profiles.py::test_unmanaged_row_selector_refuses`.
- 1.3.4 - An `enabled: false` profile requires non-empty `workflows.rules`. test: `tests/agents/test_sandbox_profiles.py::test_unsandboxed_profile_requires_rules`.
- 1.3.5 - A sandboxed caller, or a spawn with no authenticated caller session, cannot launch an `enabled: false` profile; an unsandboxed caller can, and a supplied `parent_session_id` never counts as the caller. test: `tests/mcp_proxy/tools/spawn_agent/test_sandbox_profile_spawn.py::test_caller_sandbox_state_gates_unsandboxed_profile`.
- 1.3.6 - The spawn records the profile name in resume metadata, and `_implementation.py` does not grow. test: `tests/mcp_proxy/tools/spawn_agent/test_sandbox_profile_spawn.py::test_spawn_records_sandbox_profile_name`. file: `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`.

### 1.4 Resolve the profile again at resume [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `src/gobby/agents/resume_sandbox.py`
- `src/gobby/agents/resume_executor.py::resume_agent_run`
- `tests/agents/test_resume_executor.py::*` — scope-reason: add the three re-resolution tests beside the existing resume tests

Consumers unchanged:
- `src/gobby/dispatch/daemon_resume.py` — no-edit-reason: it calls `resume_agent_run` with the same arguments and handles the refusal through the existing `ResumeAgentResult(False, error=...)` shape.
- `src/gobby/runner_lifecycle_agents.py` — no-edit-reason: same `resume_agent_run` call and result shape.
- `src/gobby/runner_lifecycle_reconcile.py` — no-edit-reason: same `resume_agent_run` call and result shape.
- `tests/ask/test_permissions.py` — no-edit-reason: the Ask path keeps its managed profile branch.
- `tests/dispatch/test_daemon_resume.py` — no-edit-reason: covers dispatch behavior, which is unchanged.
- `tests/test_runner_lifecycle.py` — no-edit-reason: covers lifecycle behavior, which is unchanged.
- `tests/test_runner_lifecycle_restart_replay.py` — no-edit-reason: covers replay behavior, which is unchanged.

**Research context:**
- `resume_agent_run` (`src/gobby/agents/resume_executor.py`, near lines 391-395) builds
  `sandbox_config = managed_runtime_profile.sandbox_config if managed_runtime_profile is not None else coerce_sandbox_config(resume_metadata.get("sandbox_config"))`,
  then calls `prepare_sandbox_launch` (near line 415) and writes the resulting SRT settings hash
  to the session.
- There is no agent policy-hash comparison. Only web chat compares
  `daemon_owned_sandbox_policy_hash`.
- `daemon_config` is a parameter of `resume_agent_run`. The stored grant is revalidated near line
  163 (`revalidate_write_grant`). The resume environment keeps the spawn cache variables
  (`_RESUME_METADATA_ENV_KEYS = frozenset(SPAWN_CACHE_ENV_VARS)` in `agents/resume_metadata.py`),
  so `sandbox_config_for_spawn` can rebuild the cache and project write paths.
- `resume_executor.py` is 972 lines and must not grow.

**Approach:**
- Move the non-managed sandbox choice out of `resume_agent_run` in `resume_executor.py` into
  the new module `src/gobby/agents/resume_sandbox.py`. It provides
  `resume_sandbox_config(daemon_config, resume_metadata, *, env, project_path) -> SandboxConfig`.
  - It reads `resume_metadata.get("sandbox_profile")`.
  - When the name is present, it looks up the profile. If the profile is missing, it raises
    `SandboxProfileError("sandbox_profile_missing_on_resume")` (Decision 5, fail closed).
  - When the name is absent, it resolves the top-level `agent_sandbox`.
  - It then applies `apply_write_grant` with `resume_metadata.get(GRANT_KEY)` and passes the
    result through `sandbox_config_for_spawn(..., resume_metadata_json=None)` so the project and
    cache paths are rebuilt.
  - The stored `sandbox_config` snapshot is no longer read for non-managed runs. It stays written
    for diagnostics.
- On resume, the rules and caller guards from 1.3 are not re-run. The run was authorized at spawn,
  the row is immutable outside the sync (1.2), and the resuming actor is the daemon.
- `resume_agent_run` maps the error to `ResumeAgentResult(False, error=...)` through its existing
  `_rollback_prepared_resume` path. Its net line change must be zero or less.
- Rejected: replaying the snapshot (a tightened policy could be dodged by resuming); refusing on a
  hash mismatch (Josh chose re-resolution).

Verification:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/agents/test_resume_executor.py tests/agents/test_sandbox_profiles.py -q`.

**Acceptance:**

- 1.4.1 - Resume re-resolves the recorded profile, so an edited profile takes effect for the resumed run. symbol: `resume_sandbox_config`. test: `tests/agents/test_resume_executor.py::test_resume_reresolves_edited_sandbox_profile`.
- 1.4.2 - Resuming a run whose profile has since been deleted is refused and rolled back. test: `tests/agents/test_resume_executor.py::test_resume_refuses_deleted_sandbox_profile`.
- 1.4.3 - A run with no recorded profile resumes on the current top-level `agent_sandbox` with its grant and cache paths rebuilt, and `resume_executor.py` does not grow. test: `tests/agents/test_resume_executor.py::test_resume_without_profile_uses_current_default`. file: `src/gobby/agents/resume_executor.py`.

## P3: Documentation
`kind: framing`

### 1.5 Document profiles in the sandboxing guide [category: docs] (depends: 1.4)
`kind: deliverable`

Targets:
- `docs/guides/sandboxing.md`

**Research context:** `docs/guides/sandboxing.md` documents both backends (near lines 85-102),
the `agent_sandbox`/`web_chat_sandbox` parameter table (near lines 129-160, which notes that
`mode` affects only provider-native and that SRT rejects `allow_network: true`), and the order in
which project `extra_write_paths` merge (near lines 190-199).

**Approach:** Add a "Named agent profiles" section covering:
- the `agent_sandbox.profiles` shape and the bundled `research` profile;
- `sandbox_profile` in bundled definitions;
- the sync-only write rule;
- the four spawn refusal codes and the resume refusal;
- why open web access requires `enabled: false` (the SRT allowlist limitation, with its error text).

**Acceptance:**

- 1.5.1 - The guide documents profile configuration, definition selection, the write and caller guards, and the refusal codes. behavior: "Named agent profiles" in `docs/guides/sandboxing.md`.

## P4: Verification
`kind: framing`

### 1.6 End-to-end check
`kind: verification`

After 1.1-1.5 land, the PD restarts the daemon, announcing it with a `global` `send_message`.
Then:
1. `uv run gobby config get agent_sandbox.profiles` (or the config UI) shows `research` with
   `enabled: false`.
2. Until #22902 ships a bundled definition with `sandbox_profile: research` and non-empty rules,
   a spawn of an existing agent behaves as before, which is the no-selector regression.
3. `create_agent_definition` with `sandbox_profile` returns the `sandbox_profile is sync-owned`
   refusal.
4. After #22902 lands, spawn the research definition from an unsandboxed pane: it launches with
   `sandbox_enabled` false. The same spawn from a sandboxed worker returns
   `sandbox_profile_caller_sandboxed`.

Rollout needs no migration and no data backfill. Rows with no selector are untouched, and the
first start after the cutover runs `sync_bundled_agents` under the new guard.
