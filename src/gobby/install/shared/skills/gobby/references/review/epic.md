# Epic review

Load before launching or performing an epic implementation review. Also load
[evidence](evidence.md) before assessment and [outcomes](outcomes.md) before a
verdict. Load standalone `proportionality`; preserve the installed epic-reviewer
methodology.

## Discover and select

Resolve the epic with `gobby-tasks:get_task(brief=false)`. Accept task references
such as `#N`, numeric refs and dotted paths. Ask for a missing target. Ask for
interactive or delegated mode unless the request already establishes it; preserve
the choice in handoff context as `interactive` or `delegated`.

Inspect `gobby-workflows:get_agent_definition(name="epic-reviewer")` and
`get_pipeline(name="review")` before relying on installed availability or defaults.
Templates describe bundled intent; installed rows and effective project overrides
determine operation. Do not infer active rules from template YAML.

## Run

- Interactive: use `gobby-agents:apply_persona(agent="epic-reviewer")`, load the
  review methodology and perform the assessment in this session.
- Delegated: use `gobby-workflows:run_pipeline(name="review", inputs={"task_id":
  "<epic-ref>", "mode": "spawned"})`. Report the execution ID and returned agent
  run IDs. The reviewer owns the verdict; the launcher does not issue a second one.

The bundled pipeline defaults provider to `codex`, leaves model and worktree ID
empty, and passes `allow_closed_task=true`. Its `mode` input does not implement an
interactive branch: interactive mode uses `apply_persona` directly. Inspect actual
agent/provider/model resolution before claiming what will run. For monitoring,
cancellation and event-driven completion, use [pipelines](../pipelines/overview.md)
and [agents](../agents/overview.md). A completed spawn step is not a review verdict.

Read the full descendant tree and review in order: spec compliance, code quality,
testing, proportionality. Missing required behavior blocks approval before quality
assessment. Keep the review's evidence trail together; the epic reviewer reviews
alone rather than spawning additional reviewers.

## Holistic epic review

Any seat reviewing an epic target, including a DevTeam reviewer's final epic
review, reviews the epic as a whole rather than re-reviewing child diffs.

1. Read the epic and every descendant with `get_task(brief=false)`.
2. For each child, read its attestations and check every attestation the selected
   policy requires (TDD, implementation, review, validation and any security
   approval): present, approved, made by the authorized seat for the exact
   candidate, and not superseded by later evidence.
3. For each child, check its validation criteria against the integrated epic
   branch, not the child's own candidate. Cite each criterion with how it is met.
4. Judge the epic against the epic's own validation criteria and its parent plan:
   gaps between children, integration seams, and anything no single child owned.

A missing or invalid attestation or an unmet criterion blocks the epic. Record
the per-child and whole-epic findings in the review evidence with the
authenticated subtree revision.

## State and recovery

An open epic review must respect its owner. If another session owns the work, coordinate through
`gobby-agents:send_message` instead of taking its claim.

Open and closed epics share one review path. Skip claiming a closed epic. Deliver
the structured findings; blocking findings require remediation tasks under the
epic or an explicit reopening of a closed epic. Never call `close_task` from epic
review. Closure and delivery belong to the owning lifecycle flow.

If the pipeline is absent/disabled or spawn fails, inspect its returned error and
installed definition, then use the pipeline/agent recovery guidance. Do not retry
by changing review state or inventing a successful verdict.

_Version: 1.1.0_

_Last verified: 2026-11-03_
