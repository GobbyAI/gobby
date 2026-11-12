Plan artifact: `.gobby/plans/operator-friction-consolidation.md`

# Single-worktree implementation and workspace recovery

**Plan ID:** operator-friction-consolidation

## P1: Scope and implementation decisions
`kind: framing`

Use one worktree from the local `0.5.0` tip, on `fix/operator-friction-consolidation`. Reuse existing task IDs and retain task-sized commits.

Implement #25912, #25913, #25828, #25854, #25768, and #25863. Replace #25833’s original design with CI-backed E2E during **implementation-root epic review**. Add the workspace and clear-lifecycle changes discussed here.

Exclude:

- #25850: explicitly removed by #25908.
- #25864: no dedicated E2E runner seat.
- #25827: unsandboxed launches remain operator-started.
- #25654: preserve installation of `features.worktrees = false`; no foreign-worktree adoption.
- #25872: the original transcript now proves its cwd; no speculative parser change.
- #25890: cleanup bookkeeping, with no scratch worktrees currently registered.

`default` retains its normal instructions and uses provider-native scratchpads. It receives no agent workspace, workspace-use guidance, or workspace-scoping rules.

### 1. Landing-time binary versions — #25912
`kind: deliverable`

Targets:
- `src/gobby/tasks/land_commit.py::*` — scope-reason: integrate mechanical version generation and receipts throughout landing.
- `src/gobby/install/bin_release_identity.py::*` — scope-reason: reuse dependency closure and release identity verification.
- `src/gobby/tasks/landing_versions.py::*` — scope-reason: generate and reproduce the complete version-only transformation through an isolated index.
- `tests/tasks/test_landing_versions.py::*` — scope-reason: verify generation, tampering, sequential integration and evidence reuse together.
- `tests/tasks/test_land_commit.py::*` — scope-reason: update nonintegration landing expectations and preserve existing landing regressions.
- `tests/install/test_binary_release_versions.py::*` — scope-reason: replace obsolete manual-bump admission checks with affected dependency closure checks.
- `tests/install/test_version_pins.py::*` — scope-reason: remove duplicated numerical floors while preserving manifest and lockfile consistency checks.
- `tests/utils/test_daemon_git_inventory.py::*` — scope-reason: register synchronous version Git boundaries with off-loop landing and closure entry points.
- `src/gobby/tasks/closure_snapshot.py::*` — scope-reason: verify version transformation before admitting receipt-based evidence reuse.
- `src/gobby/install/shared/workflows/rules/task-closure/coding.yaml::*` — scope-reason: admit validation evidence only through a verified version transformation.
- `src/gobby/install/shared/workflows/rules/task-closure/integrated-validation.yaml::*` — scope-reason: define the verified version-only validation exemption.
- `src/gobby/install/shared/skills/gobby/references/agents/procedures/release-manager.md`
- `src/gobby/install/version_pins.py::*` — scope-reason: expose pin-derived floors and version metadata.
- `AGENTS.md`
- `crates/AGENTS.md`
- `.gobby/workflows/rules/project-operating-policy.yaml::*` — scope-reason: update project-wide release guidance to landing-time version generation.
- `.gobby/gcode.json::*` — scope-reason: declare the maintained project-definition index allowlist.
- `.gitignore`

**Granularity:** One landing transaction owns generation, verification and receipt provenance; splitting these would admit unverified evidence. Focused Git-fixture unit and landing integration tests verify it together.

**Research context:** Landing currently detects missing bumps and refuses them. Existing dependency-closure and release-identity helpers already cover the six managed binaries.

**Changes:**

- Generate patch bumps inside `land_commit`, against the locked integration tip, for affected binary dependency closures.
- Update crate versions, corresponding lockfile entries, and managed pins. Keep version floors derived from pins and remove duplicated numeric maintenance where possible.
- Include unpublished managed binaries when their installation identity requires a new version; publishing remains a separate operation.
- Generate the mechanical commit through an isolated Git index, without another worktree or edits to the caller’s branch.
- Store the reviewed SHA, generated bump SHA, and landed tip in the landing receipt.
- Accept existing evidence across that transformation only after reproducing and verifying the exact permitted version-only change. An arbitrary descendant, source edit, or changed integration base requires fresh evidence.
- Bump only when integrating into `0.5.0`, not when landing children into an epic branch.
- Update the branch-version instructions and affected closure rules.
- Keep maintained project-local definitions Git-backed and include their YAML, Markdown and JSON in the code index through its existing hidden-path allowlist. Exclude generated state and local MCP instances.

**Primary targets:** `land_commit.py`, `bin_release_identity.py`, closure evidence matching, and a new focused landing-version module.

**Acceptance:**

- 1.1 - Two candidates changing the same binary land sequentially without branch version conflicts. file: `src/gobby/tasks/land_commit.py`.
- 1.2 - Altered or unrelated mechanical commits are rejected; retries do not bump twice. file: `src/gobby/tasks/landing_versions.py`.

**Bootstrap:** Land and activate this Python-only change before introducing the batch’s Rust changes. Keep the same worktree, relinking and reactivating its registration for the remaining implementation.

### 2. Checkout-aware edit attribution — #25913 (depends: 1)
`kind: deliverable`

