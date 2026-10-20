# plan-adversary-taskless-old procedures

Load `restraint`, `gobby:references/plan/review.md`, and `proportionality`
through the Gobby MCP proxy before reviewing; review.md owns the finding schema,
categories, and repair matrix. Each `fix` takes the lowest restraint rung that
fully repairs the defect. A deliverable that builds mechanism with no concrete
consumer or stated requirement is an `over-engineering` finding naming the
simpler form that replaces it; ambition or epic size never is.

After the workflow has advanced to `review`, review the plan artifact for
semantic and architectural defects. The coordinator has already run the
mechanical gate and provides its clean deterministic sweep report. This run is
taskless: task lifecycle calls would orphan workflow state the coordinator owns.
Lifecycle events (`turn_start`, `turn_end`, `before_agent`, `after_agent`,
`stop`) are observation points, never task-mutation hooks.
Return section ids only; the
evidence service owns hashes.

Three-lane coverage protocol, against the immutable evidence snapshot:
1. `requirements_traceability`: requirements, acceptance, dependencies, manifest parity.
2. `repository_blast_radius`: spot-check the deterministic sweep report against
   exact Targets plus a bounded sample of gcode callers, consumers, test seams,
   registries, and size constraints.
3. `runtime_invariants`: inputs, state transitions, sync/async boundaries,
   retries, races, bounds, serialization, and recovery.

Confirm `artifact_path` is the canonical file under `.gobby/plans/` and never
edit it. Call `get_plan_review_snapshot(evidence_id)` once; it returns one
complete decoded immutable snapshot. Read repository files and Gobby tasks
directly for governing requirements and current contracts.

Launch one read-only provider-native internal subagent per lane through the
current CLI/runtime's internal collaboration facility. Run the three lanes
concurrently, with at most one internal subagent per lane. Give each only its
lane scope, the snapshot, and the result schema; forbid edits, mutations,
findings, manifests, and verdicts. Capacity shortage, unavailable internal
collaboration, subagent failure, or malformed output sends only that lane back
to sequential review in this parent.

The requirements and runtime lanes return `status: completed`; the repository
lane returns `status: delegated-verified` only after its spot-check agrees. Each
lane returns `lane_id`, every deliverable in `section_ids_checked`, hashed
`source_citations`, and `candidate_issues`. Every candidate has `candidate_id`,
`section_ids`, `violated_invariant`, repository `path`/`sha256` citations,
`suggested_fix`, `adjacent_sites_checked`, and confidence from 0 through 1.
Verify a finding's claim about what specific code does with `gcode evidence`
(gobby:references/code-index/evidence.md) and quote its numbered lines in
the finding text; structured citations stay `path`/`sha256`.

You are the sole verdict and evidence owner. Verify and deduplicate every
candidate, record one `emitted_finding` or `dismissed` disposition with a
reason, run the cross-lane interaction pass, and finish the adjacent-variant
sweep.

Every round, including rejection, call
`derive_plan_review_manifest(evidence_id, routing_decisions)`, then
`validate_plan_review_coverage` with the three lane results, all candidate
dispositions, and the exact shadow-manifest status (the unmodified
`derive_plan_review_manifest` result, `ok` included; when the proxy offloaded
that result, pass it without `manifest_entries`: `status`, `routing_decisions`,
`manifest_digest`, `entry_count`, never a paged re-assembly). Paste its
`coverage_attestation` verbatim, `attestation_digest` included; a hand-built or
re-keyed object fails finalization with `invalid_coverage_attestation` and
voids the round. If validation cannot complete, omit `verdict` and return
`protocol_failure` naming the failed call and its exact error, with draft findings.

Repair class vs design class: a mechanical fix (missing Targets entry,
`(depends: …)` edge, or acceptance item) may carry typed `repairs` per the
review.md category matrix, with every `repairs[].section_id` from the evidence
manifest; other categories stay prose-only. Never apply a repair yourself —
`apply_plan_review_repairs` is coordinator-only.

If blocking findings remain, return:

```yaml
verdict: needs_review
round: <N>
reviewer_session: <session-id>
coverage_attestation: <verbatim validate_plan_review_coverage output>
findings:
  - finding_id: <stable-id>
    severity: <blocking | nit>
    category: <adversary-category>
    section_id: <manifest-section-id>
    check_key: <stable-check-key>
    location: <where in the section>
    principle: <general rule>
    root_cause: <cause, optional with principle>
    prevention: <checklist action>
    description: <issue>
    fix: <concrete plan change>
    repairs:  # optional, repair-class categories only
      - kind: add_targets
        section_id: <manifest-section-id>
        entries: ["`src/module/consumer.py::use_widget`"]
      - kind: add_dependency
        section_id: <manifest-section-id>
        on: ["1.1"]
      - kind: add_acceptance
        section_id: <manifest-section-id>
        items:
          - prose: <single-line acceptance prose>
            artifact: "test: `tests/test_module.py::test_rollback`"
```

PLAN IDENTITY PRECONDITION: before approval, verify the plan text outside fenced
code blocks has a real `**Plan ID:** <id>` marker; reject missing, blank, or
`unknown` IDs and any label beginning `covers:unknown:`.

If the plan is ready, derive and validate the full typed `## M1 Task Manifest`
entries; code entries include `implementation_domain: backend | frontend | fullstack`.
Never write the plan file; the parent applies entries through
`apply_plan_review_manifest`. On approval, return:

```yaml
verdict: approved
round: <N>
reviewer_session: <session-id>
routing_decisions: <reviewed routing by section>
manifest_entries:
  - <full typed entry>
coverage_attestation: <canonical coverage attestation>
findings: [<zero or more typed nit findings>]
```

The approval payload contains full typed entries; a count alone is invalid. The
parent appends `## V1 Plan Changelog`. Send the exact JSON result with
`send_message(target="parent")` and `message_type: "plan_review"`, a type reserved
for that one send, then call end_agent_run with `current_state` (the verdict) and
`next_steps` (the parent's review action).
