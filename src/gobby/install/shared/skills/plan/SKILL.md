---
name: plan
description: Adaptive /gobby plan workflow. Investigates first, recommends lightweight or full planning depth, requires decision elicitation, and preserves explicit human gates for artifact enhancement, adversarial review, and optional build handoff.
version: "4.0.0"
category: core
triggers: plan, specification, requirements
metadata:
  gobby:
    audience: interactive
    depth: 0
---

# /gobby plan

Both `$gobby plan` and `/gobby plan` invoke this workflow.

## Depth Selection and Required Elicitation

1. Investigate the request and repository before recommending a planning depth.
   Resolve discoverable facts with repository inspection, using `gcode` for code
   navigation. Do not ask the user for facts the repository can answer.
2. Determine whether the proposed implementation is a major change. Full-depth
   candidates are limited to:
   - a subsystem redesign or rework that changes multiple components and their
     consumers;
   - a complex new feature with multiple dependent deliverables across
     subsystems; or
   - a broad migration or architecture/security-model change with many
     consumers and a coordinated rollout.
3. Recommend **Full** only for a major change. Recommend **Lightweight** for bug
   fixes, maintenance, localized features or refactors, configuration, and
   documentation. Public API or schema involvement, security or destructive
   risk, unresolved product decisions, multi-agent coordination, durable
   handoff, and a desire for lifecycle automation or adversarial review increase
   the rigor within the chosen depth; none independently makes a change major.
   Ask the user to choose between the two depths. If the user already selected a
   depth in response to the Plan Mode Consider prompt, honor that choice without
   asking again.
4. Load `elicit` for every Gobby plan:

```text
get_skill(name="elicit") on gobby-skills
```

Run its grill-me protocol in both depths. Resolve discoverable facts through
repository inspection, ask one material decision at a time with a recommendation,
and finish with a confirmed Decision Record before drafting either plan.

## Lightweight Workflow

Load `plan-draft` and use its Plan-Coverage Contract as formatting guidance:

```text
get_skill(name="plan-draft") on gobby-skills
```

Write the decision-complete plan to `.gobby/plans/<slug>.md`. Keep concrete
deliverables, dependencies, file or subsystem targets, validation, risks, and
explicit out-of-scope boundaries, then run base validation:

```bash
uv run gobby plans validate <plan-file>
```

Lightweight skips enhancement and adversarial review by default. After the
validated draft, use the checkpoint menu below. `continue interactively`
continues plan refinement and returns to the same menu after validation. The
user may explicitly opt into either Full phase later without redrafting.

## Full Workflow

Full depth is artifact-first: it creates and revises a plan artifact without
creating task records for planning or per-round reviews.

Make the first line of every user-facing Full plan body — validated draft,
revised plan, or final plan — the authoritative artifact path:

```markdown
Plan artifact: `.gobby/plans/<slug>.md`
```

A link outside the plan body does not satisfy this requirement because copied
or independently rendered plans must retain their provenance.

`.gobby/plans/<slug>.md` is the artifact. Apply every revision — enhancement
votes, finding repairs, changelog entries, manifest writes — to that file and
only that file. Provider plan-mode files (`~/.claude/plans/*.md` and
equivalents) are display mirrors owned by the CLI, never the artifact: a mirror
goes stale the moment you edit the canonical file, and a stale mirror that still
reads like a finished plan is what gets reviewed or shipped by mistake. Either
resync a mirror immediately after each artifact edit, or stop maintaining it and
point the user at the artifact path. Never resolve a discrepancy by copying a
mirror over the artifact.

Choosing Full authorizes investigation, elicitation, and drafting only. Obtain
the explicit approvals described below before enhancement, adversarial review,
or build handoff. Selecting Full alone never launches any of those phases.

Load the drafting methodology before drafting:

```text
get_skill(name="plan-draft") on gobby-skills
```

The adversarial review methodology lives in `plan-review`. The taskless
`plan-adversary-taskless` agent loads it during review.

The constructive enhancement methodology lives in `plan-enhance`. The taskless
`plan-enhancer-taskless` agent loads it during the enhancement phase (step 4.5),
which runs after enhancement approval and before the adversary gate.

### Draft checkpoint

1. Use the confirmed Decision Record as drafting input. Persist every settled
   requirement, constraint, risk, default, and success criterion in the
   canonical plan; do not create a sibling requirements document.
2. Draft `.gobby/plans/<slug>.md` with the Plan-Coverage Contract.
3. Run `uv run gobby plans validate <plan-file>`.
4. Present the checkpoint menu. If the user chooses `continue interactively`,
   ask separately whether to run enhancement. Declining enhancement does not
   imply adversarial-review approval.

### Enhancement phase

Enhancement is advisory, default-on for Full, and capped at one round unless
the user changes the cap. Start it only after explicit enhancement approval.

1. Prepare evidence, spawn `plan-enhancer-taskless` without `task_id` using
   `isolation="none"`, and pass `evidence_id`, `artifact_path`, round number,
   cap, and parent session id.