Targets:
- `src/gobby/hooks/event_handlers/_tool.py::*` — scope-reason: extract shared edit attribution handling.
- `src/gobby/hooks/event_handlers/edit_attribution.py::*` — scope-reason: extract cohesive mutation enforcement and recording from the tool-event coordinator.
- `src/gobby/workflows/checkout_task_attribution.py::*` — scope-reason: centralize frozen checkout ownership and conservative dirty-check exclusions.
- `src/gobby/workflows/session_edit_ledger.py::*` — scope-reason: use frozen checkout attribution for task ledger writes.
- `src/gobby/workflows/task_tool_bindings.py::*` — scope-reason: freeze registered checkout ownership at tool start for delayed completion.
- `src/gobby/mcp_proxy/tools/tasks/_close_context.py::capture_attribution`
- `src/gobby/mcp_proxy/tools/tasks/_claim_release.py::_uncommitted_refusal`
- `tests/mcp_proxy/tools/tasks/test_checkout_edit_attribution.py::*` — scope-reason: cover leaf, shared epic, delayed completion, released claims, close/release and main-checkout attribution.
- `tests/hooks/test_tool_handlers.py::*` — scope-reason: update extracted handler mocks and verify project-definition indexing notification.
- `tests/hooks/test_opaque_program_attribution.py::*` — scope-reason: migrate the extracted shell checkout helper import.
- `src/gobby/plans/symbol_targets.py::*` — scope-reason: validate checkout-visible immutable metadata and matching symbols after parent index movement.
- `src/gobby/plans/symbol_index_scope.py::*` — scope-reason: extract checkout scope resolution and pinned index selection.
- `src/gobby/code_index/_storage/files.py::CodeIndexFileStorageMixin.get_pinned_file`
- `src/gobby/code_index/_storage/symbols.py::CodeIndexSymbolStorageMixin.get_symbols_for_file`
- `tests/plans/test_symbol_targets.py::*` — scope-reason: cover inherited pins after parent advancement and preserve overlay shadowing.
- `tests/plans/test_consumer_coverage.py::*` — scope-reason: adapt immutable symbol selection fixtures.
- `tests/plans/test_semantic_lint.py::*` — scope-reason: adapt indexed-symbol storage fixture.
- `tests/code_index/test_code_index_storage.py::*` — scope-reason: verify immutable metadata and symbols against PostgreSQL with local shadowing.
- `tests/mcp_proxy/tools/tasks/test_close_attribution.py::*` — scope-reason: extend the existing no-foreign-owner fixture to cover checkout ownership lookup.

Consumers unchanged:
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py` — no-edit-reason: the capture API and returned attribution shape remain unchanged.
- `tests/mcp_proxy/tools/tasks/test_close_candidate.py` — no-edit-reason: its capture stub and candidate-close contract remain unchanged.

Split `_tool.py` into `src/gobby/hooks/event_handlers/edit_attribution.py` before expanding attribution behavior.

Split checkout/index scope resolution from `src/gobby/plans/symbol_targets.py` into `src/gobby/plans/symbol_index_scope.py` while fixing the validation blocker found during this deliverable. Inherited metadata and symbols must resolve the pinned version even after the parent index advances; local state retains precedence. **Granularity:** this found-work repair is included because project-aware validation of the current deliverable cannot otherwise complete. The existing storage layer owns immutable records; plan validation consumes its explicit pin/hash reads.

**Research context:** The edit ledger currently chooses a task from claim history, even when the edit’s registered checkout identifies another task.

**Changes:**

- Resolve attribution from the canonical registered checkout and ownership at tool start.
- In a claimed leaf’s worktree, attribute to that leaf regardless of a later unrelated claim.
- In a shared epic worktree, retain the selected claimed descendant; do not attribute every child edit to the epic.
- Preserve last-claim behavior in the main checkout and unregistered paths.
- Use the same resolution for pre-tool enforcement, delayed completion, ledger recording, close, and claim release.
- Ignore another claimed task’s worktree dirt when closing the current task. Retain conservative checks for ambiguous or foreign ownership.
- Extract edit-attribution handling from the large tool-event module while making this change.
- Notify gcode about maintained project-definition edits; the index owns hidden-path eligibility.

**Primary targets:** the edit ledger, tool-event attribution, close attribution, and claim-release checks.

**Acceptance:**

- 2.1 - Cover two claims, separate trees, shared epic trees, delayed completion, released claims, and main-checkout fallback. file: `src/gobby/hooks/event_handlers/edit_attribution.py`.

### 3. Current-session definition activation — #25828 (depends: 2)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/admin_cold_start.py::*` — operation: delete — scope-reason: retire cold-start operation and counterpart launch.
- `src/gobby/workflows/agent_models.py::*` — scope-reason: remove cold-start fields and their validation.
- `src/gobby/workflows/agent_resolver.py::*` — scope-reason: replace alias resolution with eligible current-session choices.
- `src/gobby/skills/capability_routing.py::*` — scope-reason: route explicit activation commands to existing tools.
- `src/gobby/hooks/event_handlers/_agent.py::*` — scope-reason: remove alias resolver from typed command routing.
- `src/gobby/hooks/event_handlers/_tool.py::*` — scope-reason: remove alias resolver from native skill routing.
- `src/gobby/mcp_proxy/tools/agents_registry.py::*` — scope-reason: remove retired tool registration.
- `src/gobby/mcp_proxy/tools/agents_spawn_tools.py::*` — scope-reason: treat direct definition tool calls as explicit current-session role changes.
- `src/gobby/mcp_proxy/tools/apply_agent_definition.py::*` — scope-reason: return eligible choices before mutations for invalid definitions.
- `src/gobby/install/shared/workflows/agents/orchestrator.yaml::*` — scope-reason: remove retired alias declaration and version its activation contract.
- `src/gobby/install/shared/workflows/rules/tool-hygiene/agent-activation-authority.yaml::*` — scope-reason: remove retired tool from the declarative authority policy.
- `src/gobby/install/shared/skills/gobby/SKILL.md`
- `src/gobby/install/shared/skills/gobby/references/agents/personas.md`
- `src/gobby/install/shared/skills/gobby/references/pipelines/runbooks.md`
- `docs/guides/agents.md`
- `docs/guides/runbooks.md`
- `docs/reference-audit/agents.json::*` — scope-reason: remove the retired tool from source-audit inventory.
- `docs/contracts/parity-ledger-status.d/sessions-agents.json::*` — scope-reason: remove retired tool classification before regenerating parity ledger.
- `docs/contracts/parity-ledger.md`
- `tests/mcp_proxy/test_admin_cold_start.py::*` — operation: delete — scope-reason: remove tests of the retired operation.
- `tests/mcp_proxy/tools/test_apply_agent_definition.py::*` — scope-reason: cover public role changes, idempotence and eligible invalid-name choices with synthetic definitions.
- `tests/skills/test_capability_routing.py::*` — scope-reason: cover explicit activation routing and project override choices.
- `tests/skills/test_gobby_skill_router.py::*` — scope-reason: cover typed provider activation commands.
- `tests/hooks/test_tool_handlers.py::*` — scope-reason: cover native skill activation routing.
- `tests/agents/test_bundled_agent_definition_validity.py::*` — scope-reason: validate discovered YAML generically and parse each rule through the engine dry run.
- `tests/agents/test_bundled_model_pairing.py::*` — scope-reason: remove obsolete alias-only bundled assertions.
- `tests/agents/test_bundled_seat_guards.py::*` — scope-reason: remove assertions for deleted model fields.
- `tests/servers/routes/test_agents_routes.py::*` — scope-reason: remove deleted fields from the API authority inventory fixture.
- `web/src/components/activity/agents/AgentsTabActions.ts::*` — scope-reason: remove deleted fields from definition duplication authority inventory.
- `src/gobby/agents/agent_workspaces.py::*` — scope-reason: extract session binding before extending activation policy.
- `src/gobby/agents/workspace_bindings.py::*` — scope-reason: own explicit activation and session-start binding policy in one focused module.
- `src/gobby/hooks/event_handlers/_session_start/flow.py::*` — scope-reason: move binder imports to the extracted owner.
- `src/gobby/hooks/event_handlers/_session_start/materialize.py::*` — scope-reason: move binder imports to the extracted owner.
- `tests/agents/test_agent_workspaces.py::*` — scope-reason: migrate binding import and preserve workspace characterization.
- `tests/agents/test_sandbox_workspace_grants_persistent.py::*` — scope-reason: migrate binding import and preserve grants characterization.
- `tests/hooks/test_session_start_reactivation.py::*` — scope-reason: seed legacy unbound seats directly for compact and resume recovery coverage.

