---
name: dev
description: "Interactive /gobby dev launcher. Dispatches the shared developer agent for a task."
version: "1.0.0"
category: core
triggers: dev, gobby dev, developer agent, implement task
metadata:
  gobby:
    audience: interactive
    depth: 0
---

# /gobby dev

Use this skill when the user invokes `/gobby dev` to run the same developer
agent that lifecycle dispatch uses, independent of `gobby build`.

The workflow definition is `src/gobby/install/shared/workflows/dev.yaml`.

## Inputs

Resolve a task ref before dispatch. Accept `#N`, a numeric ref, or a dotted
task path. If the user does not provide one, ask for the task ref.

## Mode Selection

Ask for the I/D mode unless it is already clear from the user's request:

```text
I) Interactive - dispatch the developer agent and surface the run ids.
D) Delegated - dispatch the developer agent and end the turn.
```

Persist the choice as `value="interactive" | "delegated"` for the handoff.
Both modes use the `developer` agent and the shared workflow YAML; the only
difference is whether the current session stays available for follow-up.

## Dispatch

Run the `dev` workflow with the resolved task id:

```python
call_tool("gobby-workflows", "run_pipeline", {
    "name": "dev",
    "inputs": {
        "task_id": "<task_ref>",
        "mode": "<interactive|delegated>"
    }
})
```

Surface the returned execution id. The spawned `developer` agent owns the
implementation loop: claim the task, implement, validate, commit, and either
close or hand off for QA according to its agent contract.

## Boundaries

- Do not implement the task directly inside this skill.
- Do not run `gobby build`; this is the per-task developer loop.
- Do not replace the `developer` agent instructions here.
