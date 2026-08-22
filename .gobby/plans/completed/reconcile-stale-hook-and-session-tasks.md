# Reconcile Stale Hook and Session Tasks

**Plan ID:** reconcile-stale-hook-and-session-tasks

## Overview
`kind: framing`

Live task state confirms #19750 is still open and partially valid. #19894/#19895 also retain stale completed scope. This plan preserves existing tasks, corrects their residual definitions, and implements the remaining work.

## Constraints
`kind: framing`

- Use existing tasks; never expand this artifact into replacement tasks.
- Preserve closed #19896–#19898 and commits `7a356a073` / `6c3422698`.
- Validate this artifact through the Plan-Coverage Contract with project-aware symbol checks.
- Keep every touched production source below 1,000 lines.
- Keep detector failures fail-open and stop-gate state lookups fail-closed.
- Add no migration, compatibility layer, skill-pin API, `RuleEffect` field, or configuration knob.

## Decision Record
`kind: framing`

- **#19750:** retain as a docs-only leaf. Its stop-gate half moves to #19885.
- **#19751:** every successfully loaded skill becomes sticky for the session lifetime. Preserve order, deduplicate names, and apply no allowlist, cap, or decay. This is the previously confirmed breadth choice.
- **Human waits:** plan coordinator is the single writer. It calls top-level `set_variable(waiting_on_user_input=true)` immediately before every `request_user_input` vote/review prompt and repeats this on each multi-turn vote round.
- **Agent waits:** only a durable completion subscription joined to a `pending` or `running` agent run grants exemption.
- **Plan validation:** use typed phases, Targets, and Acceptance items. Validation supplies review evidence; task expansion remains prohibited.
- **Malformed manifests:** provider reconciliation leaves them untouched with a warning. Codex installation alone owns corrupt-file quarantine.
- **Run commands:** extract subprocess execution into a focused internal module to keep `effects.py` comfortably below the line ceiling.

## P1: Reconcile Records and Guidance
`kind: framing`

**Goal:** Make the artifact and live task records accurately describe residual work.

### 1.1 Materialize the plan and land no-poll guidance [category: docs]
`kind: deliverable`

Targets:
- `.gobby/plans/reconcile-stale-hook-and-session-tasks.md`
- `src/gobby/install/shared/skills/plan/SKILL.md`
- `tests/skills/test_plan_skill_delegated_mode.py::*` — scope-reason: verify plan review waiting guidance and bundled synchronization
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: refresh generated shared-content inventory

Before file edits, update live task records through Gobby MCP:

- Keep #19750 open with docs-only scope: explicitly prohibit foreground polls, direct run-API polling, and Bash sleep heartbeats; require subscribe-once `wait_for_agent` plus daemon wake.
- Record the sticky-all-loaded decision and exact variable contract in #19751.
- Replace #19885’s alternatives with the selected dual wait-state design.
- Rename #19894 to “Harden corrupt Codex hook quarantine” and remove completed A1/A3 work covered by #19897.
- Rename #19895 to “Finish Impeccable manifest ownership and bounded detector execution” and remove the effect/rule creation already covered by #19898.
- Update #19893 to identify #19894/#19895 as its only remaining close conditions.

Claim #19750, materialize this contract-shaped artifact, add the explicit no-poll wording, refresh bundled content, validate, commit, and close #19750 with its SHA.

**Acceptance:**

- 1.1.1 - Contract-shaped plan exists and passes project-aware validation. file: `.gobby/plans/reconcile-stale-hook-and-session-tasks.md`.
- 1.1.2 - Plan skill explicitly bans custom polling and requires `wait_for_agent` plus daemon wake. behavior: "Waiting on Spawned Runs" in `src/gobby/install/shared/skills/plan/SKILL.md`.
- 1.1.3 - Live task titles, descriptions, criteria, dependency, and umbrella boundaries match the residual definitions recorded here. behavior: "Gobby task records #19750, #19751, #19885, #19893, #19894, and #19895".
- 1.1.4 - Bundled skill synchronization preserves the new guidance. test: `tests/skills/test_plan_skill_delegated_mode.py`.

## P2: Independent Runtime Fixes
`kind: framing`

**Goal:** Complete the three independent leaves. Execute 2.1 first in a sequential run because it unblocks P3; 2.2 and 2.3 have no dependency on the Impeccable epic.

### 2.1 Harden corrupt Codex hook quarantine [category: code]
`kind: deliverable`

Targets:
- `src/gobby/cli/installers/codex.py::_quarantine_corrupt_hooks_file`
- `tests/cli/installers/test_codex_installer.py::*` — scope-reason: add collision, byte-preservation, and I/O-failure regressions

