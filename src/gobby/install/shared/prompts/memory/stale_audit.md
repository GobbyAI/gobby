---
description: Classify stale memory candidates for safe cleanup
required_variables:
  - candidates
  - max_stale_age_days
  - max_stale_access_count
  - stale_confidence_threshold
---
You are auditing persistent assistant memories for cleanup. Classify each candidate as `delete`, `keep`, or `review`.

## Candidate Selection Context

The cleanup job already limited this set to memories whose last activity is older than {{ max_stale_age_days }} days and whose access count is at most {{ max_stale_access_count }}.

## Verdicts

- `delete`: The memory is obsolete, ephemeral, low-value, code-derivable, or only useful for a finished one-time task.
- `keep`: The memory is still useful across sessions: durable user preferences, stable project conventions, architectural decisions, recurring workflows, or hard-to-rediscover facts.
- `review`: The memory might be stale, but deletion is not safe without human judgment.

## Rules

1. Prefer `keep` for user preferences, project conventions, security constraints, task workflow requirements, and design decisions.
2. Prefer `review` when the memory mentions an unresolved bug, task, migration, incident, or specific user instruction.
3. Use `delete` only when the reason is clear from the candidate itself.
4. A `delete` verdict needs confidence at least {{ stale_confidence_threshold }} to be acted on.
5. Return exactly one classification for every candidate ID.

## Candidates

{{ candidates }}

## Output Format

Return strict JSON only with this shape:

{
  "memories": [
    {
      "id": "memory-id",
      "verdict": "delete",
      "confidence": 0.91,
      "reason": "Superseded one-time task note with no durable project value."
    }
  ]
}
