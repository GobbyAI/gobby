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
3. `--stage <stage>[:key=value[,key=value...]]` for stage selection or caps.
4. `--isolation none|worktree|clone`.
5. `--no-merge`, only with `worktree` or `clone` isolation.
6. `--pr <number-or-url>`, optional.
7. `--target-branch <branch>`, optional.
8. `--agent <agent-name>`, optional.
9. `--reset-expansion-output`, only when rebuilding a task ref with existing expansion output.

## Confirmation

Before running, show the equivalent command:

```text
gobby build <input> --quick --skip-stage <stage,...> --stage <stage>:max_review_rounds=<n> --isolation <mode> --no-merge --target-branch <branch> --agent <agent>
```

Omit flags that are unset.

Ask for confirmation. On approval, run the command exactly as shown. The CLI calls the shared build service, which records artifacts, lifecycle events, cascade state, and dispatcher kick metadata.

## Result

Report the returned task id, whether the task was created, the initial lifecycle, skipped stages, and dispatcher tick count. If the shared build service returns a validation error, show the error and do not retry with changed options unless the user explicitly approves the change.
