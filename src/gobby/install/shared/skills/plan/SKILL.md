---
name: plan
description: Artifact-first /gobby plan workflow. Drafts and revises a plan with the user, runs a constructive enhancement pass and then taskless adversarial review after approval, records enhancement and review history in the plan, and hands approved plans to gobby build.
version: "3.0.0"
category: core
triggers: plan, specification, requirements
metadata:
  gobby:
    audience: interactive
    depth: 0
---

# /gobby plan

`/gobby plan` creates and revises a plan artifact. It does not create a planning
epic, review-anchor task, or per-round review tasks.

The drafting methodology lives in `plan-draft`. Load it before drafting:

```text
get_skill(name="plan-draft") on gobby-skills
```

The adversarial review methodology lives in `plan-review`. The taskless
`plan-adversary-taskless` agent loads it during review.

The constructive enhancement methodology lives in `plan-enhance`. The taskless
`plan-enhancer-taskless` agent loads it during the enhancement phase (step 4.5),
which runs after approval and before the unchanged adversary gate.

## Workflow

1. Gather requirements, constraints, risks, and success criteria.
2. Draft `.gobby/plans/<slug>.md` using the Plan-Coverage Contract from
   `plan-draft`.
3. Run plan verification locally:

   ```bash
   uv run gobby plans validate <plan-file>
   ```

4. Ask the user to approve the draft for adversarial review.

   **Step 4.5 — Enhancement phase** (default on, `max_enhancement_rounds = 1`):
   after approval and before the adversary gate, run the constructive
   Better/Bigger pass.

   1. Spawn `plan-enhancer-taskless` without `task_id` and with
      `isolation="none"`. Pass `artifact_path`, `round_number`,
      `max_enhancement_rounds`, and the parent session id in the prompt or
      variables.
   2. Wait for the run completion message, then surface the ranked suggestions
      to the user — highest impact-vs-effort first — for accept/decline. The
      human is the scope gate (present-and-stop): you present, the user decides
      what ships.
   3. Apply only the **accepted** suggestions to the plan artifact, re-run
      `uv run gobby plans validate <plan-file>`, and append a `## V1 Plan
      Changelog` entry with `kind: enhancement`.
   4. Stop on `converged: true`, all suggestions declined, or the
      `max_enhancement_rounds` cap. The enhancer never gates; control then
      proceeds to the unchanged adversary gate in step 5.
5. After the enhancement phase, spawn `plan-adversary-taskless` without
   `task_id` and with `isolation="none"`. Pass `artifact_path`, `round_number`,
   `max_review_rounds`, and the parent session id in the prompt or variables.
6. Wait for the adversary run completion message. Read the run result and
   append a `## V1 Plan Changelog` entry with `kind: verification`.
7. If the verdict is `needs_review`, revise the plan with the user, rerun
   validation, and dispatch the next taskless adversary round until the review
   cap is reached.
8. If the verdict is `approved`, ensure `## M1 Task Manifest` exists and passes
   expansion-mode validation:

   ```bash
   uv run gobby plans validate <plan-file> --mode expansion
   ```

9. Hand the approved artifact to build:

   ```bash
   uv run gobby build <plan-file> --planning-seed-state approved --completed-plan-review-rounds <N>
   ```

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
```

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

`## M1 Task Manifest` is the build bridge. It may be written by the approving
adversary or by the interactive plan coordinator after the user accepts the
final revision.

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
reruns. `/gobby plan` should hand approved artifacts to `gobby build`.

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
