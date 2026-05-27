---
name: build
description: "Interactive /gobby build wizard. Starts lifecycle automation from a plan file or task ref, or delegates raw ideas to /gobby plan."
version: "1.0.0"
category: core
triggers: build, gobby build, lifecycle automation
metadata:
  gobby:
    audience: interactive
    depth: 0
---

# /gobby build - Interactive Build Launcher

Use this skill when the user invokes `/gobby build` without a complete CLI command. The wizard gathers enough information to call the shared build service through the `gobby build` command.

The build surface accepts either:

- A plan file path.
- A task ref such as `#12805`.
- A raw idea, which must be sent to `/gobby plan` first.

Do not implement build behavior in the skill. The shared build service is the source of truth; this skill only collects options, confirms them, and runs the equivalent `gobby build ...` invocation.

## Input Classification

Ask for the build input if it was not supplied.

Classify in this order:

1. Existing markdown path -> plan file.
2. `#N`, numeric ref, or dotted task path -> task ref.
3. Anything else -> idea.

For an idea, stop and delegate:

```text
This needs a plan first. Run /gobby plan for the idea, approve the generated plan, then return to /gobby build with the plan file or task ref.
```

## Manual Options

Collect only options the user explicitly wants to change:

1. `--quick` for exactly one lifecycle step.
2. `--skip-stage <stage[,stage...]>`, only when starting a new lifecycle.
3. `--stage <stage>[:key=value[,key=value...]]` for stage cap/settings overrides.
4. `--clone` to use clone workspaces instead of the default worktree backend.
5. `--no-merge` to skip final promotion for the requested build.
6. `--pr <number-or-url>`, optional.
7. `--target-branch <branch>`, optional.
8. `--agent <agent-name>`, optional.
9. `--reset-expansion-output`, only when rebuilding a task ref with existing expansion output.
10. `--max-active-agents <n>`, optional bounded concurrency for dispatcher ticks.

## Interactive E2E Validation

For an unattended build-flow test, do not use the same task as both the tracking work and the automation target.

- Keep a separate claimed coordinator/tracking epic for the active session; use it for blocker fixes and to prevent stopping mid-run.
- Create a separate build or document epic as the `gobby build #epic` target. Fix and merge blockers before starting the final automation epic.
- Before the final launch, verify project build automation is enabled. If it is paused from prior `gobby build stop` or restart work, run `gobby build resume` once to restore daemon-owned dispatch before judging the E2E.
- Use `--quick` only for smoke checks of one lifecycle step. For real end-to-end validation, run without `--quick`, usually with bounded concurrency such as `--max-active-agents <n>`.
- For final unattended E2E, launch `gobby build #epic ...` once and then observe daemon-owned automation through task state, agent runs, logs, and final acceptance checks.
- Do not repeatedly rerun `gobby build` as manual dispatcher ticks to keep work moving. That is an anti-pattern because it changes the test shape and can hide a broken dispatcher loop.
- Be explicit about docs scope. For `docs/guides` refreshes, leave root `README.md` out unless it is named in scope; run `docs/guides/README.md` last when it indexes the other guides.
- Do not make the test pass by changing the required agent, provider, lifecycle route, task scope, or acceptance criteria. Diagnose and fix the blocker while preserving the requested path.
- Escalating because an assigned agent is incapable of the task is an extreme edge case. Use it only after exhausting practical fixes and documenting why the specified agent path cannot complete the work.
- Final acceptance must verify that the document epic is closed, the merge stage records the real merge SHA, no agents are running, no tasks remain claimed for the build, no stale build worktrees or clones remain, and any intentionally preserved dirty or conflicted workspace is explained.

## Automation Debugging Pattern

When build automation stalls or agents behave as if required startup context is missing, debug the lifecycle rather than changing requirements.

- Compare against the last known successful run before changing configuration. Check what changed in agent definitions, provider routing, rules, hooks, MCP startup config, and step workflows.
- Treat provider-specific symptoms as evidence, not proof of provider incapability. Preserve the requested agent/provider path while diagnosing.
- If progress appears stuck after the initial launch, first prove daemon-owned dispatch is idle or wedged by checking task stages, active agent runs, automation loop health, and correlated logs. If project automation is paused, use `gobby build resume`; a bounded explicit tick may be used only as a diagnostic or recovery step after recording evidence, not as normal E2E execution.
- Check whether SessionStart activation completed and left expected state before the first provider-neutral prompt event: session linkage, `_agent_type`, active rule and skill variables, terminal pickup metadata, baseline dirty-file capture, and any spawned-agent step workflow.
- A first-prompt reconciliation guard can be useful as a backstop: call an idempotent `ensure_session_activation(session_id)` helper that creates only missing activation effects and preserves existing progress. Do not replay the raw SessionStart hook wholesale.
- For cross-boundary stalls, use OpenTelemetry or equivalent correlated logs keyed by `agent_run_id` and `session_id` across spawn, tmux, SessionStart, workflow activation, auto-claim, step transitions, MCP calls, and rule blocks.

## Confirmation

Before running, show the equivalent command:

```text
gobby build <input> --quick --skip-stage <stage,...> --stage <stage>:max_review_rounds=<n> --clone --no-merge --target-branch <branch> --agent <agent> --max-active-agents <n>
```

Omit flags that are unset.

Ask for confirmation. On approval, run the command exactly as shown. The CLI calls the shared build service, which records artifacts, lifecycle events, cascade state, and dispatcher kick metadata.

## Result

Report the returned task id, whether the task was created, the initial lifecycle, skipped stages, and dispatcher tick count. If the shared build service returns a validation error, show the error and do not retry with changed options unless the user explicitly approves the change.
