---
name: plan
description: Adaptive /gobby plan workflow. Investigates first, recommends lightweight or full planning depth, requires decision elicitation, and preserves explicit human gates for artifact enhancement, adversarial review, and optional build handoff.
version: "3.5.0"
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
2. Assess these complexity signals:
   - multiple dependent deliverables or subsystems;
   - public API, schema, migration, security, or destructive-risk work;
   - material unresolved product decisions;
   - multi-agent coordination or durable handoff requirements; or
   - an explicit desire for an artifact, lifecycle automation, or adversarial
     review.
3. Recommend **Full** when one or more signals materially affect the work.
   Recommend **Lightweight** for localized, low-risk work. Ask the user to choose
   between the two depths. If the user already selected a depth in response to
   the Plan Mode Consider prompt, honor that choice without asking again.
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

Produce a conversational, decision-complete plan. Keep concrete deliverables,
dependencies, file or subsystem targets, validation, risks, and explicit
out-of-scope boundaries. Lightweight depth has no plan artifact, artifact
validation, enhancement pass, adversarial review, or build handoff. End after
the user receives the conversational plan.

## Full Workflow

Full depth is artifact-first: it creates and revises a plan artifact. Full depth
does not create a planning epic, review-anchor task, or per-round review tasks.

Make the first line of every user-facing Full plan body — validated draft,
revised plan, or final plan — the authoritative artifact path:

```markdown
Plan artifact: `.gobby/plans/<slug>.md`
```

A link outside the plan body does not satisfy this requirement because copied
or independently rendered plans must retain their provenance.

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

1. Use the confirmed Decision Record as the requirements, constraints, risks,
   and success-criteria source.
2. Draft `.gobby/plans/<slug>.md` using the Plan-Coverage Contract from
   `plan-draft`.
3. Run plan verification locally:

   ```bash
   uv run gobby plans validate <plan-file>
   ```

4. Ask the user to approve the validated draft for the enhancement phase.

**Step 4.5 — Enhancement phase** (default on, `max_enhancement_rounds = 1`):
after approval and before the adversary gate, run the constructive
Better/Bigger pass.

   1. Spawn `plan-enhancer-taskless` without `task_id` and with
      `isolation="none"`. Pass `artifact_path`, `round_number`,
      `max_enhancement_rounds`, and the parent session id in the prompt or
      variables.
   2. Immediately after every enhancer launch, call
      `gobby-sessions:compact_self` for the parent session before waiting or
      doing any other work. This is mandatory on every enhancement round.
   3. Wait as described in **Waiting on Spawned Runs**, then present a ranked
      summary of every suggestion — highest impact-vs-effort first — including
      its `id`, lens, location, impact, effort, risk, and a one-line gist.
      Present each suggestion's full text verbatim, including its description,
      suggested enhancement, and metadata. When a suggestion modifies existing
      text, quote the current plan sections it touches. Collect an individual
      accept/decline vote for each suggestion, and allow per-item exploration
      before recording its vote. Put the complete presentation and vote prompt
      in the interaction payload itself, with the full item text inside that
      payload; free text emitted outside tool calls is not guaranteed to render.
      If the user declines an item with deferral, record a typed
      `kind: deferred` plan section that points to its follow-up task (section
      2.3 is the worked example). The human is the scope gate
      (present-and-stop): you present, the user decides what ships.
   4. Apply only the **accepted** suggestions to the plan artifact, re-run
      `uv run gobby plans validate <plan-file>`, and append a `## V1 Plan
      Changelog` entry with `kind: enhancement`.
   5. Stop on `converged: true`, all suggestions declined, or the
      `max_enhancement_rounds` cap. The enhancer never gates; control then
      returns to the user. Ask for separate approval before starting adversarial
      review in step 5.