**Research context:** `apply_agent_definition` already applies full definitions. The older cold-start layer adds alias and counterpart-pane behavior.

**Granularity:** one current-session activation outcome owns its router adapters, existing activation tool, eligibility model, removed legacy operation and documentation carriers. The tool explicitly authorizes the existing transactional role-change path; ambient session-start recovery keeps its existing role-change safeguard. Focused synthetic behavior tests preserve persona overlays and idempotence. Bundled rule checks are limited to generic YAML validation and engine dry runs.

**Changes:**

- Route `/gobby agents <definition>` directly through `apply_agent_definition` for the current session.
- Split session binding from `agent_workspaces.py` into `src/gobby/agents/workspace_bindings.py`; prepare workspace binding, persistence, and injection changes under the same identity lock as activation. Preserve existing folders as recovery data and repeated-activation idempotence.
- Keep `/gobby persona <persona>` as the persona-overlay operation.
- Require an enabled definition supporting the persona surface and no pipeline requirement.
- Remove the `admin` alias, cold-start tool, `cold_start_commands`, and counterpart-opening configuration and code.
- Return eligible choices for invalid names without changing session state.
- Ensure activation installs the definition’s rules, workflows, capabilities, prompts, and workspace policy.
- Preserve manual unsandboxed pane launch; add no spawn bypass or Telegram approval infrastructure.

**Acceptance:**

- 3.1 - Activation changes the session’s effective identity and enforcement, opens no pane, and is idempotent when repeated. test: `tests/mcp_proxy/tools/test_apply_agent_definition.py::test_public_activation_switches_current_definition_without_pane`.

### 4. Clear semantics and default scratch space — new lifecycle work (depends: 3)
`kind: deliverable`

Targets:
- `src/gobby/hooks/event_handlers/_session_end.py::*` — scope-reason: implement and verify the confirmed-clear lifecycle and effective default workspace exemption.
- `src/gobby/hooks/event_handlers/_session_start/flow.py::*` — scope-reason: implement and verify the confirmed-clear lifecycle and effective default workspace exemption.
- `src/gobby/hooks/event_handlers/_session_start/materialize.py::*` — scope-reason: implement and verify the confirmed-clear lifecycle and effective default workspace exemption.
- `src/gobby/hooks/event_handlers/_session_start/agents.py::*` — scope-reason: implement and verify the confirmed-clear lifecycle and effective default workspace exemption.
- `src/gobby/agents/workspace_bindings.py::*` — scope-reason: implement and verify the confirmed-clear lifecycle and effective default workspace exemption.
- `src/gobby/agents/agent_workspaces.py::*` — scope-reason: implement and verify the confirmed-clear lifecycle and effective default workspace exemption.
- `src/gobby/mcp_proxy/tools/apply_agent_definition.py::*` — scope-reason: implement and verify the confirmed-clear lifecycle and effective default workspace exemption.
- `src/gobby/sessions/clear_run_lineage.py::*` — scope-reason: implement and verify the confirmed-clear lifecycle and effective default workspace exemption.
- `src/gobby/workflows/selectors.py::*` — scope-reason: implement and verify the confirmed-clear lifecycle and effective default workspace exemption.
- `src/gobby/workflows/engine/core.py::*` — scope-reason: implement and verify the confirmed-clear lifecycle and effective default workspace exemption.
- `tests/hooks/test_clear_identity.py::*` — scope-reason: implement and verify the confirmed-clear lifecycle and effective default workspace exemption.
- `tests/hooks/test_clear_successor_seat.py::*` — scope-reason: implement and verify the confirmed-clear lifecycle and effective default workspace exemption.
- `tests/hooks/test_session_end_handlers.py::*` — scope-reason: implement and verify the confirmed-clear lifecycle and effective default workspace exemption.
- `tests/agents/test_agent_workspace_lifecycle.py::*` — scope-reason: implement and verify the confirmed-clear lifecycle and effective default workspace exemption.
- `tests/workflows/test_selectors.py::*` — scope-reason: implement and verify the confirmed-clear lifecycle and effective default workspace exemption.
- `tests/servers/test_managed_clear_identity.py::*` — scope-reason: replace removed spawned-clear forwarding expectations with preserved authentication after refused transfer.
- `src/gobby/hooks/event_handlers/clear_identity.py::*` — scope-reason: centralize semantic reset and cancellation dispatch.
- `docs/contracts/session-boundary.md`
- `src/gobby/workflows/engine/rule_selection.py::*` — scope-reason: own enabled-rule loading and applicability filtering.
- `src/gobby/hooks/event_handlers/_session_start/diagnostics.py::*` — scope-reason: own lifecycle and timing diagnostics extracted from flow.
- `src/gobby/install/shared/workflows/rules/roles/inject-agent-workspace.yaml::*` — scope-reason: declare workspace applicability outside default and version the changed policy.
- `src/gobby/install/shared/workflows/rules/roles/agent-workspace-scope.yaml::*` — scope-reason: declare workspace applicability outside default and version the changed policy.
- `src/gobby/install/shared/workflows/rules/roles/workspace-only-write-scope.yaml::*` — scope-reason: declare workspace applicability outside default and version the changed policy.
- `src/gobby/install/shared/workflows/rules/roles/inject-standing-directives.yaml::*` — scope-reason: declare workspace applicability outside default and version the changed policy.
- `src/gobby/install/shared/workflows/rules/roles/block-temp-scratchpad-writes.yaml::*` — scope-reason: keep provider-native scratchpads available to default.

