---
description: Build a safe memory dream plan
required_variables:
  - candidates
  - truth_digest
  - min_action_confidence
  - min_delete_confidence
  - min_rescope_confidence
---
You are maintaining persistent assistant memories. Review each candidate against what is true *now* and return one plan that hides obsolete memories, repairs stale wording, and promotes genuinely universal repo memories to global scope.

## Actions

- `keep`: the memory is still true and useful now.
- `delete`: the memory is obsolete, junk, one-time task residue, or code-derivable. Use only with confidence at least {{ min_delete_confidence }}.
- `refresh`: the memory is worth keeping but its wording is outdated — supply better current content.
- `review`: the memory is genuinely ambiguous and a human should look before it is hidden.
- `promote`: the memory is repo-scoped but clearly universal across projects and should become global. Use only with confidence at least {{ min_rescope_confidence }}.

`delete` and `review` hide the memory from agents (recoverable). `keep`, `refresh`, and `promote` leave it visible. `promote` only changes scope to global; it must not rewrite content. There is no merge, demotion, or consolidation in this sweep.

## Current truth

Judge each memory against these canonical current facts. A memory that contradicts them — superseded infrastructure (e.g. Neo4j -> FalkorDB, SQLite -> PostgreSQL), removed or renamed APIs, retired config keys, stale daemon paths, or completed one-time residue — is a strong `delete` or `refresh` signal.

{{ truth_digest }}

## Rules

1. Return exactly one action for every candidate ID.
2. Preserve user preferences, project conventions, security constraints, workflow rules, and architectural decisions.
3. Use `review` for unresolved bugs, open migrations, active tasks, or facts you genuinely cannot resolve against current truth.
4. Treat `source_type` as a prior, not a verdict: user-authored memories deserve extra caution, but agent-authored memories can still be durable when current and broadly useful.
5. Every mutating action needs confidence at least {{ min_action_confidence }}; `delete` needs at least {{ min_delete_confidence }}; `promote` needs at least {{ min_rescope_confidence }}. Low-confidence judgments should be `keep`.
6. Use `promote` only when the memory is independent of this repository's code, paths, tasks, local daemon state, project-specific workflows, and transient implementation details.
7. `promote` is appropriate for broadly reusable engineering preferences, durable cross-project operating conventions, and universal lessons that are not tied to Gobby.
8. `refresh` must include replacement `content`.
9. Use only candidate IDs shown below.
10. Cover every rendered candidate exactly once. Do not omit candidates because they look low-value; return `keep` when no safer action applies.

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
