# Planning evidence and recovery

### Recovery

Both checkpoint calls are idempotent, so the remedy for every step-4 failure is
to re-run the step with the canonical payload:

- `missing_round_result` or `stale_plan_evidence` from `append_plan_changelog_round`:
  the call ran without the adversary's payload (or with a different one), so
  re-call it with the canonical `round_result`. A `needs_review` payload never
  verifies reviewed bytes; accepted repairs applied early do not block it.
- `missing_v1_checkpoint` from `finalize_plan_review_evidence`: the round fence
  is not in `## V1 Plan Changelog` yet, so call `append_plan_changelog_round`
  with the canonical payload, then finalize again.
- `invalid_repair` from `apply_plan_review_repairs`: the plan is untouched, so
  hand-apply that finding's `fix`, then re-run the call without that finding
  id; re-running `apply_plan_review_repairs` is always safe because applied
  repairs come back as `already_present`.

Never hand-build the fence; the daemon renders it on append.

## Universal Checkpoint and Handoff Contract

After drafting, enhancement, every finalized adversary round, and final
approval, present exactly these choices:

1. `continue interactively`
2. `hand off to build`
3. `stop`

`stop` leaves the latest base-validated canonical artifact in place and starts
no build.

`hand off to build` is explicit human approval to skip all remaining
enhancement and adversarial rounds. Handoff always:

1. Materializes a complete canonical artifact. During elicitation or drafting,
   use recorded decisions and explicit named defaults for unresolved
   non-material details; include those defaults in `## Constraints`. Resolve
   every material gap before handoff.
2. Runs base validation.
3. Derives explicit routing decisions for every deliverable, calls
   `derive_plan_handoff_manifest(plan_path, routing_decisions)`, and passes its
   exact `source_plan_hash`, `rendered_plan_hash`, and `manifest_digest` to
   `apply_plan_handoff_manifest`. These coordinator-only tools perform the only
   handoff manifest write.
4. Runs `uv run gobby plans validate <plan-file> --mode expansion`.
5. Invokes build with `planning_seed_state=approved` and the count of finalized
   adversary rounds:

   ```bash
   uv run gobby build <plan-file> --planning-seed-state approved --completed-plan-review-rounds <N>
   ```

If handoff is requested while an enhancer or adversary is active, mark it
pending and wait for that run to finish. Present every returned suggestion or
finding for individual voting, apply accepted edits, complete the normal
changelog/checkpoint/finalization work for that phase, and base-validate. Then
perform handoff without launching another enhancement or adversary round.

Human handoff never manufactures an adversary verdict, findings,
`coverage_attestation`, or review evidence. Never invoke `emit_stub_manifest`;
the coordinator handoff derivation is the sole evidence-independent route to a
canonical M1 manifest.

## Interactive Review Evidence Protocol

Interactive votes and review prompts use `request_user_input` directly. Provider
interaction remains within the current turn until the user responds.

Use the adversary's canonical result as the sole round payload. Every conclusive
result carries trusted three-lane `coverage_attestation`. Rejection results
carry typed findings and shadow-manifest status; repair-class findings also
carry typed `repairs` that only `apply_plan_review_repairs` writes, after the
vote and the finalized checkpoint. Approval results carry `findings`, routing
decisions, and exact server-derived manifest entries. The adversary never
edits the plan.

Approval commit order is fixed, idempotent, and resumable:

1. Call `apply_plan_review_manifest` with the complete approval result. This is
the only manifest write. It calls `verify_plan_unchanged` at compare-and-apply,
then persists the complete result as the durable pre-finalization approval
intent in the same durable write as the manifest checkpoint. Reviewed-section
drift before this intent refuses the round with the row unfinalized, the plan
untouched by the apply, and no lesson mint.
2. Call `append_plan_changelog_round` with the round prose. The daemon renders
the canonical fence from the durable intent (byte-identical to
`render_plan_changelog_round` output) and atomically inserts prose + fence at
the end of `## V1 Plan Changelog`. Never hand-edit the changelog.
3. Call `finalize_plan_review_evidence` with the durable result. Finalization is
gated on the exact V1 fence and atomically stamps
`lesson_mint_status=pending` with `finalized_at`.
4. Mint proven plan lessons from prior finalized evidence, then call
`checkpoint_plan_review_lesson_mint`: `minted` with lesson ids, `failed` with
the recorder error, or `none` when no class is provable. Approval completes
only after the status leaves `pending`.

Rejection rounds have no freshness gate; `append_plan_changelog_round` verifies
reviewed bytes only for approved payloads. The approval freshness gate lives in
`apply_plan_review_manifest` and exists only in step 1. Plan drift after a
completed step 1 cannot block steps 2–4 because they read the immutable
evidence row, durable intent, and persisted manifest entries. An identical
step-1 retry returns its recorded result without another write. Finalize, mint,
and mint checkpointing are also idempotent.

A crash before step 1 records its intent leaves an incomplete approval: no
proof, no mint, and no plan change. This incomplete-round rule also applies to
rejection rounds whose canonical V1 checkpoint was never persisted. Every
approval crash after the intent write is recovered:

- `manifest_state=pending` with unchanged reviewed sections converges to
  `applied`; recovery then persists the exact V1 fence, finalizes, and exposes
  `pending_lesson_mint`.