**Granularity:** Operator reset and spawned cancellation are the two outcomes of one confirmed-clear boundary. Keep their ingress guards, transactional ownership check, and cancellation adapter in this lifecycle deliverable; reuse the existing cancellation state machine rather than creating another one. Default workspace allocation and selection must change together to prevent recovery from restoring stale scope.

Split lifecycle and timing diagnostics from `src/gobby/hooks/event_handlers/_session_start/flow.py` into `src/gobby/hooks/event_handlers/_session_start/diagnostics.py` before extending clear ingress.

Split rule loading and selection from `src/gobby/workflows/engine/core.py` into `src/gobby/workflows/engine/rule_selection.py` before extending scope matching. The engine delegates to this mixin; selector scope matching remains the shared activation and evaluation policy.

**Research context:** Session-end handling owns termination; new `clear_identity.py` will centralize reset and spawned-seat cleanup integration. Preserve provider authentication and safeguards while clearing semantic definition state. Verify with isolated lifecycle fixtures.

Activation records explicit workflow-variable ownership separately from initialized runtime defaults, retaining that inventory across same-role definition refreshes. Clear uses the recorded inventory even after a definition changes or disappears, restores registered defaults for its explicit values, and preserves canonical task ledgers and runtime safeguards. The forced-default marker controls recovery selection only; each confirmed clear still removes any persona applied since the previous boundary.

**Changes:**

- For an operator-opened session, a confirmed `/clear` resets the semantic agent identity to `default`.
- Remove inherited definition state, persona state, step workflow, workspace bindings, and workspace injection flags. Prevent subsequent recovery from restoring the previous definition.
- Preserve normal default instructions, provider identity, authentication, and ordinary repository/task safeguards.
- Stop creating `default/session-<session_ref>` directories.
- Exclude workspace injection and workspace-scoping rules from the effective default definition, including installed overrides.
- A handoff with `clear=false` retains the session and definition.
- A clear in a spawned seat ends that agent run and tears down its sandbox/pane. It creates no default successor and does not mark the task complete.
- Reuse existing interrupted-run cleanup and coordinator notification.

**Acceptance:**

- 4.1 - Test operator clear, handoff without clear, clear with handoff, duplicate lifecycle events, and spawned-seat teardown. file: `src/gobby/hooks/event_handlers/clear_identity.py`.
- 4.2 - A default scratch operation neither allocates an agent workspace nor encounters a workspace-only rule. file: `tests/hooks/test_clear_identity.py`.

### 5. Stable persistent workspaces — new recovery work (depends: 4)
`kind: deliverable`

Targets:
- `src/gobby/agents/agent_workspaces.py::*` — scope-reason: extract binding and backup owners and integrate stable layout.
- `src/gobby/agents/workspace_bindings.py::*` — scope-reason: extend the extracted binding owner with stable persistent ownership and rotation.
- `src/gobby/agents/persistent_workspaces.py::*` — scope-reason: own serialized stable-folder acquisition, durable rotation and legacy migration.
- `src/gobby/agents/workspace_backups.py::*` — scope-reason: extract the complete nightly backup owner and document stable layout.
- `src/gobby/mcp_proxy/tools/apply_agent_definition.py::*` — scope-reason: acquire workspace ownership during activation and unchanged-definition resume with atomic refusal.
- `src/gobby/hooks/event_handlers/_session_start/materialize.py::*` — scope-reason: remove obsolete predecessor-binding argument after stable ownership replaces inheritance.
- `src/gobby/runner_init/orchestration.py::*` — scope-reason: register the extracted nightly backup owner.
- `src/gobby/runner_init/cron.py::*` — scope-reason: extract cron dependency initialization and handler registration from orchestration.
- `tests/test_runner_lifecycle.py::*` — scope-reason: move the heartbeat consumer and correct the stale scoped-backfill logger assertion found during characterization.
- `tests/test_runner_init.py::*` — scope-reason: move the retired-cron constant consumer and characterize unchanged initialization.
- `src/gobby/install/shared/workflows/rules/roles/inject-agent-workspace.yaml::*` — scope-reason: explain stable current and read-only history recovery with a version bump.
- `crates/gdaemon/src/nodes/agents/sandbox/mod.rs::*` — scope-reason: represent trusted persistent-workspace policy in native sandbox inputs.
- `crates/gdaemon/src/nodes/agents/sandbox/workspace_scope.rs::*` — scope-reason: project stable current and default exemption into native sandbox scopes.
- `crates/gdaemon/src/nodes/agents/launch.rs::*` — scope-reason: retain absent seat-folder authority on native launch.
- `crates/gdaemon/src/nodes/agents/sandbox/caches/tests.rs::*` — scope-reason: update the synthetic policy constructor.
- `crates/gdaemon/tests/node_agents.rs::*` — scope-reason: consume persistent policy from the generated parity corpus.
- `crates/gdaemon/tests/fixtures/node_srt/corpus.json::*` — scope-reason: regenerate synthetic workspace policy cases through the Python oracle.
- `tests/agents/test_persistent_workspace_binding.py::*` — scope-reason: verify ownership, rotation recovery, collisions, launch admission and default cleanup.
- `tests/agents/test_agent_workspace_backup_bounds.py::*` — scope-reason: move backup characterization to the extracted owner.
- `tests/agents/test_agent_workspace_commit_lock.py::*` — scope-reason: move backup lock characterization to the extracted owner.
- `tests/agents/test_agent_workspace_lifecycle.py::*` — scope-reason: verify stable persistent bindings through lifecycle boundaries.
- `tests/agents/test_agent_workspace_epics.py::*` — scope-reason: initialize synthetic non-default identities in epic workspace fixtures.
- `tests/agents/test_agent_workspace_skipped_paths.py::*` — scope-reason: retain exclusion characterization after extraction.
- `tests/agents/test_agent_workspaces.py::*` — scope-reason: retain nightly-only backup and non-default workspace characterization.
- `tests/agents/test_sandbox_workspace_grants_persistent.py::*` — scope-reason: verify stable current write grants and historical read scope after takeover.
- `tests/agents/test_sandbox_workspace_grants.py::*` — scope-reason: verify default exemption while preserving non-default and epic grants.
- `tests/agents/test_sandbox_workspace_scope.py::*` — scope-reason: verify stable current writes and sibling history reads with synthetic definitions.
- `tests/agents/test_node_srt_corpus.py::*` — scope-reason: generate and verify synthetic stable-workspace and default-exemption parity cases.
- `tests/mcp_proxy/tools/test_apply_agent_definition.py::*` — scope-reason: verify public activation ownership refusal and repeat activation.
- `tests/utils/test_daemon_git_inventory.py::*` — scope-reason: track the moved synchronous Git initializer.