2. Immediately call `gobby-sessions:compact_self`, then use **Waiting on
   Spawned Runs**. In a terminal session that call comes back as a rejected or
   cancelled tool use attributed to the user. That is the daemon interrupting
   the turn to deliver the compaction command, never a refusal: do not stop, do
   not ask the user about it, and resume from the continuation prompt.
3. Present every suggestion with its full text and metadata. Collect one
   accept/decline vote per suggestion before editing. Apply only accepted
   suggestions, append the enhancement changelog entry, and base-validate. In
   unattended mode, the coordinator judges every item and records each vote
   with its rationale.
4. Present the checkpoint menu. If the user chooses `continue interactively`,
   ask separately whether to begin adversarial review.

### Adversarial review phase

Start only after explicit adversarial-review approval.

1. Call `prepare_plan_review_round` immediately before spawning
   `plan-adversary-taskless` without `task_id`, using `isolation="none"`. Pass
   only `plan_path`, `round_number`, and optional `project`, `session_id`,
   `task_id`, and `stage` preparation inputs. Pass the returned `evidence_id`,
   artifact path, round number, cap, and parent session id to the agent.
2. Bind the spawned run with `bind_evidence_run`. Expire the evidence if spawn
   or bind fails. After a successful bind, immediately call
   `gobby-sessions:compact_self`, then use **Waiting on Spawned Runs**. In a
   terminal session that call comes back as a rejected or cancelled tool use
   attributed to the user. That is the daemon interrupting the turn to deliver
   the compaction command, never a refusal: do not stop, do not ask the user
   about it, and resume from the continuation prompt.
3. Read the canonical result. Present every finding with its full text and
   metadata, and collect one accept/decline vote per finding before editing.
   Record declined items and deferrals explicitly. In unattended mode, the
   coordinator judges every item and records each vote with its rationale.
4. For a `needs_review` result, persist the rejection checkpoint before applying
   accepted repairs, so the checkpoint records the reviewed artifact and the
   next round snapshots the repaired one. Call
   `append_plan_changelog_round(evidence_id, prose, round_result)`, then
   `finalize_plan_review_evidence(evidence_id, round_result)`. Pass the
   adversary's canonical payload verbatim as `round_result` to both calls; a
   rejection round without it fails with `missing_round_result` unless a durable
   intent already exists. Only after both calls succeed, apply accepted repairs:
   call `apply_plan_review_repairs(evidence_id, accepted_finding_ids)` with
   every accepted finding id — it applies the typed `repairs` those findings
   carry, is idempotent and all-or-nothing, and returns the unified diff plus
   `plan_hash_before`/`plan_hash_after`; then hand-apply the accepted
   prose-only fixes (findings the result lists as `prose_only`) and
   base-validate the artifact. Increment `completed_plan_review_rounds`
   only when finalization succeeds; display attempts, expired evidence, and
   incomplete rounds never count.
5. Present the checkpoint menu after every finalized round and any accepted
   repairs. Choosing
   `continue interactively` after `needs_review` revises and launches the next
   approved review round. Choosing it after `approved` keeps planning open;
   edits invalidate approval and require a fresh reviewed round.
   When a `needs_review` verdict reaches the configured review cap, process
   every final finding and vote before editing, append and finalize the normal
   rejection checkpoint on the unchanged artifact with the canonical
   `round_result`, apply accepted repairs, base-validate, then append a
   human-handoff changelog entry. Do not launch another adversary round.
   Continue only through the explicit human-handoff tools described below.
6. On approval, apply the server-derived manifest, persist the checkpoint,
   finalize evidence, complete lesson-mint checkpointing, and run:

   ```bash
   uv run gobby plans validate <plan-file> --mode expansion
   ```

   Then present the checkpoint menu as the final-approval checkpoint.

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
mandatory post-launch `gobby-sessions:compact_self` call:

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

## Build Handoff States

Use build seed inputs for plan-file handoff:

- `planning_seed_state=drafted`: start at planning.
- `planning_seed_state=needs_review`: start at planning review with
  `completed_plan_review_rounds` already counted.
- `planning_seed_state=approved`: start directly at expansion.

`/gobby expand` remains available for manual expansion, debugging, and targeted
reruns. Full planning offers approved artifacts to `gobby build` only after the
user approves the optional handoff.

## Boundaries

- Do not create or claim tasks during plan drafting or plan review.
- Any `.md` under `.gobby/`, `.claude/`, or `.codex/` (CLI-owned artifact
  trees) is exempt from `require-task-before-edit`.
- Enhancement (step 4.5) is advisory: apply only user-accepted suggestions, and
  never let it gate, approve, reject, or block the adversary review. The human
  is the scope gate.
- Do not emit `[TDD]`, `[IMPL]`, or `[REF]` tasks in the plan.
- Do not leave unanswered questions in the final plan. Resolve them before
  approval or record them as explicit out-of-scope deferrals.
- Do not bypass expansion-mode validation before build handoff.
