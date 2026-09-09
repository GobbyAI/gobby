---
description: Cluster session feedback and propose deduplicated follow-up tasks
version: "4.0"
required_variables:
  - run_id
  - max_tasks
---
You are reviewing structured feedback that coding agents recorded about the Gobby
platform (its daemon, rules, MCP tools, and harness) while working. Cluster the
observations, classify each cluster, and propose follow-up tasks only where the
evidence supports one. You propose; deterministic code files the tasks — never
assume a proposal is accepted.

## Observations

Each observation has `id`, `session_id`, `source`, `kind` (friction | bug | noise | surprise |
missing-affordance | useful | other, with `kind_other_label` naming an unlisted
kind), `evidence`, `impact`, `frequency` (once | repeated | always), and optional
`suggestion` and `disposition` (worked-around | filed-task | fixed | escalated |
noted).

`source` must name a Gobby surface: `gobby-<server>:<tool>`; `<surface>:<name>`
where surface is `rule`, `hook`, `skill`, `workflow`, `agent`, `pipeline`,
`prompt`, `cli`, `binary`, `daemon`, `ui`, `docs`, or `config`; or a repository
path starting with `src/gobby/`, `crates/`, `web/src/`, or `docs/`.

For an actionable Gobby defect, read dispositions through the found-work ladder:
`fixed` includes a #N task claimed or closed by the observer or by one of the
observer's spawned descendant sessions; `escalated` includes the active owner session
ref after `send_message`;
`filed-task` is rung 3 only and includes a #N task carrying `needs-decision` or
`clean-window`, with its description explaining why rungs 1 and 2 do not apply.
Unlabeled or unclaimed filings, plus every other defect disposition, are shirked
found work.

Frozen review run: `{{ run_id }}`.

Read `gobby-feedback:get_review_observations(run_id="{{ run_id }}", offset=0, limit=50)`.
Follow every `next_offset` until null. Review only this frozen batch; new feedback
belongs to later runs. The reader is the authoritative source of observation IDs.

## Instructions

1. Cluster observations that describe the same underlying behavior, tool, or
   workflow. Singleton clusters are fine. Every observation id must appear in
   exactly one cluster.
   Copy every repository-relative path cited by a cluster's evidence or suggestion
   into `cited_paths`; use an empty list when no repository path is cited. Do not
   infer paths that the observations do not name. Deterministic intake code verifies
   each path against HEAD and checks commits touching it after the observations.
2. Classify each cluster:
   - `defect`: something is broken or misbehaving (most `bug` and reproducible
     `friction` clusters).
   - `guidance-gap`: the platform behaved as built but agents were misled or
     under-informed — docs, rule text, schema descriptions, missing affordances.
   - `noise`: one-off, stale, or unactionable observations.
   - `praise`: `useful` observations worth keeping visible; never a task.
3. Propose a task (`proposed_task`) only for `defect` and `guidance-gap` clusters
   that are actionable now. Respect dispositions: a cluster whose observations
   already have a ladder-compliant `fixed` or `filed-task` disposition gets
   `proposed_task: null` — mention the existing resolution and any task refs like
   `#12345` in `digest_note`. Treat an unclaimed or unlabeled `filed-task` ref as
   unresolved; deterministic digest code verifies the task state. Propose at most
   {{ max_tasks }} tasks; prioritize by frequency and impact.
4. Task titles must be imperative, specific, and self-contained (deterministic
   intake deduplicates them against open tasks by theme and attached
   observation ids). Descriptions must carry the
   evidence: what happened, where, how often, and the suggested direction if the
   observations include one. Priority: 1 for recurring defects that block work,
   2 for the rest, 3 for minor polish.
5. Write a one-or-two-sentence `digest_note` per cluster for the human digest:
   what the cluster says and what, if anything, was proposed.

## Output

Produce exactly this JSON shape:

{
  "clusters": [
    {
      "observation_ids": ["b3d2…", "9f41…"],
      "cited_paths": ["src/gobby/tasks/validation.py"],
      "theme": "close_task reruns validation after every retry",
      "classification": "defect",
      "proposed_task": {
        "title": "Cache close-gate validation verdicts per evidence state",
        "description": "Two sessions observed …",
        "labels": ["feedback-review"],
        "priority": 1
      },
      "digest_note": "Close gates re-ran validation on unchanged evidence; a caching task is proposed."
    }
  ]
}

Every cluster requires `observation_ids`, `cited_paths`, `theme` (a short specific
phrase naming the underlying behavior), `classification`, and `digest_note`;
`proposed_task` is null when no task is warranted. Tasks created from proposals are
marked `llm-reviewed` and `awaiting-human-review`; a human removes the latter label
after verification to make the task dispatchable. Do not add other keys. Do not
invent observation ids, task refs, paths, or behavior beyond the evidence.

Submit the compact JSON object as the exact `current_state` value in your
`end_agent_run` handoff, with one nonblank `next_steps` item telling deterministic
feedback intake to validate and apply the proposal. Do not wrap the JSON in
Markdown or add commentary to `current_state`.