Split `agent_workspaces.py` into `src/gobby/agents/workspace_bindings.py` and `src/gobby/agents/workspace_backups.py` before extending stable ownership.

Move cron initialization from `src/gobby/runner_init/orchestration.py` into `src/gobby/runner_init/cron.py` so the runner remains below the plan's growth threshold after its backup consumer changes. Preserve dependency failures, initialization ordering, handler registration and nightly cadence. Existing runner initialization/lifecycle tests characterize the extraction; a stale test logger target discovered in the baseline is corrected to its existing owner.

**Research context:** `persistent_workspace` already exists, but independent sessions still receive session-numbered folders.

The binding owner is `workspace_bindings.bind_session_workspace`; `definition_workspace_changes` participates in `commit_definition_changes`' identity transaction. New `persistent_workspaces.bind_persistent_workspace` owns the project/definition advisory lock and durable filesystem journal. Live data resides in `<definition>/current`, historical data in sibling `history/session-<seq>`, and ownership metadata in the daemon-only sibling Git directory outside backup content. Launch grants must acquire ownership before exposing current, including before SessionStart has established semantic identity. Default cleanup also clears stale persistent/injection flags when folder bindings are absent. Archive collisions refuse without merging or deleting either copy. Directory renames and journal replacements are fsynced before ownership completes.

Pre-launch ownership records its workspace definition separately from semantic identity, so a default-session cleanup cannot erase a newly admitted persistent launch. Cleanup follows the variable manager's advisory-lock order, rechecks identity under that lock and only removes empty top-level generated default session folders; nested recovery directories remain intact.

Consumer sweep: `gcode grep -w bind_session_workspace -m 50 src/gobby` identified SessionStart agents/flow/materialize plus public activation. Agents and flow already call the unchanged two-argument API; materialize's unused inheritance argument is removed. `gcode grep -w inherit_from -m 40 src/gobby` identified only that obsolete parameter/caller. Native `launch.policy_inputs` explicitly grants no seat-folder authority; preserve that restriction. Native `WorkspaceScope::resolve` and the generated Python/native parity corpus still need stable/default projection semantics for trusted policy inputs; no payload authority is added.

**Granularity:** Keep one leaf because activation, launch grants, rotation and recovery must enforce the same exclusive-owner invariant atomically. The backup extraction moves its existing nightly-only behavior and consumers together to keep the workspace module below the production ceiling; it adds no independent lifecycle or backup trigger. Native changes only project the same folder policy. The resulting modules separately own paths/grants, binding, persistent rotation and nightly backups.

Observed checks during implementation: 68 focused ownership/activation/lifecycle/grant tests passed; synthetic Python corpus regeneration passed on both platform projections; native SRT parity initially failed the new default exemption, then 14 focused native SRT tests passed after alignment. Final static, audit, generic YAML-engine and exact-candidate checks remain required.

**Changes:**

- For definitions declaring `persistent_workspace: true`, bind one stable folder under the existing project workspace anchor.
- On a new independent owner’s activation, move the previous owner’s live files into `history/session-<previous-session-ref>`, preserving existing history.
- Persist ownership and serialize rotation per project and definition. Repeated activation or resume by the same owner does not rotate again.
- Refuse concurrent takeover while another live owner holds the folder.
- Make interrupted rotation recoverable and idempotent; never overwrite an existing archive.
- Migrate legacy persistent session folders into that history layout without merging unrelated sessions’ files.
- Update sandbox grants, workspace references, recovery guidance, and backup exclusions for the stable layout.
- Preserve nightly-only backups; lifecycle events do not create workspace commits.
- Remove stale default bindings and empty generated default folders. Preserve nonempty historical default folders as inert recovery data rather than deleting their contents.

**Primary targets:** workspace binding and grant helpers, activation integration, workspace rules, and recovery references. Split the large workspace module into focused binding and backup modules.

**Acceptance:**

- 5.1 - Test fresh activation, takeover, same-owner resume, crash recovery, history collisions, project isolation, and refusal of a second live owner. file: `src/gobby/agents/workspace_bindings.py`.

### 6. Credential-file protection — #25854 (depends: 5)
`kind: deliverable`

Targets:
- `src/gobby/hooks/credential_file_policy.py::*` — scope-reason: credential operand policy and protected path matching are one new module.
- `src/gobby/workflows/workspace_read_targets.py::*` — scope-reason: expose shared normalized read operands, retain lexical paths, consume every normalized edit path, and extract pattern handling.
- `src/gobby/workflows/read_path_patterns.py::*` — scope-reason: directly extract bounded pattern expansion and lexical/canonical path resolution from the shared reader.
- `src/gobby/workflows/condition_helpers_sessions.py::agent_workspace_read_violation`
- `src/gobby/workflows/safe_evaluator.py::build_condition_helpers`
- `src/gobby/install/shared/workflows/rules/worker-safety/credential-file-protection.yaml::*` — scope-reason: declare the complete new mandatory policy and default exemption.
- `src/gobby/install/shared/workflows/rules/worker-safety/no-data-exfiltration.yaml::*` — scope-reason: remove overlapping bootstrap authority and bump the file version.
- `tests/hooks/test_credential_file_policy.py::*` — scope-reason: synthetic tool, path, identity, and native-child matrix plus normalization regressions.
- `tests/workflows/test_agent_workspace_scope.py::test_unreadable_directory_and_brace_limit_are_distinct`

