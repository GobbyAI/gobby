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
- `tests/mcp_proxy/test_definition_activation.py`

**Research context:** `apply_agent_definition` already applies full definitions. The older cold-start layer adds alias and counterpart-pane behavior.

**Changes:**

- Route `/gobby agents <definition>` directly through `apply_agent_definition` for the current session.
- Keep `/gobby persona <persona>` as the persona-overlay operation.
- Require an enabled definition supporting the persona surface and no pipeline requirement.
- Remove the `admin` alias, cold-start tool, `cold_start_commands`, and counterpart-opening configuration and code.
- Return eligible choices for invalid names without changing session state.
- Ensure activation installs the definition’s rules, workflows, capabilities, prompts, and workspace policy.
- Preserve manual unsandboxed pane launch; add no spawn bypass or Telegram approval infrastructure.

**Acceptance:**

- 3.1 - Activation changes the session’s effective identity and enforcement, opens no pane, and is idempotent when repeated. file: `tests/mcp_proxy/test_definition_activation.py`.

### 4. Clear semantics and default scratch space — new lifecycle work (depends: 3)
`kind: deliverable`

Targets:
- `src/gobby/hooks/event_handlers/_session_end.py::*` — scope-reason: route confirmed clear through semantic lifecycle reset.
- `src/gobby/hooks/event_handlers/clear_identity.py`
- `tests/hooks/test_clear_identity.py`

**Research context:** Session-end handling owns termination; new `clear_identity.py` will centralize reset and spawned-seat cleanup integration. Preserve provider authentication and safeguards while clearing semantic definition state. Verify with isolated lifecycle fixtures.

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
- `src/gobby/agents/workspace_bindings.py`
- `src/gobby/agents/workspace_backups.py`
- `tests/agents/test_persistent_workspace_rotation.py`

Split `agent_workspaces.py` into `src/gobby/agents/workspace_bindings.py` and `src/gobby/agents/workspace_backups.py` before extending stable ownership.

**Research context:** `persistent_workspace` already exists, but independent sessions still receive session-numbered folders.

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
- `src/gobby/hooks/credential_file_policy.py`
- `tests/hooks/test_credential_file_policy.py`

**Research context:** Direct tool and shell payloads enter hook normalization; new `credential_file_policy.py` will share normalized path checks, with applicability supplied by definition policy. Use synthetic files and definitions to verify refusals without reading live credentials.

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
- `crates/gclient/src/app/live_loop/project_operations.rs`
- `crates/gclient/src/app/live_loop/jobs.rs::*` — scope-reason: add project operation job and outcome variants.
- `crates/gclient/src/app/live_loop/jobs_apply.rs::*` — scope-reason: apply generation-checked project outcomes.
- `crates/gclient/src/app/live_loop/project_operations_tests.rs`

Split `projects.rs` into `crates/gclient/src/app/live_loop/project_operations.rs` before expanding operations.

**Research context:** Project attachment and worktree removal still await daemon operations from the input path.

**Changes:**

- Add held-daemon characterization tests for project kill, worktree removal, and project attachment.
- Move the blocking operations into the existing job/outcome system.
- Apply results through generation-aware handling so stale completions cannot alter a newly focused project.
- Preserve kill-before-removal ordering and existing refusal/error reporting.
- Split project-operation code before expanding the large projects module.

**Acceptance:**

- 7.1 - While each daemon operation is held, input reaches the terminal within the established bound; cover failure, cancellation, repeated requests, and focus changes. file: `crates/gclient/src/app/live_loop/project_operations_tests.rs`.

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
