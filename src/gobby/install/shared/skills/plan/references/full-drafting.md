# Full drafting and enhancement

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

### Agent roles and tier boundary

Plan orchestration names planner, enhancer, and adversary roles only. Provider,
model, and reasoning-effort choices live in user-editable agent profiles. Omitted
profile fields inherit provider or session defaults; explicit profile values win.
Internal research and mechanical repair use relative tier language, never a model
name, so the workflow remains portable across profile inventories.

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

1. Spawn `plan-enhancer-taskless` without `task_id` using `isolation="none"`,
   and pass `artifact_path`, round number, cap, and parent session id. No
   evidence is prepared for enhancement; `prepare_plan_review_round` and
   `bind_evidence_run` belong to adversary rounds only.
2. Immediately call `gobby-sessions:set_handoff` with `clear_session=false`, then use **Waiting on
   Spawned Runs**. In a terminal session that call comes back as a rejected or
   cancelled tool use attributed to the user. That is the daemon interrupting
   the turn to deliver the compaction command, never a refusal: do not stop, do
   not ask the user about it, and resume from the continuation prompt.
3. Present every suggestion with its full text and metadata. Collect one
   accept/decline vote per suggestion before editing. Each vote walks
   `restraint`'s decision ladder: a suggestion that stops at a lower rung than
   the mechanism it proposes is declined `over-mechanism` with the rung named.
   Apply only accepted suggestions, append the enhancement changelog entry, and
   base-validate. In unattended mode, the coordinator judges every item and
   records each vote with its rationale.
4. Present the checkpoint menu. If the user chooses `continue interactively`,
   ask separately whether to begin adversarial review.