- `manifest_state=pending` with reviewed-section drift atomically becomes
  `manifest_state=revoked`, clears `round_result`, leaves plan bytes untouched,
  and expires after its bound run is terminal. Re-review is required.
- `manifest_state=applied` resumes from step 2 even when later plan drift exists.
- A durable V1 fence on an unfinalized row is reconciled losslessly at the next
  preparation.
- A finalized approval at `lesson_mint_status=pending` blocks preparation with
  `pending_lesson_mint` until the surfaced payload is minted exactly once and
  checkpointed.

The next `prepare_plan_review_round` runs this recovery drain before creating a
round. It converges pending applies, persists missing V1 bytes, finalizes from
durable intent, and refuses new work while mint completion is pending.

At final approval, classify lessons from changelog evidence:

- `reviewer-miss` requires every id in `participating_section_ids` to be
hash-unchanged since an earlier finalized reviewed round.
- `fixer-induced-defect` requires changed hashes for every id in
`causal_section_ids`, plus `causal_finding_id` and `introduced_in_round`.
- Dual classification requires both complete bundles. An unproven class mints
nothing for that class.

Record with `source_kind=plan_review`, the class-scoped identity from
`review-learning`, `guardrail_target=checklist`, and
`finding.rule_id=plan-review:<adversary-category>`. Never use a plan-file path
as the promotion anchor. Mint at most five lessons per plan with class-aware
selection: reserve at least one slot for each present class; rank reviewer
misses by rounds missed and fixer-induced defects by causal occurrences, then
severity, `check_key` ascending, and `finding_id` ascending.

## Waiting on Spawned Runs

Use this same policy for enhancer and adversary runs after completing the
mandatory post-launch `gobby-sessions:set_handoff` call:

1. Keep doing useful independent work while the run is active.
2. When the enhancer or parent adversary run is active and no actionable
   independent work remains, subscribe once by calling
   `gobby-agents:wait_for_agent(run_id)`. If the run
   remains active, end the turn and let the daemon wake resume the session.
   Never replace this subscribe-once protocol with a custom foreground poll,
   direct agent-run API polling, or a Bash sleep heartbeat. The daemon wake is
   the only supported resume mechanism for an active subscribed run.
3. On the daemon wake, re-call `gobby-agents:wait_for_agent(run_id)` first to
   retrieve the terminal snapshot, then perform a full status and health sweep.
   Call `gobby-agents:get_agent_result(run_id)` only if you need to re-read the
   final report. When its payload includes capture metadata, page
   `gobby-agents:get_agent_capture` until `next_offset` is null before consuming
   the complete report.

The mandatory compaction immediately after each launch takes priority over
independent work and waiting. A run already known to be
terminal skips subscribing and waiting and goes directly to `get_agent_result`.

## Changelog Contract

`## V1 Plan Changelog` is a single `kind: verification` section that accumulates
both enhancement rounds (step 4.5) and adversary rounds (step 5). Each round
entry is a **bold label**, never a `###` heading — a `### Round N` heading is not
a canonical contract section and makes `uv run gobby plans validate` fail with
`non-canonical heading missing kind: framing`.

Every adversarial round appends a `kind: verification` entry to
`## V1 Plan Changelog`:

```markdown
## V1 Plan Changelog
`kind: verification`

**Round <N>** `kind: verification`

- reviewer_run: <run-id>
- reviewer_session: <session-id>
- verdict: approved | needs_review
- findings:
- <finding id/severity/summary>
- resolution_notes: <what changed or why no change was needed>

<exact fenced JSON bytes rendered by append_plan_changelog_round>
```

The prose fields are a projection of the checkpoint payload. Record adversary
rounds with `append_plan_changelog_round` — the daemon renders the fence and
inserts the entry atomically; never hand-build, reformat, or reorder its JSON.

Every enhancement round (step 4.5) appends a `kind: enhancement` entry to the
same changelog:

```markdown
**Round <N>** `kind: enhancement`

- enhancer_run: <run-id>
- enhancer_session: <session-id>
- converged: true | false
- suggestions_presented: <count>
- accepted:
  - <suggestion id / lens / one-line summary>
- declined:
  - <suggestion id / lens / one-line summary>
- resolution_notes: <what was folded into the plan, or why nothing changed>
```

Keep prior rounds. Do not overwrite history.

## Manifest Contract

`## M1 Task Manifest` is the build bridge. The approving adversary returns its
full typed entries in the verdict payload. The coordinator writes them only
through `apply_plan_review_manifest` after the user accepts the final revision.

Each manifest entry maps one deliverable section to one expansion leaf. For
`category: code`, include `implementation_domain`:

```yaml
- title: "Implement widget API and UI"
  category: code
  implementation_domain: fullstack
  task_type: task
  source_section: "1.1"
  depends_on: []
  validation_criteria: "Focused tests prove the API and UI behavior."
  labels:
    - "covers:<plan-id>:1.1:1.1.1"
  tdd: true
```

Valid domains are `backend`, `frontend`, and `fullstack`.

Use `tdd: true` for code leaves that require behavior changes. Use it for
`config` only when the plan identifies executable behavior that can be pinned
before changing configuration.
