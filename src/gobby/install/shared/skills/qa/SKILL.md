---
name: qa
description: "Interactive /gobby qa launcher. Dispatches the shared qa-reviewer agent for a task."
version: "1.0.0"
category: core
triggers: qa, gobby qa, qa reviewer, review task
metadata:
  gobby:
    audience: interactive
    depth: 0
---

# /gobby qa

Use this skill when the user invokes `/gobby qa` to run the same read-only
QA reviewer that lifecycle dispatch uses, independent of `gobby build`.

The workflow definition is `src/gobby/install/shared/workflows/qa.yaml`.

## Inputs

Resolve a task ref before dispatch. Accept `#N`, a numeric ref, or a dotted
task path. If the user does not provide one, ask for the task ref.

## Mode Selection

Ask for the I/D mode unless it is already clear from the user's request:

```text
I) Interactive - dispatch qa-reviewer and surface the run ids.
D) Delegated - dispatch qa-reviewer and end the turn.
```

Persist the choice as `value="interactive" | "delegated"` for the handoff.
Both modes use the `qa-reviewer` agent and the shared workflow YAML.

## Verdict Contract

The QA loop is read-only. The `qa-reviewer` checks the diff, runs focused
validation, and emits exactly one verdict:

- approve with `approve_review(stage_name="development")`
- reject with `reject_review(stage_name="development")`
- escalate with `escalate_task`

For tasks marked `tdd:required` or requesting `test-driven-development`, the
reviewer must reject unless red, green, refactor/final-green, exact test command,
and test-quality audit evidence are present.

Use the explicit user-facing summary `approve / reject / escalate` when
describing possible outcomes.

## Dispatch

Run the `qa` workflow with the resolved task id:

```python
call_tool("gobby-workflows", "run_pipeline", {
    "name": "qa",
    "inputs": {
        "task_id": "<task_ref>",
        "mode": "<interactive|delegated>"
    }
})
```

Surface the returned execution id. The spawned `qa-reviewer` owns the verdict;
this skill does not edit files, commit, approve, reject, or escalate itself.

## Boundaries

- Do not edit files from this skill.
- Do not call `close_task`; the lifecycle dispatcher owns closure.
- Do not replace the `qa-reviewer` agent instructions here.