Use same-directory `mkstemp` with a timestamp prefix, random component, and `.corrupt` suffix. Close the reservation descriptor, move the malformed source with `os.replace`, and remove the reservation only when replacement failed and it remains empty. Fresh hooks writing stays atomic; failures propagate while quarantined bytes remain recoverable. Completed foreign-hook preservation and merge consolidation stay untouched.

**Acceptance:**

- 2.1.1 - Two quarantines during the same second produce distinct siblings without clobbering. test: `tests/cli/installers/test_codex_installer.py`.
- 2.1.2 - Successful quarantine preserves the original bytes exactly. test: `tests/cli/installers/test_codex_installer.py`.
- 2.1.3 - Rename failure preserves the original and removes only the unused empty reservation. test: `tests/cli/installers/test_codex_installer.py`.
- 2.1.4 - Replacement-write failure propagates while leaving the quarantine recoverable. test: `tests/cli/installers/test_codex_installer.py`.

### 2.2 Persist loaded skills through compaction [category: code]
`kind: deliverable`

Targets:
- `src/gobby/sessions/compact_markers.py`
- `src/gobby/sessions/compact_continuation.py::persist_compact_resume_required_skills`
- `src/gobby/mcp_proxy/stdio_proxy.py::DaemonProxy.set_variable`
- `src/gobby/mcp_proxy/stdio_tools.py::set_variable`
- `src/gobby/mcp_proxy/tools/workflows/_variables.py::set_variable`
- `src/gobby/install/shared/skills/goal/SKILL.md`
- `tests/sessions/test_compact_continuation.py::*` — scope-reason: cover two complete compaction/reload cycles
- `tests/mcp_proxy/test_mcp_proxy_stdio.py::*` — scope-reason: cover structured top-level variable transport
- `tests/mcp_proxy/tools/workflows/test_variables.py::*` — scope-reason: cover skill-list validation
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: refresh generated shared-content inventory

Move the pre-compaction `loaded_skills` snapshot into the required resume tier while continuing to clear the current-context ledger. Successful reloads repopulate the ledger for subsequent compactions; `additional_skills` remains advisory.

Extend both stdio transport layers to accept lists and objects. The daemon-facing server already supports them. Define the known skill-list set centrally:

`required_skills`, `additional_skills`, `loaded_skills`, `claimed_task_required_skills`, `claimed_task_additional_skills`, `workflow_requested_skills`, `compact_resume_required_skills`, and `compact_resume_advisory_skills`.

Writes to those names must be arrays containing only non-empty strings. Invalid writes return `{"success": false, "error": "Variable '<name>' requires a JSON array of non-empty skill names."}` and leave stored state unchanged.

**Acceptance:**

- 2.2.1 - A successfully loaded skill appears in required reloads across two consecutive compactions. test: `tests/sessions/test_compact_continuation.py`.
- 2.2.2 - `loaded_skills` clears at compaction and is repopulated only by successful reloads; `additional_skills` remains advisory. test: `tests/sessions/test_compact_continuation.py`.
- 2.2.3 - Top-level stdio `set_variable` transports arrays and objects intact. test: `tests/mcp_proxy/test_mcp_proxy_stdio.py`.
- 2.2.4 - Every known skill-list variable rejects strings, nulls, objects, empty names, and non-string items atomically. test: `tests/mcp_proxy/tools/workflows/test_variables.py`.
- 2.2.5 - Goal guidance states that loading the skill once activates session-lifetime sticky reload behavior. behavior: "compaction guidance" in `src/gobby/install/shared/skills/goal/SKILL.md`.

### 2.3 Let legitimate waits pass stop gates [category: code]
`kind: deliverable`

Targets:
- `src/gobby/storage/pipeline_subscribers.py::PipelineCompletionSubscriberMixin`
- `src/gobby/workflows/engine/templating.py::TemplatingMixin._build_allowed_funcs`
- `src/gobby/workflows/engine/core.py::RuleEngine.evaluate`
- `src/gobby/install/shared/workflows/variables/gobby-default-variables.yaml::*` — scope-reason: add the one-turn human-wait default
- `src/gobby/install/shared/workflows/rules/stop-gates/require-task-close.yaml::*` — scope-reason: add legitimate-wait conditions
- `src/gobby/install/shared/workflows/rules/stop-gates/require-epic-tree-close.yaml::*` — scope-reason: add legitimate-wait conditions
- `src/gobby/install/shared/skills/plan/SKILL.md`
- `tests/agents/test_completion_subscribers.py::*` — scope-reason: cover active, terminal, missing, and orphan subscriptions
- `tests/workflows/test_stop_gates_rules.py::*` — scope-reason: cover both stop gates and wait states
- `tests/workflows/test_rule_engine_task_helper_wiring.py::*` — scope-reason: cover condition-helper wiring and one-turn reset
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: refresh generated shared-content inventory

