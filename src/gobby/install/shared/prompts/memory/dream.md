---
description: Build a safe memory dream plan
required_variables:
  - candidates
  - duplicate_groups
  - min_action_confidence
  - min_delete_confidence
---
You are maintaining persistent assistant memories. Build one plan that improves stale memories while preserving durable facts.

## Actions

- `keep`: the memory is still useful.
- `delete`: the memory is obsolete, junk, one-time task residue, or code-derivable. Use only with confidence at least {{ min_delete_confidence }}.
- `refresh`: replace outdated content with a better current statement.
- `merge`: combine duplicate memories into one canonical memory.
- `supersede`: replace an outdated fact with a newer fact, or point it at an existing newer memory.
- `review`: mutation is not safe without human judgment.

## Rules

1. Return exactly one action for every candidate ID unless it appears in a `merge`.
2. Preserve user preferences, project conventions, security constraints, workflow rules, and architectural decisions.
3. Use `review` for unresolved bugs, open migrations, active tasks, or ambiguous facts.
4. Every mutating action needs confidence at least {{ min_action_confidence }}; `delete` needs at least {{ min_delete_confidence }}.
5. `refresh`, `merge`, and content-based `supersede` must include replacement content.
6. Use only candidate IDs shown below.

## Candidates

{{ candidates }}

## Exact Duplicate Groups

{{ duplicate_groups }}

## Output

Return strict JSON only:

{
  "actions": [
    {
      "action": "review",
      "memory_id": "memory-id",
      "confidence": 0.6,
      "reason": "Needs human judgment."
    }
  ]
}
