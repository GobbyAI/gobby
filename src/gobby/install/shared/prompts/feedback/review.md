---
description: Cluster session feedback and propose deduplicated follow-up tasks
required_variables:
  - observations
  - max_tasks
---
You are reviewing structured feedback that coding agents recorded about the Gobby
platform (its daemon, rules, MCP tools, and harness) while working. Cluster the
observations, classify each cluster, and propose follow-up tasks only where the
evidence supports one. You propose; deterministic code files the tasks — never
assume a proposal is accepted.

## Observations

Each observation has `id`, `kind` (friction | bug | noise | surprise |
missing-affordance | useful | other, with `kind_other_label` naming an unlisted
kind), `evidence`, `impact`, `frequency` (once | repeated | always), and optional
`suggestion` and `disposition` (worked-around | filed-task | fixed | escalated |
noted).

```json
{{ observations }}
```

## Instructions

1. Cluster observations that describe the same underlying behavior, tool, or
   workflow. Singleton clusters are fine. Every observation id must appear in
   exactly one cluster.
2. Classify each cluster:
   - `defect`: something is broken or misbehaving (most `bug` and reproducible
     `friction` clusters).
   - `guidance-gap`: the platform behaved as built but agents were misled or
     under-informed — docs, rule text, schema descriptions, missing affordances.
   - `noise`: one-off, stale, or unactionable observations.
   - `praise`: `useful` observations worth keeping visible; never a task.
3. Propose a task (`proposed_task`) only for `defect` and `guidance-gap` clusters
   that are actionable now. Respect dispositions: a cluster whose observations are
   already `fixed` or `filed-task` gets `proposed_task: null` — mention the
   existing resolution (and any task refs like `#12345` found in the text) in
   `digest_note` instead. Propose at most {{ max_tasks }} tasks; prioritize by
   frequency and impact.
4. Task titles must be imperative, specific, and self-contained (they are
   deduplicated against open tasks by exact title). Descriptions must carry the
   evidence: what happened, where, how often, and the suggested direction if the
   observations include one. Priority: 1 for recurring defects that block work,
   2 for the rest, 3 for minor polish.
5. Write a one-or-two-sentence `digest_note` per cluster for the human digest:
   what the cluster says and what, if anything, was proposed.

Return JSON matching the provided schema. Do not invent observation ids, task
refs, or behavior beyond the evidence.
