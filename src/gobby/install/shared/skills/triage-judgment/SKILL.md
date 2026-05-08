---
name: triage-judgment
description: Internal methodology for side-effect-free GitHub issue triage verdicts.
version: "1.0.0"
category: methodology
internal: true
triggers: github issue triage, triage judgment, duplicate issue detection
metadata:
  gobby:
    audience: agent
    depth: 0
---

# GitHub Issue Triage Judgment

Return structured JSON only. Do not create tasks, call GitHub, change labels, close
issues, or start builds. Python services own all side effects.

## Input

You receive a GitHub issue snapshot and any project-scoped duplicate candidates from
Qdrant.

## Output

Return exactly one JSON object:

```json
{
  "verdict": "implement",
  "reason": "Clear actionable product bug",
  "comment": "Optional concise public GitHub comment",
  "labels": ["optional:gobby-label"],
  "close_issue": false,
  "duplicate": null
}
```

`verdict` must be one of:

- `implement`: issue is actionable and should become a Gobby task.
- `skip`: issue should not become work for this project.
- `escalate`: issue needs human product or security review.
- `dedup`: issue duplicates a candidate already returned in the input.

For `dedup`, set `duplicate` to an object with `repo`, `issue_number`, and `score`.
For `skip` or `dedup`, set `close_issue` only when the reason is safe to state
publicly.

## Labels

Use only these Gobby-managed labels unless the caller asks for additional project
labels:

- `gobby:accepted`
- `gobby:skipped`
- `gobby:duplicate`
- `gobby:needs-triage`
- `gobby:resolved`
