---
description: Build a safe memory dream plan
required_variables:
  - candidates
  - truth_digest
  - min_action_confidence
  - min_delete_confidence
---
You are maintaining persistent assistant memories. Review each candidate against what is true *now* and return one plan that hides obsolete memories while preserving durable facts.

## Actions

- `keep`: the memory is still true and useful now.
- `delete`: the memory is obsolete, junk, one-time task residue, or code-derivable. Use only with confidence at least {{ min_delete_confidence }}.
- `refresh`: the memory is worth keeping but its wording is outdated — supply better current content.
- `review`: the memory is genuinely ambiguous and a human should look before it is hidden.

`delete` and `review` hide the memory from agents (recoverable). `keep` and `refresh` leave it visible. There is no merge or consolidation in this sweep.

## Current truth

Judge each memory against these canonical current facts. A memory that contradicts them — superseded infrastructure (e.g. Neo4j -> FalkorDB, SQLite -> PostgreSQL), removed or renamed APIs, retired config keys, stale daemon paths, or completed one-time residue — is a strong `delete` or `refresh` signal.

{{ truth_digest }}

## Rules

1. Return exactly one action for every candidate ID.
2. Preserve user preferences, project conventions, security constraints, workflow rules, and architectural decisions.
3. Use `review` for unresolved bugs, open migrations, active tasks, or facts you genuinely cannot resolve against current truth.
4. Every mutating action needs confidence at least {{ min_action_confidence }}; `delete` needs at least {{ min_delete_confidence }}. Low-confidence judgments should be `keep`.
5. `refresh` must include replacement `content`.
6. Use only candidate IDs shown below.

## Candidates

{{ candidates }}

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