**Research context:** `WorkflowHookHandler` retrieves stored session variables through trusted `_platform_session_id`; provider-supplied identity fields cannot grant an exemption. Native child payloads retain `agent_id` while using the authenticated parent session. Mandatory rules survive an empty definition selector; YAML supplies `agent_scope: ["*", "!default"]`. `tool_read_targets` reuses existing shell cwd, wrapper, and data-operand parsing. Its credential consumer requests lexical paths as well as resolved paths so named symlinks remain protected; normalized multi-file edit fields are checked in full. Preserve both lexical and canonical configured-home roots. The old spawned-only secret regex loses its overlapping bootstrap alternative so it cannot override the default exemption; other existing protections remain. Directly split pattern expansion and path resolution from `workspace_read_targets.py` into `read_path_patterns.py`; dependencies point from the shell/tool reader into the stateless pattern module, never back. Synthetic fixtures verify behavior without reading live credentials. Bundled YAML receives generic schema validation and engine parsing only.

**Consumer sweep:** `gcode grep -w 'tool_read_targets|_workspace_read_targets|credential_file_access' -m 50` found the new policy, safe-evaluator registry, workspace-scope consumer, new YAML, and synthetic tests; no stale private-name consumer remains. Workspace-scope behavior retains canonical-only resolution by default. Focused parsing and scope tests require no edits.

Consumers unchanged:
- `tests/workflows/test_agent_workspace_read_parsing.py` — no-edit-reason: unchanged public scope helper; existing regressions verify reader parity.
- `src/gobby/workflows/dry_run_validation.py` — no-edit-reason: helper registry is additive; call signature and existing entries remain unchanged.
- `src/gobby/workflows/engine/templating.py` — no-edit-reason: helper registry is additive with no caller contract change.
- `src/gobby/workflows/step_context.py` — no-edit-reason: helper registry is additive with no caller contract change.
- `tests/review_learning/test_review_evidence_gate.py` — no-edit-reason: existing helper-registry use has unchanged arguments and entries.
- `tests/workflows/test_approval_brace_guard.py` — no-edit-reason: existing helper-registry use remains valid.
- `tests/workflows/test_block_temp_scratchpad_writes.py` — no-edit-reason: existing helper-registry use remains valid.
- `tests/workflows/test_condition_helpers_paths.py` — no-edit-reason: existing helper-registry use remains valid.
- `tests/workflows/test_condition_helpers_task_state.py` — no-edit-reason: existing helper-registry use remains valid.
- `tests/workflows/test_database_deadline_retry.py` — no-edit-reason: existing helper-registry use remains valid.
- `tests/workflows/test_definition_pin_guard.py` — no-edit-reason: existing helper-registry use remains valid.
- `tests/workflows/test_handoff_pull_gate.py` — no-edit-reason: existing helper-registry use remains valid.
- `tests/workflows/test_monolith_guard.py` — no-edit-reason: existing helper-registry use remains valid.
- `tests/workflows/test_safe_evaluator.py` — no-edit-reason: existing helper-registry use remains valid.
- `tests/workflows/test_session_transcript_writes.py` — no-edit-reason: existing helper-registry use remains valid.
- `tests/workflows/test_tdd_gate_retirement.py` — no-edit-reason: existing helper-registry use remains valid.
- `tests/workflows/test_time_windows.py` — no-edit-reason: existing helper-registry use remains valid.

**Granularity:** This is one credential-access decision with no new persisted state or independent lifecycle owner. Its five production Python targets contain the detector, shared parser extraction, two existing consumers, and helper registration; the YAML declares applicability. Keep them in one security task so no intermediate commit activates an incomplete policy.

**Verification:** Before review fixes, the focused credential/parser/scope and generic YAML run passed 980 cases. Nine additional intended assertion failures reproduced globbed-parent, multi-file edit, and linked-home bypasses; fixes passed 136 credential/parser cases. Repeat focused checks after the pattern extraction, run Ruff/mypy and changed-test audits, then perform project-aware plan validation. Live verification remains content-free and requires both a non-default seat and its native child after activation.

**Changes:**

- Apply protection to every non-default definition and its provider-native subagents, including operator-opened persona sessions.
- Cover direct file tools and normalized shell invocations naming bootstrap, local token, `.secret_kek`, or `.secret_salt` paths.
- Use authenticated session/subagent identity and provider payload fields.
- Declare applicability and exemptions in YAML; use synthetic definitions in behavior tests.
- Keep ordinary Gobby CLI operations working when they read credentials internally.
- Refusals identify the policy without exposing contents.

**Acceptance:**

- 6.1 - Exercise the tool/path/session matrix using synthetic credential files, harmless control commands and default exemptions; live probes expose no contents. file: `tests/hooks/test_credential_file_policy.py`.