5. After the enhancement phase, call `prepare_plan_review_round` immediately
before you spawn `plan-adversary-taskless` without `task_id` and with
`isolation="none"`. Do not pass provider or model: the reviewer model is the
agent definition's decision, not the coordinator's. Pass an override only when
the user explicitly asks for a different reviewer provider or model. Pass the
prepared snapshot/evidence, `artifact_path`,
`round_number`, `max_review_rounds`, and the parent session id in the prompt or
variables. The adversary reads
`changed_section_ids` and `review_complexity` from that evidence snapshot.
Evidence preparation is the single indexing site for the round.
The plan-mode entry observer owns the initiating-request anchor. Never create,
replace, or synthesize that anchor in this skill. Preparation fails closed when
taskless entry has neither exact request content nor a valid persisted anchor.
The immutable `requirements_bundle` in the prepared evidence is the reviewer's
only requirements context; do not send a live or reconstructed requirements
payload alongside it.
Do not run a separate pre-spawn `gcode index`; preparation already brackets the
one index run it needs. Repository churn during a round is expected and is not
tracked — see the repository-state section in `plan-review`.
For every round after the first, also pass `prior_finding_resolutions` and
`repair_attestations` to `prepare_plan_review_round`. Build exactly one
resolution record per finding in the prior finalized result:
`prior_finding_id` plus `decision: repair | carry`. Every `repair` record needs
one attestation. After applying the accepted edits, call
`derive_plan_review_sweep_scope` with the finalized prior evidence and the
edited plan path, plus `repair_finding_ids` containing exactly the findings
whose recorded decision is `repair`. Submit the returned `sweep_scope` and
`sweep_scope_digest` as top-level preparation inputs.
Build the attestation against that typed graph and copy its canonical digest
into `sweep_scope_digest`. Preparation verifies the submitted graph and digest,
derives current repository reality separately, and exposes both
`required_scope_delta` and `inventory_churn` to the adversary.
Each attestation carries `prior_finding_id`, `check_key`,
`changed_section_ids`, `accepted_resolution`,
`deviation_from_minimal_repair`, `changed_symbols`,
`consumer_sites_swept`, `adjacent_variants_swept`, `validation_evidence`, and
`deferred_sites`, plus `sweep_scope_digest`, `sweep_query_evidence`, and
`repair_bundle_interactions`. Sweep every required site and adjacent-variant
ID. A deferral is an object with `site_id` and a non-empty `reason`; an empty
derived site set requires the exact query that returned no sites in
`sweep_query_evidence`. Emit one interaction record for every graph edge,
carrying `edge_id`, `disposition`, and non-empty `validation_evidence`.
Preparation refuses malformed attestations, duplicate identities, stale check
keys, and section claims outside the real hash diff. Repository drift proceeds
as adversary input. `carry` applies only to non-blocking findings.
Set `deviation_from_minimal_repair` to `null` when following the minimal repair.
For a deviation, use one closed object with exactly `violated_invariant`,
`original_counterexample`, `how_alternative_closes_it`, `validation_evidence`,
and `accepted_risk`. Every value is a non-empty string; use the literal `none`
for `accepted_risk` when the deviation adds no risk.
The coordinator waits only for the parent adversary result; the adversary owns
provider-native internal research results, timeouts, and sequential lane
fallbacks. The adversary never uses Gobby-managed agents for lane research.
Immediately bind
the interactive run id returned by `spawn_agent`
with `bind_evidence_run`. If spawn or bind fails, call
`expire_plan_review_evidence`; the next attempt prepares fresh evidence.
Immediately after a successful bind, call `gobby-sessions:compact_self` for the
parent session before waiting or doing any other work. This is mandatory on
every adversarial review round.
6. Wait as described in **Waiting on Spawned Runs** for the adversary run. Read
the run result as the canonical round result. It must contain the
`coverage_attestation` returned by `validate_plan_review_coverage`; approval
also contains exact server-derived `routing_decisions`, `manifest_entries`, and
`approval_result.quality_ledger`. Render that full ledger beside the manifest;
it records every explicit non-blocking quality decision shown to the user.
Treat the ledger as approval output; approval inputs remain the canonical
findings, routing decisions, manifest entries, and coverage attestation.
For `inconclusive/source_drift`, expire evidence and respawn the same display
round without appending a changelog entry, incrementing the round, finalizing
evidence, or minting lessons.
For a terminal agent timeout, classify the result as `inconclusive` with reason
code `timeout` and `timeout_seconds: 2700`. Do not reuse partial lane output or
create a timeout checkpoint. Call `expire_plan_review_evidence`, then retry the
same display round from a fresh snapshot, inventory, and index token under a
newly bound adversary run. This is the sole timeout recovery path; do not append
a changelog entry, increment the round, finalize evidence, or mint lessons.
Otherwise append a `## V1 Plan Changelog` entry,
persist the exact fence returned by `render_v1_round_checkpoint`, then call
`finalize_plan_review_evidence` with that same result. Before any step 7
revision, present a ranked
summary of every finding, including its `id`, severity, location, impact,
effort, risk, and a one-line gist. Present each finding's full text verbatim,
including its description, finding detail, and metadata. When a finding
   modifies existing text, quote the current plan sections it touches. Collect
   an individual accept/decline vote for each finding, and allow per-item
   exploration before recording its vote. Put the complete presentation and
   vote prompt in the interaction payload itself, with the full item text inside
   that payload; free text emitted outside tool calls is not guaranteed to
   render. If the user declines an item with deferral, record a typed
   `kind: deferred` plan section that points to its follow-up task (section 2.3
   is the worked example).
