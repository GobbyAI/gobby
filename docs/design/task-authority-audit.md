# Task-Transition Authority Audit

Date: 2026-08-23. Method: five parallel read-only audits over every mutation tool
registered on `gobby-tasks` and `gobby-tasks-ops`, tracing each handler into its
storage layer, asking one question: before mutating, does the tool verify the
calling session's relationship to the target task? Trigger: the #20816/#20813
cross-session attribution deadlock and the discovery that `claim_task(force=True)`
is an ungated live-session takeover.

This document is the design input for the hub-boundary authority contract
(#20822, Rust gobby-tasks) and the spec context for the interim MCP-layer claim
guard (#20821). The takeover-grounds contract is #20818 (child of #20822).

## Headline

Outside `claim_task` and `submit_close_review`, **no task mutation on either MCP
server compares the calling session to the task's claim**. Session identity is
resolved for attribution and notification routing, never for authorization.
Storage layers are plain SQL writes keyed on `task_id`.

## The positive exemplars (the patterns to generalize)

- `claim_task` — `_lifecycle_claim.py:129-153`: reads `get_claimed_session_id`,
  compares to the caller, models a delegation exception
  (`_has_delegated_agent_run`, parent→child agent runs), refuses with
  `TASK_CLAIM_CONFLICT`. Weakness: `force=True` overrides unconditionally, with
  no liveness check on the owner and no consent record (see #20818).
- `submit_close_review` — `_lifecycle_close_orchestration.py:282-308`:
  four-factor agent-run identity binding. Caller's ambient run must equal the
  persisted `review.agent_run_id`; run must be the `task-close-validator` agent,
  taskless, parented by the requesting session, with the live calling session as
  its child. Finalization is single-shot via `claim_finalizing`. This is the
  call-time re-verification pattern reviewer flows should adopt.
- Stage-move storage — `_stage_state_transitions.py:645` has an owner-compare
  with explicit `force`, but the MCP stage tools do not route through it.
- `escalate_task_if_owned` — `_transitions.py:319` exists; its only caller is
  the dead-session reaper. The MCP `escalate_task` uses the unguarded variant.

## Systemic patterns

1. **Attribution vs authorization.** `by_session_id` / `triggering_session_id` /
   `session_id=` are provenance fields everywhere; no handler treats them as a
   precondition.
2. **Revoke-instead-of-deny.** `get_claimed_session_id(task)` is fetched in at
   least six handlers (`_stage_review.py:237,385,571,704`,
   `_lifecycle_status.py:61,148`) solely to *clear the owner's claim and session
   variables* after a stranger's mutation. A foreign call evicts the legitimate
   owner instead of being refused.
3. **Fail-open session resolution.** `_stage_ops.py:43-50` swallows resolution
   errors and proceeds with actor `"system"`; `_stage_review.py:63-73` refuses
   with `SESSION_REQUIRED`. Two policies in one surface.
4. **Role separation is spawn-time only.** Agent YAML `allowed_mcp_tools` decides
   who *holds* a tool; nothing at call time checks the holder's relationship to
   the *target task*. `submit_close_review` is the sole exception.
5. **UUID refs bypass project scoping.** `_resolution.py:12-51` scopes `#N`,
   numeric, and dotted-path refs to the project; a raw UUID is a direct global
   lookup.
6. **Parallel unguarded lanes.** HTTP `routes/tasks.py` (POST/PATCH/DELETE) and
   `routes/tasks_lifecycle_routes.py` (escalate `:179`, de-escalate `:254`)
   reach the same storage methods with operator-cookie authority and no session
   identity. The contract must live at the hub so every lane passes through it.

## Matrix

Relationship-check status per mutation tool. Bucket: **G** = interim claim guard
(#20821), **R** = Rust-era hub contract (#20822), **✓** = already adequate.

| Tool | Check today | Sharpest exposure | Bucket |
|---|---|---|---|
| claim_task | owner-compare + delegation; `force=True` ungated | live-session takeover, no consent/liveness | R (#20818) |
| release_task_paths | owner-only + clean-file proof | any-dirty refusal deadlocks with foreign dirt (#20818 analysis) | R |
| close_task | none (caller `or`-chain wins) | non-owner closes using the *owner's* evidence; caller recorded in audit row | R |
| escalate_task | none (presence only) | `SET claimed_by_session_id = NULL` — strips a live claim | G |
| de_escalate_task | none | any agent clears human escalation, resets failure counters | G |
| reopen_task | none — session never resolved | anonymous reopen wipes closure + `validation_fail_count` | G |
| delete_task | none — no session import at all | `cascade=True` default hard-deletes trees | G |
| update_task | none | rewrite foreign `validation_criteria`, `affected_files`, parent, priority | G |
| add_label / remove_label | `live-session` guard only (caller *type*, not relationship) | strip `tdd:required` / `needs-decision` / `clean-window` from anyone's task | G |
| append_description_section | none | heading-squatting suppresses later legitimate appends | G |
| create_task (`blocks=[...]`) | creator resolved; target owner never | throwaway task blocks a foreign in-flight task | R (edge ownership) |
| add_dependency / remove_dependency | none | flip a foreign task's readiness either direction | R |
| link_commit / unlink_commit | none | feed or strip close gates 7/8 cross-session | G |
| auto_link_commits | none; `task_id` optional | bulk link across every `#N` task in the project | R |
| set_affected_files | none | wholesale replace declared scope (gate 8), both directions | G |
| update_observed_files | none | second-order via commits | R |
| wire_affected_files_from_run | none; keyed on `run_id` | rewrite declared scope across a whole expansion tree | R |
| set_artifact / set_artifacts_atomic | none | repoint `worktree_id`/`clone_id` bindings mid-run | G |
| clear_isolation_pair | none | detach the isolation env under an active session | G |
| link_task_to_session | none; explicit `session_id` overrides caller | forge provenance rows (`claimed`/`closed`/`review_approved`) for other sessions | G |
| submit_for_review | none | stranger submits, scope evaluated against owner's files, owner claim cleared | R |
| approve_review | none (planning: dispatch-run binding) | **self-approval** on non-planning stages; planning binding is task-scoped, not reviewer-scoped | R |
| reject_review | none | burn a foreign task's review-round cap toward escalation | R |
| submit_close_review | four-factor agent-run binding | — | ✓ |
| start/complete/fail_stage | none; fail-open to `"system"` | complete a foreign merge stage with attacker SHA; `fail_stage` bumps attempt caps, `cited_subtasks` resets other tasks | R |
| initialize_task_manifest / add/remove_stage | state guards only | replace a foreign task's lifecycle + caps; strip future QA/review stages | R |
| update/delete/restore_stage (registry) | none; session never resolved | unattributed global registry mutation | R |
| record_pr_verdict/opened/state | none | approve/reject a foreign PR stage → unlocks merge | R |
| record_merge_result | integrity guards, zero identity | mark a foreign task merged with arbitrary SHA | R |
| record_plan_enhancement | none | reads owner's claim and **evicts it**; links task to caller | R |
| open_delivery_pr | none — file has zero session imports | push branch + open real GitHub PR for anyone's task | R |
| start/resume/cancel_expansion_run | none (cancel: session never resolved; resume: unscoped `run_id`) | cancel anyone's run; apply subtrees under foreign tasks | R |
| reset_expansion_output | symmetric `is claimed` refusal (caller-agnostic); `run is None` path unchecked | hard-delete run descendants | R |
| backup_tasks / restore_tasks | none | restore ingests caller-supplied JSONL as ledger state incl. claim/closure fields; backup exfiltrates to any path | R (operator-only) |
| reindex_tasks | none | `all_projects=true` resource lever | R (low) |

## Interim guard (#20821, 0.5.0)

One helper at the MCP layer: authorized when the task is **unclaimed**, the
caller **is the claiming session**, or caller and owner share the task's
**agent-run delegation lineage**; otherwise `TASK_CLAIM_CONFLICT` naming the
owner and the sanctioned path (message the owner; `claim_task` for stale
claims). Applied to the **G** rows. `delete_task` cascade default flips to
False. Storage APIs stay open for daemon internals; this guards the agent
boundary only.

## Hub contract (#20822, Rust-era)

Every transition declares allowed relationships (owner, delegation lineage,
operator, daemon) and daemon-verifiable preconditions, enforced at the hub so
MCP, HTTP, and CLI lanes share one contract; unlisted pairs fail closed;
exceptional transitions record the ground that fired plus evidence. Includes
the #20818 takeover grounds (`owner_dead`, `owner_idle`, `ceded`, `delegated`),
reviewer-identity separation on the `submit_close_review` pattern, gate-bearing
labels as protected fields, UUID project scoping, operator-only backup/restore,
dependency-edge ownership, and the attribution-conservation invariant: dirty
attributed paths are always owned; attribution ends only by commit,
clean-file-proof release, or recorded transfer.