### 7. Nonblocking gclient project operations — #25768 (depends: 6)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_loop/projects.rs::*` — scope-reason: extract project operations into asynchronous jobs.
- `crates/gclient/src/app/attach.rs::*` — scope-reason: prevent a recovery-owned detach deadline from also requesting daemon reconnect; cover timer-first ordering.
- `crates/gclient/src/app/live_loop/project_operations.rs::*` — scope-reason: own project-close and worktree-removal chains and reducers.
- `crates/gclient/src/app/live_loop/project_navigation.rs::*` — scope-reason: own serialized navigation intent, generation checks and typed continuations.
- `crates/gclient/src/app/live_loop/jobs.rs::*` — scope-reason: add project operation job and outcome variants.
- `crates/gclient/src/app/live_loop/jobs_apply.rs::*` — scope-reason: apply generation-checked project outcomes.
- `crates/gclient/src/app/live.rs::*` — scope-reason: initialize navigation and removal state.
- `crates/gclient/src/app/live/relist.rs::*` — scope-reason: expose inventory membership for explicit terminal resolution.
- `crates/gclient/src/app/live_loop.rs::*` — scope-reason: dispatch and resume asynchronous navigation beside input and move the view adapter to projection.
- `crates/gclient/src/app/live_loop/actions.rs::*` — scope-reason: invalidate dismissed navigation and queue project actions.
- `crates/gclient/src/app/live_loop/actions/sidebar.rs::*` — scope-reason: pass the existing outcome channel to navigation actions.
- `crates/gclient/src/app/live_loop/daemon_ops.rs::*` — scope-reason: share kill batching and barrier ordering.
- `crates/gclient/src/app/live_loop/lifecycle.rs::*` — scope-reason: retire synchronous terminal termination and reuse asynchronous kills.
- `crates/gclient/src/app/live_loop/menu_dispatch.rs::*` — scope-reason: preserve typed menu continuations across project selection.
- `crates/gclient/src/app/live_loop/project_paths.rs::*` — scope-reason: resolve project registration from the navigation job.
- `crates/gclient/src/app/live_loop/terminal_location.rs::*` — scope-reason: resolve authoritative terminal placement without mutating the input loop.
- `crates/gclient/src/daemon/live.rs::*` — scope-reason: atomically fence navigation requests to their captured connection generation when selecting the outbound channel.
- `crates/gclient/src/daemon/live_workspace.rs::*` — scope-reason: expose generation-bound workspace attachment while preserving pending-attach cleanup.
- `crates/gclient/src/app/live_loop/projection.rs::*` — scope-reason: own the workspace view adapter beside layout projection.
- `crates/gclient/src/app/mod.rs::*` — scope-reason: store operation state and expose production reducers to integration fixtures.
- `crates/gclient/tests/client_loop.rs::*` — scope-reason: exercise asynchronous project flows and repair characterized detach fixtures.
- `crates/gclient/tests/attention_flow.rs::*` — scope-reason: await observable agent focus and retain stable mock inventory.
- `crates/gclient/tests/loop_liveness.rs::*` — scope-reason: include held-project operation cases.
- `crates/gclient/tests/loop_liveness/projects.rs::*` — scope-reason: characterize input delivery during held project operations and failures.
- `crates/gclient/tests/mock_daemon/mod.rs::*` — scope-reason: expose navigation fixture helpers.
- `crates/gclient/tests/mock_daemon/transport.rs::*` — scope-reason: provide stable terminal inventory for recovery fixtures.
- `crates/gclient/tests/mock_daemon/project_jobs.rs::*` — scope-reason: pump production job reducers for direct integration calls.
- `crates/gclient/tests/placed_agent.rs::*` — scope-reason: cover placed and unplaced agent project ownership.
- `crates/gclient/tests/project_flows.rs::*` — scope-reason: cover cancellation, supersession, reconnect retries and coalesced creation.

Split `projects.rs` into `crates/gclient/src/app/live_loop/project_operations.rs` before expanding operations.

Security-review follow-up: compensate the subscription side effect of dismissed, superseded or failed navigation; allow pending selection during restoration backoff. Preserve and report in-flight creation results across dismissal and reconnect, and retain request identity so reselection coalesces. Resample authoritative worktree occupants before closing captured tabs and deleting the worktree. Generation checks must share the outbound-channel selection lock, because an HTTP lookup can outlive its original connection.

**Research context:** `projects.rs::focus_agent`, `create_worktree` and `open_worktree` are called by modal, sidebar and menu input paths. `project_operations.rs::remove_worktree` owns kill-before-close-before-delete ordering. Reuse `jobs.rs::spawn_job`, `JobOutcome` and `LoopJobs::drain_pending_refetch`; keep all daemon waits in jobs and apply results in `jobs_apply.rs::apply_job_outcome`. `project_navigation.rs::dispatch`, `apply` and `resume` separate actual request identity from the newest intent, serialize attachments and reject stale generations. Terminal discovery uses `terminal_location.rs::prepare`; an existing workspace placement wins over the sidebar's assigned project, while unplaced agents retain their assigned project. Newly resolved rows must set `roster_landed` so the loop starts initial attachments. Reuse `MockDaemon`, the held native host, existing responsiveness bound of 1500ms, and production reducers in integration fixtures. The project operation module and navigation module are new extractions in this task; the view adapter moves from the loop into the existing projection owner. Keep source versions untouched: step 1 owns the integration-only mechanical bump.

**Granularity:** Retain the operator-selected existing #25768 as one input-responsiveness deliverable. Its navigation and termination paths share the live loop's job channel, generation boundary and surviving-pane acceptance check. The broad file inventory records callers and fixture consumers rather than independently closeable features.

**Observed checks:** Held deletion and attachment missed 1500ms before changes (run `f06ac314-4353-4730-a3c7-1404f3e4c0e4`); terminal discovery missed the same bound (run `f9b745bb-b608-4158-b4a5-5f88dea556ad`); creation did too (run `22ea5ff9-6414-49b8-9376-e5c4dd5b79dc`). Initial kill RED used an invalid fixture and is excluded; the corrected surviving-pane kill fixture passed. A→B→A navigation and creation, cross-project unplaced agents, and placed-owner cycling have separate tests-first evidence. Focused project/placed-agent/liveness tests passed 27/27 (run `34769531-fa1d-492b-b87a-d0c2625162ab`); later review fixes still require final crate validation. The first full run of the current phase passed 1001/1004 with three failures, which remain under correction rather than being counted as success.

**Verification:** Run `cargo nextest run -p gobby-client`, `cargo test --doc -p gobby-client`, `cargo clippy -p gobby-client --all-targets -- -D warnings`, and `cargo fmt -p gobby-client -- --check`, then audit every changed test with `uv run --no-sync gobby test-quality audit` against the existing baseline. Verify version-only landing receipt and installed binary identity after promotion from main. No full local pytest or per-task local E2E run.

**Final observed reruns:** The earlier full run `e0041df0-e45d-4a7a-bb89-489121b37103` passed 1007 tests, but later full runs exposed an intermittent late-detach reconnect and superseded that result. A deterministic timer-first regression failed before the fix (run `5c252c1b-b676-48fb-836e-aa740f6427ba`): an expired recovery-owned detach incorrectly requested reconnect. Keep that deadline under the active recovery future's ownership; unowned detach expiry retains its existing reconnect behavior. Focused regression and live-loop checks then passed 2/2 (run `a8b1b60b-6ea3-4a1b-88fc-d2d01400c605`). Final full crate run `c4075ba5-69db-406a-bf10-6a2e9217eccc` passed 1008 tests with one configured skip in 39.358s. Clippy passed with warnings denied. The changed-test quality audit scanned ten files and 190 tests with zero issues. Formatting, doctests, candidate review, landing and installed activation are recorded separately.