Add a parameterized `EXISTS` query joining `completion_subscribers.completion_id` to `agent_runs.id`, filtered by canonical session ID and active statuses `pending`/`running`. Missing runs, terminal runs, and cleanup-lag orphans return false.

Compute the result once per `turn_end`, expose it as zero-argument `has_active_agent_wait()`, and treat database errors as false with a warning. Both stop gates yield when this helper or `waiting_on_user_input` is true. These waits also skip the `stop_attempts` increment.

At `turn_start`, clear `waiting_on_user_input` before rules execute. Plan coordinator re-sets it immediately before every later vote prompt in a multi-turn review.

**Acceptance:**

- 2.3.1 - Plain claimed leaf and epic stops still block. test: `tests/workflows/test_stop_gates_rules.py`.
- 2.3.2 - Active subscribed runs let both gates yield; terminal, missing, orphaned, and failed lookups remain blocking. test: `tests/agents/test_completion_subscribers.py`.
- 2.3.3 - A human-wait marker yields for one turn, clears at the next `turn_start`, and can be deliberately re-set for another vote. test: `tests/workflows/test_rule_engine_task_helper_wiring.py`.
- 2.3.4 - Legitimate waits consume no `stop_attempts`; ordinary stops retain existing accounting. test: `tests/workflows/test_stop_gates_rules.py`.
- 2.3.5 - Subscription terminalization automatically re-arms gates without an agent-managed flag. test: `tests/agents/test_completion_subscribers.py`.

## P3: Finish Impeccable Ownership
`kind: framing`

**Goal:** Complete #19895 after #19894 makes Codex quarantine reliable.

### 3.1 Reconcile manifests and harden detector execution [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/cli/install_setup_impeccable.py::*` — scope-reason: add multi-provider ownership reconciliation and consent merging
- `src/gobby/cli/install_setup.py::run_daemon_setup`
- `src/gobby/install/shared/skills/impeccable/scripts/hook-admin.mjs::*` — scope-reason: remove all obsolete manifest-repair machinery
- `src/gobby/workflows/engine/effects.py::EffectsMixin._apply_run_command`
- `src/gobby/workflows/engine/effects.py::EffectsMixin._execute_run_command`
- `src/gobby/workflows/engine/effects.py::EffectsMixin._run_command_then_deliver`
- `src/gobby/workflows/engine/run_command.py::*` — scope-reason: own subprocess execution, payload shaping, output limits, and result parsing
- `src/gobby/hooks/hook_manager.py::HookManager._create_rule_evaluator`
- `src/gobby/hooks/rule_evaluator.py::WorkflowRuleEvaluator.__init__`
- `src/gobby/hooks/rule_evaluator.py::WorkflowRuleEvaluator.evaluate`
- `src/gobby/workflows/hooks.py::WorkflowHookHandler._evaluate_rules`
- `src/gobby/workflows/hooks.py::WorkflowHookHandler.evaluate_async`
- `src/gobby/workflows/hooks.py::WorkflowHookHandler.evaluate`
- `src/gobby/workflows/hooks.py::WorkflowHookHandler.handle`
- `src/gobby/workflows/engine/core.py::RuleEngine.evaluate`
- `src/gobby/storage/workflow_audit.py::WorkflowAuditEntry`
- `src/gobby/storage/workflow_audit.py::WorkflowAuditManager.log`
- `tests/cli/test_install_setup_impeccable.py::*` — scope-reason: cover all manifest shapes and consent behavior
- `tests/cli/test_install_setup.py::*` — scope-reason: cover daemon-setup reconciliation wiring
- `tests/workflows/test_run_command_effect.py::*` — scope-reason: cover subprocess limits, deadlines, delivery, and audits
- `tests/hooks/test_hook_manager.py::*` — scope-reason: cover shared deadline propagation
- `tests/workflows/test_impeccable_rules.py::*` — scope-reason: cover provider payloads and six-source detector synchronization
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: refresh generated shared-content inventory

After managed Impeccable install/verification, reconcile:

- `.claude/settings.local.json`
- `.claude/settings.json`
- `.codex/hooks.json`
- `.cursor/hooks.json`
- `.github/hooks/impeccable.json`

An entry is Impeccable-owned only when its `command`, `args`, `bash`, `powershell`, or nested `hooks` contains a known `skills/impeccable/scripts/` hook marker. Remove matching entries and empty event containers while preserving foreign data and top-level metadata. Delete shared manifests only when the resulting object is empty. Delete the dedicated Copilot manifest when no foreign hook entries remain, treating its `version`/empty-hooks scaffolding as Impeccable-owned.

Leave malformed or non-object reconciliation inputs untouched with warnings. Codex install handles malformed `.codex/hooks.json` through #19894 before reconciliation. Merge `hook.consent = "declined"` into `.impeccable/config.local.json` while preserving detector and foreign keys.

