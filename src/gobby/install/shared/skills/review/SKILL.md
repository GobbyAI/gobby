---
name: review
description: "Interactive /gobby review launcher. Dispatches epic-reviewer for an epic."
version: "1.0.0"
category: core
triggers: review, gobby review, epic review
metadata:
  gobby:
    audience: interactive
    depth: 0
---

# /gobby review

Use this skill when the user invokes `/gobby review` to run the same epic
epic reviewer that lifecycle dispatch uses, independent of `gobby build`.

The workflow definition is `src/gobby/install/shared/workflows/review.yaml`.

## Inputs

Resolve an epic task ref before dispatch. Accept `#N`, a numeric ref, or a
dotted task path. If the user does not provide one, ask for the task ref.

## Mode Selection

Ask for the I/D mode unless it is already clear from the user's request:

```text
I) Interactive - apply the epic-reviewer persona to this session and review in-line.
D) Delegated - dispatch a separate epic-reviewer agent and surface the run ids.
```

Persist the choice as `value="interactive" | "delegated"` for the handoff.
Interactive maps to in-line mode below; delegated maps to the spawned workflow.
Both modes use the `epic-reviewer` methodology. Spawned mode works for open
and already-closed epics — the workflow passes `allow_closed_task` so the
reviewer can run post-hoc; on a closed epic the reviewer makes no stage
transitions and instead delivers findings plus remediation tasks (or reopens
the epic).

### In-line mode

Apply the persona, then run the review yourself in this session:

```python
call_tool("gobby-agents", "apply_persona", {"agent": "epic-reviewer"})
```

Follow the loaded epic-reviewer methodology against the target epic. For a
closed epic, skip claiming and stage transitions; deliver the
`## Epic Findings` verdict and file remediation tasks for blocking findings.

## Verdict Contract

The epic review loop checks the approved plan, aggregate implementation
diff, validation evidence, and child task outcomes. The reviewer emits exactly
one verdict:

- approve with `approve_review(stage_name="epic_qa")`
- reject with `reject_review(stage_name="epic_qa")`
- escalate with `escalate_task`

Use the explicit user-facing summary `approve / reject / escalate` when
describing possible outcomes.

## Dispatch (Spawned mode)

Run the `review` workflow with the resolved epic task id:

```python
call_tool("gobby-workflows", "run_pipeline", {
    "name": "review",
    "inputs": {
        "task_id": "<epic_task_ref>",
        "mode": "spawned"
    }
})
```

Surface the returned execution id. The spawned `epic-reviewer` owns the
review verdict and terminates through its agent workflow.

## Boundaries

- Do not approve, reject, or escalate directly from this skill.
- Do not call `close_task`; lifecycle dispatch owns closure.
- Do not replace the `epic-reviewer` or `epic-review` methodology here.
