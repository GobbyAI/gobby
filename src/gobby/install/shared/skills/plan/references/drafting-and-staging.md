# Plan drafting and staging

## Plan Drafting and Staging

Choosing the plan route authorizes investigation, elicitation, and drafting only.
Obtain explicit approval before enhancement or adversarial review. The route alone
never launches either optional phase or implementation.

Load the drafting methodology before drafting:

```text
get_skill(name="plan-draft") on gobby-skills
```

The adversarial review methodology lives in `plan-review`. The taskless
`plan-adversary-taskless` agent loads it during an approved review. The constructive
enhancement methodology lives in `plan-enhance`; `plan-enhancer-taskless` loads it
during an approved enhancement pass.

### Canonical file authority

When the provider permits project plan writes, create
`.gobby/plans/<slug>.md` immediately and make it the sole authority. Make the
first line of every user-facing canonical plan body:

```markdown
Plan artifact: `.gobby/plans/<slug>.md`
```

A link outside the plan body does not satisfy this requirement because copied or
independently rendered plans must retain their provenance. Apply every accepted
revision, changelog entry, and manifest write to that file only.

Provider plan-mode files (`~/.claude/plans/*.md` and equivalents) are display
mirrors owned by the CLI. Resync a mirror after each canonical edit or stop
maintaining it and point the user at the canonical file. Never copy a stale mirror
over the canonical file.

### Write-restricted staging

When the provider cannot write the canonical project path, keep the plan in the
conversation as a staged draft. Never ask another agent or MCP tool to write around
the restriction. Create no scratch file, draft-storage tool, or periodic autosave.
A conversational draft has not passed deterministic validation and is not a
validated artifact.

At every compaction boundary, call `gobby-sessions:set_handoff` with
`clear_session=false` and preserve:

- the complete latest draft, including Markdown and code fences, in
  `current_state`;
- the confirmed Decision Record and review-stage approvals in `key_decisions`;
- unresolved material questions in `notes`;
- concrete continuation actions in `next_steps`.

After compaction, call `gobby-sessions:get_handoff` with no arguments before doing
more planning. Restore the exact staged draft and metadata, then continue from the
recorded `next_steps`.

In a terminal session that call comes back as a rejected or cancelled tool use
attributed to the user. That is the daemon interrupting the turn to deliver the
compaction command, never a refusal: do not stop, do not ask the user about it,
and resume from the continuation prompt.

This is staging in the existing handoff, not a second plan authority. Do not claim
that the staged conversation was saved as a plan file, registered, or validated.

### Materialization and validation

As soon as the provider permits the project write, Materialize the complete latest
draft at `.gobby/plans/<slug>.md`. Replace any staging-only provenance line with
the canonical `Plan artifact:` line. Confirm the file contains the complete draft,
then treat the file as the sole authority; later handoffs point to its path and
hash instead of duplicating the full body.

Run file-based base validation before starting any review and after every accepted
revision:

```bash
uv run gobby plans validate <plan-file>
```

The equivalent `gobby-plans:validate_plan(plan_file)` MCP operation is also
file-only. It cannot validate a conversational draft.

### Draft checkpoint

After the canonical file passes base validation, present the universal checkpoint.
`continue interactively` returns here after any revision and fresh validation.

### Agent roles and tier boundary

Plan orchestration names planner, enhancer, and adversary roles only. Provider,
model, and reasoning-effort choices live in user-editable agent profiles. Omitted
profile fields inherit provider or session defaults; explicit profile values win.
Internal research and mechanical repair use relative tier language.

### Enhancement phase

Enhancement is recommended, optional, advisory, and capped at one round unless the
user changes the cap. Start it only after explicit enhancement approval and before
an optional adversarial review.

1. Spawn `plan-enhancer-taskless` without `task_id` using `isolation="none"`.
   Pass `artifact_path`, round number, cap, and parent session id. Prepare no review
   evidence; evidence preparation belongs to adversary rounds.
2. Immediately call `gobby-sessions:set_handoff` with `clear_session=false`, then
   use **Waiting on Spawned Runs**. In a terminal session that call comes back as a
   rejected or cancelled tool use attributed to the user. That is the daemon
   interrupting the turn to deliver the compaction command, never a refusal: do not
   stop, do not ask the user about it, and resume from the continuation prompt.
3. Present every suggestion with its full text and metadata. Collect one
   accept/decline vote per suggestion before editing. Apply only accepted
   suggestions, append the enhancement changelog entry, and base-validate. In
   unattended mode, the coordinator judges every item and records each vote with
   its rationale.
4. Present the universal checkpoint. Declining or completing enhancement never
   implies approval for adversarial review or implementation.

Enhancement must never let it gate, approve, reject, or block the adversary review.
The human is the scope gate.