Move subprocess mechanics into `run_command.py`. Build stdin from a deep copy of raw event data, preserve provider fields, normalize tool fields, add `cwd`, and synthesize `PostToolUse`/`Stop` only when `hook_event_name` is absent. Inline execution consumes the shared blocking deadline; background execution uses its own effect/default timeout.

Read stdout and stderr concurrently with 256 KiB and 64 KiB caps. On timeout or overflow, cancel readers, kill, and reap. Empty stdout is a successful no-context result; malformed non-empty output is `invalid_output`. Audit `success`, `spawn_error`, `nonzero_exit`, `timeout`, `output_limit`, `invalid_output`, and `deadline_exhausted` as `event_type="effect"` with rule ID, duration, exit code, byte counts, timeout, overflow stream, and background flag. Never store stdin, stdout, or stderr contents.

**Acceptance:**

- 3.1.1 - Claude, Codex, Cursor, and Copilot manifests are pruned idempotently while foreign entries and metadata survive. test: `tests/cli/test_install_setup_impeccable.py`.
- 3.1.2 - Malformed reconciliation inputs remain byte-identical; Codex install quarantine remains the sole corruption owner. test: `tests/cli/test_install_setup_impeccable.py`.
- 3.1.3 - Consent merging preserves detector configuration, and vendored hook administration contains no manifest repair code. test: `tests/cli/test_install_setup_impeccable.py`.
- 3.1.4 - Inline commands honor remaining aggregate deadline; background commands use independent timeouts and deliver successful context next turn. test: `tests/workflows/test_run_command_effect.py`.
- 3.1.5 - Stream overflow and timeout kill/reap the child and fail open; audits contain metadata only. test: `tests/workflows/test_run_command_effect.py`.
- 3.1.6 - Edit events preserve single/multiple file fields and deep-pass events receive a synthesized Stop envelope. test: `tests/workflows/test_impeccable_rules.py`.
- 3.1.7 - Enabled detector templates sync and evaluate in isolated databases for `claude`, `codex`, `qwen`, `droid`, `grok`, and `agy`. test: `tests/workflows/test_impeccable_rules.py`.

## Verification
`kind: framing`

Run focused validation only:

```bash
uv run gobby plans validate .gobby/plans/reconcile-stale-hook-and-session-tasks.md --project gobby

GOBBY_TEST_PROTECT=1 uv run pytest tests/skills/test_plan_skill_delegated_mode.py -v
GOBBY_TEST_PROTECT=1 uv run pytest tests/cli/installers/test_codex_installer.py -v
GOBBY_TEST_PROTECT=1 uv run pytest tests/sessions/test_compact_continuation.py tests/mcp_proxy/test_mcp_proxy_stdio.py tests/mcp_proxy/tools/workflows/test_variables.py -v
GOBBY_TEST_PROTECT=1 uv run pytest tests/agents/test_completion_subscribers.py tests/workflows/test_stop_gates_rules.py tests/workflows/test_rule_engine_task_helper_wiring.py -v
GOBBY_TEST_PROTECT=1 uv run pytest tests/cli/test_install_setup_impeccable.py tests/cli/test_install_setup.py tests/workflows/test_run_command_effect.py tests/hooks/test_hook_manager.py tests/workflows/test_impeccable_rules.py -v
GOBBY_TEST_PROTECT=1 uv run pytest tests/test_build_backend.py::test_committed_bundled_content_manifest_matches_shared_tree -v
```

Run Ruff and mypy against every changed Python target and test-type auditing against every changed test. Recheck line counts before edits and commits. The full pytest suite remains excluded.

Commit and close each leaf independently with its SHA. Closing #19894 unblocks #19895. Close #19893 only after #19894 and #19895 pass bounded criteria reviews; #19750, #19751, and #19885 are outside that umbrella condition.

## Task Mapping
`kind: framing`

| Plan item | Existing task | Close condition |
|---|---:|---|
| 1.1 | #19750 | No-poll guidance, valid artifact, linked docs commit |
| 2.1 | #19894 | Unique quarantine and I/O regressions |
| 2.2 | #19751 | Two-cycle sticky reload and structured-variable validation |
| 2.3 | #19885 | Both wait states, re-arm, and stop-attempt accounting |
| 3.1 | #19895 | Ownership cleanup, bounded execution, audits, six-source sync |
| Umbrella | #19893 | #19894 and #19895 closed |

## V1 Plan Changelog
`kind: verification`

- Corrected live task-state claims and retained #19750 as a docs residual.
- Converted the brief into a contract-valid, project-verifiable artifact.
- Recorded the confirmed sticky-all-loaded decision and exact variable validation contract.
- Assigned human-wait ownership and stop-attempt behavior.
- Defined residual task rewrites, provider ownership rules, malformed-file boundaries, subprocess extraction, and umbrella closure conditions.
