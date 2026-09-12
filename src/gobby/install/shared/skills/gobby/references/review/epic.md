# Epic review

Load before launching or performing an epic implementation review. Also load
[evidence](evidence.md) before assessment and [outcomes](outcomes.md) before a
verdict. Load standalone `proportionality` and the applicable memory review
learning guidance; preserve the installed epic-reviewer methodology.

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

## State and recovery

An open epic review must respect its owner and actual stage; applying a persona
does not initialize an `epic_qa` manifest or start a stage. Discover current stages
before attempting a verdict. If another session owns the work, coordinate through
`gobby-agents:send_message` instead of taking its claim.

For an already-closed epic, skip claiming and stage transitions. Deliver the
structured findings; blocking findings require actionable remediation work or an
explicit reopening through the task workflow. Never call `close_task` from epic
review. Closure and delivery belong to the owning lifecycle flow.

If the pipeline is absent/disabled or spawn fails, inspect its returned error and
installed definition, then use the pipeline/agent recovery guidance. Do not retry
by changing review state or inventing a successful verdict.
