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

## Profile Selection

Ask for a profile unless the user already supplied one.

Available profiles:

| Profile | Use |
|---------|-----|
| `quick` | Single automated leaf. Skips review-heavy stages and uses no isolation. |
| `review` | Plan-file launch with planning review retained but PR skipped. |
| `full` | Full lifecycle automation in a worktree. |
| `full-yolo` | Full lifecycle except PR, with yolo enabled. |
| `manual` | Prompt for every build option. |

If the input is a plan file, do not allow `quick`; choose `review` by default.
If the input is a leaf task ref, choose `quick` by default.
If the input is an epic task ref, choose `full` by default.

## Manual Options

When profile is `manual`, collect:

1. Skip stages. Allow any combination of `plan_review`, `test_arch`, `expanding`, `qa`, `holistic_review`, and `pr`.
2. Isolation. Offer `none`, `worktree`, and `clone`; for a single leaf, force `none`.
3. Yolo. Ask yes/no and map to `--yolo` or `--no-yolo`.
4. Max review rounds. Ask only when `plan_review` is not skipped.
5. Target branch. Optional.
6. Agent. Optional; pass through as `--agent`.

For non-manual profiles, only ask for optional target branch and agent if the user wants overrides.

## Confirmation

Before running, show the equivalent command:

```text
gobby build <input> --profile <profile> --skip-stage <stage,...> --isolation <mode> --yolo --max-review-rounds <n> --target-branch <branch> --agent <agent>
```

Omit flags that are unset. For manual yolo=false, show `--no-yolo`.

Ask for confirmation. On approval, run the command exactly as shown. The CLI calls the shared build service, which records artifacts, lifecycle events, cascade state, and dispatcher kick metadata.

## Result

Report the returned task id, whether the task was created, the initial lifecycle, skipped stages, and dispatcher tick count. If the shared build service returns a validation error, show the error and do not retry with changed options unless the user explicitly approves the change.
