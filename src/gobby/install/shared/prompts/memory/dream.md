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

- `keep`: the memory reflects the current repo, or carries durable value generic to the repo, and is useful to agents as-is.
- `delete`: the memory is obsolete residue — it neither reflects the current repo nor carries durable generic value — or it is junk, one-time task/status residue, or trivially code-derivable. Use only with confidence at least {{ min_delete_confidence }}, and only when you can name a concrete obsolescence signal (see Decision procedure).
- `refresh`: the memory is worth keeping but its wording is outdated — supply better current content.
- `review`: you genuinely cannot resolve the memory's current-ness or value, or it describes an unresolved issue that is plausibly still active. For real ambiguity only — never a soft `delete` for residue you are confident about.
- `promote`: the memory is repo-scoped but clearly universal across projects and should become global. Use only with confidence at least {{ min_rescope_confidence }}.

`delete` and `review` both hide the memory from agents (recoverable); the only difference is whether a human is expected to triage it — so never route confidently-obsolete memories to `review`. `keep`, `refresh`, and `promote` leave it visible. `promote` only changes scope to global; it must not rewrite content. There is no merge, demotion, or consolidation in this sweep.

## Decision procedure

Apply this to every candidate:

1. Does the memory reflect the current state of the repo?
   - Yes → Is it useful to agents?
     - Yes → `keep` (use `refresh` if only the wording is stale; `promote` if it is universal across projects).
     - No → `delete` if it is confidently noise; `keep` if it is harmless and you are unsure.
   - No → Does it provide durable value generic to the repo — general rules, patterns, practices, user preferences, architectural or security decisions?
     - Yes → `keep` (use `refresh` for stale wording; `promote` if universal across projects).
     - No → `delete`. A memory that is neither current nor durably useful is obsolete residue.

`review` is the escape hatch, not a branch of this tree: choose it only when you genuinely cannot answer the current-ness or value questions, or when the memory is an unresolved issue that is plausibly still active. A malformed, unknown, or below-threshold verdict degrades to a visible `keep`, never to `review` — so reserve `review` for a deliberate "a human should decide."

A `delete` requires a concrete, citable obsolescence signal in your `reason` — for example: it contradicts the current truth below (removed or renamed API, superseded infrastructure, retired config key, stale daemon path); a newer related memory records a decision or state change that contradicts or supersedes this memory; it references a task, epic, branch, or migration that is completed or closed; or it bakes in a hard date that marks it as time-bound state (a test-run snapshot, an in-progress investigation, a status or progress report). High `age_days` with `access_count` at or near zero may corroborate a `delete`; a citable `delete` does not require low `access_count` when a contradiction signal exists. Absent such a signal, do not assign high `delete` confidence.

Judge each candidate against its `rationale`. `rationale` is the writer's own claim about why the memory deserved to persist; `source_task_id` and `created_by_agent` say which task and agent produced it. A rationale that names a one-time event — a specific review run, test run, task, or dated status — is a concrete, citable time-bound-state signal once that event is over; `source_task_id` referring to a completed or closed task corroborates it. A `delete` or `refresh` on a candidate with a non-null rationale must quote or paraphrase that rationale in its `reason` and say why the claim no longer holds. A `NULL` rationale is a legacy row, not evidence — absence of a rationale may corroborate other signals but never justifies `delete` on its own.

## Current truth

Judge each memory against the current-truth digest below. When the digest says `authoritative - complete current set`, its infrastructure stack is the complete current stack for this scope: infrastructure that contradicts the stack, or is absent from it, is a concrete stale signal. For example, under an authoritative Gobby platform digest, Neo4j contradicted by FalkorDB or MySQL contradicted by PostgreSQL is a strong `delete` or `refresh` signal.

When the digest says `partial - do NOT infer staleness from absence`, treat listed facts as positive evidence only. Do not delete or refresh solely because infrastructure, APIs, config keys, or paths are absent from a partial or empty digest.

Completed one-time residue, stale daemon paths, retired config keys, and removed or renamed APIs are still concrete obsolescence signals when you can cite current evidence from the digest or candidate metadata.

{{ truth_digest }}

## Related newer memories

Candidates may carry `related_newer_memories`: newer memories from the same scope that share distinctive terms or meaning. A newer memory, especially one with `memory_type: decision`, that contradicts or supersedes the candidate is a concrete, citable obsolescence signal. For that signal, cite its `id` in `reason` and choose `delete` when the candidate has no residual value, or `refresh` to rewrite the candidate as current truth while referencing the newer memory.

Absent or empty `related_newer_memories` is not evidence of currentness.

## Rules

1. Return exactly one action for every candidate ID.
2. Preserve durable knowledge regardless of age: user preferences, project conventions, security constraints, workflow rules, reusable patterns and practices, and architectural decisions. These are `keep` (or `refresh`/`promote`) even when they predate the current repo state.
3. Do not route stale residue to `review`. Assign `delete` only when the reason cites a concrete contradiction, supersession, completed task/epic or migration, retired branch or API, or explicit time-bound-state signal. High `age_days`, no recent access, and hard dates corroborate that signal; none establishes obsolescence alone. Use `review` only for an unresolved issue that is plausibly still active, or a memory whose current-ness or value you genuinely cannot determine.
4. Treat `source_type` as a prior, not a verdict: user-authored memories deserve extra caution, but agent-authored memories can still be durable when current and broadly useful.
5. Every mutating action needs confidence at least {{ min_action_confidence }}; `delete` needs at least {{ min_delete_confidence }}; `promote` needs at least {{ min_rescope_confidence }}. Low-confidence judgments should be `keep`.
6. Use `promote` only when the memory is independent of this repository's code, paths, tasks, local daemon state, project-specific workflows, and transient implementation details.
7. `promote` is appropriate for broadly reusable engineering preferences, durable cross-project operating conventions, and universal lessons that are not tied to Gobby. User-authored, repo-independent preferences are prime promote candidates. The `source_type` label is a prior for consideration, not a deciding rule.
8. `refresh` must include replacement `content`.
9. Use only candidate IDs shown below.
10. Cover every rendered candidate exactly once. Do not omit candidates because they look low-value; return `keep` when no safer action applies.
11. High `access_count` is never evidence of correctness: recall frequency measures retrieval, not truth, and a wrong-but-popular memory self-reinforces. Only `access_count` at or near zero may corroborate a `delete`; never use high `access_count` to justify `keep` against a concrete obsolescence signal.

## Candidates

Each candidate includes `rationale` (the writer's own claim about why the memory deserved to persist) plus `source_task_id` and `created_by_agent` (which task and agent produced it). Legacy rows carry `"rationale": null`.

{{ candidates }}

## Output

Return strict JSON only:

{
  "actions": [
    {
      "action": "delete",
      "memory_id": "memory-id",
      "confidence": 0.9,
      "reason": "Progress report for epic #8803, which is closed; age_days 120, access_count 0."
    }
  ]
}