**Changes:**

- Add held-daemon characterization tests for project kill, worktree removal, and project attachment.
- Move the blocking operations into the existing job/outcome system.
- Apply results through generation-aware handling so stale completions cannot alter a newly focused project.
- Preserve kill-before-removal ordering and existing refusal/error reporting.
- Split project-operation code before expanding the large projects module.

**Acceptance:**

- 7.1 - During held project kill, input reaches the surviving terminal within 1500ms. test: `crates/gclient/tests/loop_liveness/projects.rs::held_project_kill_keeps_direct_input_live`.
- 7.2 - Held deletion, attachment, discovery and creation preserve input delivery. file: `crates/gclient/tests/loop_liveness/projects.rs`.
- 7.3 - Cancellation, repeated requests, focus changes, reconnects and A→B→A intent do not install stale results or duplicate creation. file: `crates/gclient/tests/project_flows.rs`.
- 7.4 - Placed terminal ownership wins; unplaced agents retain their assigned project. file: `crates/gclient/tests/placed_agent.rs`.
- 7.5 - Kill refusals preserve terminals and prevent deletion; dismissed errors identify the original target. file: `crates/gclient/tests/client_loop.rs`.

### 8. Status-report outcomes — #25863 (depends: 7)
`kind: deliverable`

Targets:
- `src/gobby/status_report/collect.py::*` — scope-reason: extend task and worktree row collection with actual holder evidence.
- `src/gobby/status_report/model.py::*` — scope-reason: carry semantic row and activation state.
- `src/gobby/status_report/render.py::*` — scope-reason: render rows, removals and operator-local times.
- `src/gobby/status_report/compose.py::*` — scope-reason: coordinate full and delta report artifacts.
- `src/gobby/status_report/delivery.py`
- `src/gobby/status_report/activation.py`
- `tests/status_report/test_delivery.py`
- `tests/status_report/test_activation.py`

**Research context:** Installed project policy supplies report cadence and full/delta windows. The existing status-report package has collection, model, render and compose owners; `compose_status_report` in the communications tools calls its composer, and the status-report-delivery pipeline orchestrates delivery. Extend those owners. New delivery and activation helpers will keep semantic row comparison separate from delivery state; activation records require successful source/binary identity proof. Test with synthetic task, delivery and activation fixtures.

**Granularity:** These are three ordered implementation outcomes under existing #25863. Implement and verify rows first, delivery second and activation third, retaining task-sized commits for the existing task.

Implement three ordered deliverables:

1. **Rows and idle holders:** Add task title, actual claim holder and session state, claimed-since, latest attestation, blocker, and ETA. Preserve reconcile-note freshness. Identify task/epic worktrees from stored relationships, sort tasks first, and include epic leaf counts and landing needs.
2. **Full and delta delivery:** Honor installed policy’s full/delta schedule. Persist a delivered full-report baseline and compare semantic row content against it. Show changes and removals; do not advance the baseline after failed delivery. Give overlapping runs distinct report artifacts.
3. **Activation reporting:** Persist successful restart/cutover records with source and installed-binary identities. Associate landings with proven activation rather than timestamps alone; render pending or unknown activation honestly.

Extend worktree notes with an optional ETA. Use one report cadence as the default idle-holder age, configurable through project policy. Render operator-facing times in `America/Chicago`.

**Acceptance:**

- 8.1 - Synthetic fixtures cover exclusions, row order, stale notes, and idle holders. file: `src/gobby/status_report/collect.py`.
- 8.2 - Cover delta removals, failed delivery and overlapping runs. file: `src/gobby/status_report/delivery.py`.
- 8.3 - Cover restart/cutover activation and stale binaries. file: `src/gobby/status_report/activation.py`.

### 9. CI E2E at implementation-root review — revised #25833 (depends: 8)
`kind: deliverable`

Targets:
- `.github/workflows/root-review-e2e.yml`
- `src/gobby/tasks/root_review_e2e.py`
- `tests/tasks/test_root_review_e2e.py`

**Research context:** CI verification already supplies authoritative run validation. New root-review evidence handling will bind that result to an immutable candidate and subtree revision, exempting leaves and descendant subepics. Test synthetic CI results and root selection with no credentialed provider activity.

**Changes:**

- Remove per-task local E2E landing/closure prerequisites and host-run instructions.
- Require CI E2E during final review of the highest implementation root. Administrative umbrella epics do not define this boundary; descendant subepics are exempt.
- Add a dedicated CI workflow triggered by an authorized immutable root-review tag identifying the root and full candidate SHA.
- Check out that SHA, build its native binaries, prepare isolated test state, warm imports, and run the E2E harness.
- Publish the tested SHA, binary identities, node counts, skips, and report digest.
- Reuse the existing CI verification helper to verify the selected workflow’s authoritative result.
- Record CI evidence for the root candidate and subtree revision. Root approval waits for it; leaf and subepic landing do not.
- Reuse evidence across the strictly verified version-only transformation from step 1. Other candidate changes invalidate it.
- Preserve separate admission for credentialed real-provider proofs; do not represent skipped proofs as passing.

**Acceptance:**

- 9.1 - Test root-versus-subepic selection, wrong SHA/workflow, failed or cancelled runs, superseding attempts, stale subtree evidence, and absence of per-task E2E waits. file: `src/gobby/tasks/root_review_e2e.py`.

## V1 Validation and execution
`kind: verification`

- Claim before edits, coordinate #25912’s existing owner, and stage only owned paths.
- Run focused tests with the isolated PostgreSQL hub, `GOBBY_TEST_PROTECT=1`, and `uv run --no-sync`.
- Run Ruff, mypy, changed-test audits, and the affected Rust crate’s tests, clippy, and formatting checks. Do not run the full local pytest suite.
- Validate bundled YAML generically and bump changed definitions, rules, skills, pipelines, and references.
- Verify installed effective definitions and rules after activation; template edits alone do not prove live behavior.
- Run root E2E in CI after assembling the final candidate.
- After final landing, rebuild/promote changed binaries through the coherent-set installer and activate from the main checkout.

Already observed: **184 cwd-proof tests passed**, **4 worktree-related installer tests passed**, and the original #25872 staging transcript recovered its correct workdir. The implementation checks above remain unrun.

Materialize this draft and run project-aware plan validation before expansion or implementation.