7. If the verdict is `needs_review`, recall `fixer-induced-defect` plan lessons
before revising. Record one `repair` or `carry` vote for every finding, revise
the plan with the user, capture the actual changed sections and symbols plus
consumer, adjacent-variant, validation, and deferred-site evidence for each
repair vote, rerun validation, and dispatch the next taskless adversary round
until the review cap is reached.
8. If the verdict is `approved`, run the approval sequence below. Ensure the
resulting `## M1 Task Manifest` passes expansion-mode validation:

   ```bash
   uv run gobby plans validate <plan-file> --mode expansion
   ```

9. Offer build handoff as an optional final step. Run it only after the user
explicitly approves the handoff:

```bash
uv run gobby build <plan-file> --planning-seed-state approved --completed-plan-review-rounds <N>
```

## Interactive Review Evidence Protocol

Use the adversary's canonical result as the sole round payload. Every conclusive
result carries trusted three-lane `coverage_attestation`. Rejection results
carry typed findings and shadow-manifest status; approval results carry
`findings`, routing decisions, and exact server-derived manifest entries. The
adversary never edits the plan.

Approval commit order is fixed, idempotent, and resumable:

1. Call `apply_plan_review_manifest` with the complete approval result. This is
the only manifest write. It calls `verify_plan_unchanged` at compare-and-apply,
then persists the complete result as the durable pre-finalization approval
intent in the same durable write as the manifest checkpoint. Reviewed-section
drift before this intent refuses the round with the row unfinalized, the plan
untouched by the apply, and no lesson mint.
2. Call `render_v1_round_checkpoint` from the durable intent and persist the
returned canonical fence byte-for-byte in `## V1 Plan Changelog`.
3. Call `finalize_plan_review_evidence` with the durable result. Finalization is
gated on the exact V1 fence and atomically stamps
`lesson_mint_status=pending` with `finalized_at`.
4. Mint proven plan lessons from prior finalized evidence, then call
`checkpoint_plan_review_lesson_mint`: `minted` with lesson ids, `failed` with
the recorder error, or `none` when no class is provable. Approval completes
only after the status leaves `pending`.

The freshness gate exists only in step 1. Plan drift after a completed step 1
cannot block steps 2–4 because they read the immutable evidence row, durable
intent, and persisted manifest entries. An identical step-1 retry returns its
recorded result without another write. Finalize, mint, and mint checkpointing
are also idempotent.

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
- verdict: approved | needs_review | needs_requirements
- findings:
- <finding id/severity/summary>
- resolution_notes: <what changed or why no change was needed>

<exact fenced JSON bytes returned by render_v1_round_checkpoint>
```

The prose fields are a projection of the checkpoint payload. Paste the rendered
fence verbatim; never hand-build, reformat, or reorder its JSON.

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
- Do not create review anchors.
- Enhancement (step 4.5) is advisory: apply only user-accepted suggestions, and
  never let it gate, approve, reject, or block the adversary review. The human
  is the scope gate.
- Do not emit `[TDD]`, `[IMPL]`, or `[REF]` tasks in the plan.
- Do not leave unanswered questions in the final plan. Resolve them before
  approval or record them as explicit out-of-scope deferrals.
- Do not bypass expansion-mode validation before build handoff.
