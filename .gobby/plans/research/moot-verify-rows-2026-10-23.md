## Research Findings

### Research Questions

- Which of the eight verify rows remain actionable after DevTeam v4?

### Domain Context

- **CLOSE 4 / LIVE 4.** Research task #24836; no task or production edits. Only this report is authored. Existing unrelated dirty files remain untouched.
- Planned end state: `task-23850:.gobby/plans/runbook-devteam-redesign.md` (branch observed at `27fedf1cd2`) plus `runbook-devteam-addendum.md`. Delivered sections 1.1–1.4 are distinguished from future sections. Branch templates do not prove installed registry activation.
- Sources: the eight task descriptions/criteria, gcode repository evidence, branch plan and addendum, and the Orchestrator's live measurements supplied on 2026-10-23. No external sources were needed.

### Evidence & Sources

| Task | Verdict | Reason and evidence | LIVE rescope |
| --- | --- | --- | --- |
| #24509 | **CLOSE** | Orchestrator #17281 timed `close_task(task_id="#24717", preview=true, reason="obsolete")`: **99.9 ms**, `can_close=true`; children, no-work/commit and uncommitted-edit gates passed; ancestry skipped because no work was linked. First preview with `reason="superseded"` took **78.6 ms**, refused the no-work reason and returned the same four gates. Neither approached 300 seconds. Matches v4 §1.1 and delivered P1 removal summary. | — |
| #24646 | **CLOSE** | V4 §2.3 replaces caller-authored independent-review receipts with role-owned review records; contradictory replays fail and later rounds/epochs carry new evidence. §2.4 deletes the MCP `record_close_receipt` tool and activation/landing-approval/authority-transfer contracts. Branch `close_receipts.py:61–124` retains daemon-authored landing receipts replayed by author/commit; its module states review approval comes from step records. The mistaken caller-authored review fact that motivated this task has no surviving authority. | — |
| #24403 | **LIVE** | The activation prerequisite retires in §2.4, but wrong metadata survives: branch `land_commit.py:238` still assigns `classify_paths(paths)` to `facts["activation_class"]`. Main `landing_policy.py:21,39–41` classifies all `crates/` paths as cutover, including crate tests. Release §4.2 chooses activation through workspace source identity instead. `bin_release_identity.py:96–148` excludes tests/benches/examples/docs, `tests.rs` and `*_tests.rs`, so test-only changes alone do not force that cutover decision. | Correct surviving landing activation metadata for crate test-only paths; pin release activation to source identity rather than the old path classifier. |
| #24135 | **LIVE** | The affected maker step machine remains: branch `developer.yaml:67,81,192–207,275–290,496` contains `step_workflow`, `_developer_handoff_tasks` and handoff guards. §3.2 introduces atomic same-team claim transfer/recovery; §3.5 rewrites definitions without expressly deleting maker steps. Addendum §7.1 explicitly preserves maker route_skills and submit-bounce behavior keyed on implementation grants. Automatic close-review waits retire, but stale claim binding can still block maker edits/recovery. | Verify selection/binding through same-team handoff, rejection and resume in retained maker steps; remove obsolete close-review-wait and recycled-seat requirements. |
| #24398 | **CLOSE** | Branch `code-reviewer.yaml` has `step_workflow` at line 76 and no park references in the targeted search. V4 §2.4 explicitly retires park/resume grammar, reviewer landing recovery and reviewer landing conditions; reviewers finish at their verdict. §3.5 supplies the DevTeam replacement. Main's parked-candidate text is historical pending activation. | — |
| #24497 | **CLOSE** | `unregistered_execution_reason` is removed. `gcode outline src/gobby/tasks/transcript_outcomes.py` shows the surviving wrapper/outcome helpers; `_executable_wrapper_reason:131–151` contains neither the old npx -c nor make -f credit check. General consumers retain outcome extraction (`condition_helpers.py:22`, `transcript_output_retention.py:13`). Delivered §1.4 explicitly preserves found-work/general consumers while deleting close-only evidence evaluation. The removed Gate-10 recognizer is distinct from retained failure detection. | — |
| #24723 | **LIVE** | Removing automatic close review (§1.3) does not establish that shared sandbox denials disappear for DevTeam reviewers. Provider launch still uses `_prepare_provider_sandbox` (`spawn_executor_providers.py:109,314,424`); §3.2 includes a fresh Claude reviewer after the fixer round. Run cache/environment construction is shared (`sandbox_run_environment.py:50–96`, `sandbox_policy.py:866–916`). §5.1 retains the sandbox trust split and adds Cargo/sccache handling without claiming fixes for bootstrap/shell-rc reads or network-denial attribution. No fresh v4 violation log disproves these shapes. | Validate shared DevTeam reviewer launches for cache, bootstrap, shell-rc and unattributed-network denials; fix surviving callers/diagnostics while preserving the credential boundary. |
| #24727 | **LIVE** | Shared environment construction does not set `UV_PROJECT_ENVIRONMENT` (`sandbox_run_environment.py:65`), and the plan supplies no explicit fix for resolving the main checkout environment. `SandboxRunPaths.writable` includes cache, but that alone does not prove the cached-settings denial is resolved under provider enforcement. Shared sandbox launch infrastructure survives (§3.2, §5.1); no fresh v4 Claude/uv run disproves either denial. | Pin reviewer uv to its own worktree environment and verify cached-settings writes under the actual provider sandbox, without unrelated write grants. |

Reproducible gcode evidence:

```text
gcode outline src/gobby/tasks/transcript_outcomes.py
gcode symbol-at src/gobby/tasks/transcript_outcomes.py:137
gcode symbol-at src/gobby/tasks/landing_policy.py:39
gcode symbol-at src/gobby/install/bin_release_identity.py:96
gcode symbol-at src/gobby/agents/sandbox_run_environment.py:65
gcode symbol-at src/gobby/agents/sandbox_policy.py:866
gcode --project /Users/josh/.gobby/worktrees/gobby/task-23850 grep -n 'park|step_workflow|claim:|_developer_handoff_tasks' src/gobby/install/shared/workflows/agents/code-reviewer.yaml src/gobby/install/shared/workflows/agents/developer.yaml -m 30
gcode --project /Users/josh/.gobby/worktrees/gobby/task-23850 grep -n 'activation_class|classify_paths|receipt' src/gobby/tasks/land_commit.py src/gobby/tasks/close_receipts.py -m 40
```

### Risks

- Researcher serve enforcement denied this session's close_task call, including preview=true. The Orchestrator ran the two previews and reported their timings; they are attributed evidence. #24717 remains open and unchanged. The Orchestrator also agreed to perform final task closure.
- Live close rejects superseded as a no-work disposition and accepts obsolete; the branch plan calls for canonical superseded. Reconcile this observed live/planned mismatch during #24797 activation.
- #24135 and the sandbox rows remain LIVE conservatively. Retained mechanisms and absent retirement evidence do not prove every historical symptom reproduces after full v4 activation. Narrowed validation must precede closure.
- #24403's metadata defect survives independently of release §4.2's source-identity decision. Its historical per-task activation blocker retires.
- Orchestrator owns task moves and closes. No restart, provider launch, process termination or sandbox widening was performed.
