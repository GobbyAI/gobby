# Review Flows

Load this reference for autonomous or pipeline review transitions. Review tools
live on `gobby-tasks-ops` and require an explicit stage such as `planning`,
`expansion`, `development`, or `epic_qa`.

Autonomous agents must submit work for review:

```
call_tool("gobby-tasks-ops", "submit_for_review", {
    "task_id": "#42",
    "stage_name": "development",
    "review_notes": "Implemented retrieval exemptions; verify focused tests."
}, session_id="#2333")
```

QA approves a reviewed stage:

```
call_tool("gobby-tasks-ops", "approve_review", {
    "task_id": "#42",
    "stage_name": "development",
    "approval_notes": "Verified focused tests and acceptance criteria."
}, session_id="#2333")
```

Reject a stage back to ready:

```
call_tool("gobby-tasks-ops", "reject_review", {
    "task_id": "#42",
    "stage_name": "development",
    "rejection_notes": "Oversized get_skill still offloads after compaction.",
    "round_number": 2
}, session_id="#2333")
```

All completion gates remain active. Commits from the submitting or reviewing
session are linked by the review transition. Review tools are reserved for
autonomous/reviewer sessions; interactive sessions close directly.
