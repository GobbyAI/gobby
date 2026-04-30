---
name: review
description: "Interactive /gobby review launcher. Dispatches holistic-reviewer for an epic."
version: "1.0.0"
category: core
triggers: review, gobby review, holistic review, epic review
metadata:
  gobby:
    audience: interactive
    depth: 0
---

# /gobby review

Use this skill when the user invokes `/gobby review` to run the same holistic
epic reviewer that lifecycle dispatch uses, independent of `gobby build`.

The workflow definition is `src/gobby/install/shared/workflows/review.yaml`.

## Inputs

Resolve an epic task ref before dispatch. Accept `#N`, a numeric ref, or a
dotted task path. If the user does not provide one, ask for the task ref.

## Mode Selection

Ask for the I/D mode unless it is already clear from the user's request:

```text
I) Interactive - dispatch holistic-reviewer and surface the run ids.
D) Delegated - dispatch holistic-reviewer and end the turn.
```

Persist the choice as `value="interactive" | "delegated"` for the handoff.
Both modes use the `holistic-reviewer` agent and the shared workflow YAML.

## Verdict Contract

The holistic review loop checks the approved plan, aggregate implementation
diff, validation evidence, and child task outcomes. The reviewer emits exactly
one verdict:

- approve with `mark_task_review_approved`
- reject with `mark_task_review_rejected`
- escalate with `escalate_task`

Use the explicit user-facing summary `approve / reject / escalate` when
describing possible outcomes.

## Dispatch

Run the `review` workflow with the resolved epic task id:

```python
call_tool("gobby-workflows", "run_pipeline", {
    "name": "review",
    "inputs": {
        "task_id": "<epic_task_ref>",
        "mode": "<interactive|delegated>"
    }
})
```

Surface the returned execution id. The spawned `holistic-reviewer` owns the
review verdict and terminates through its agent workflow.

## Boundaries

- Do not approve, reject, or escalate directly from this skill.
- Do not call `close_task`; lifecycle dispatch owns closure.
- Do not replace the `holistic-reviewer` or `holistic-review` methodology here.
