# Review evidence

Load before evaluating task or epic completion. Discover task records through
`gobby-tasks:get_task`, `list_tasks`, linked commits and artifact readers; load
[task reviews](../tasks/reviews.md) for stage evidence and
[source control](../source-control/overview.md) for implementation diffs.

## Establish the review scope

Read the epic and all descendant tasks, recursively enumerating child epics and
completing list pagination. Collect the approved plan, coverage matrix, task
descriptions and criteria, linked commits, close notes, validation output and
aggregate implementation diff. A closed task or passing command alone does not
prove the required behavior.

For docs/build epics without a plan artifact, a Discovery Brief, validation
criteria and full descendant task set can establish equivalent scope. Reconstruct
an unavailable aggregate diff from descendant commits or the integration branch.
Escalate only when neither reliable scope nor implementation evidence can be
established; name the missing evidence and concrete decision.

## Apply the checks in order

1. **Spec compliance:** compare required behavior with implementation and child
   outcomes; identify missing coverage, integration gaps, drift and omitted cleanup.
2. **Code quality:** assess maintainability, architecture fit, cross-leaf
   consistency, safety and fragile or duplicated implementations.
3. **Testing:** assess the validation types the scope actually requires. For
   `tdd:required`, requested `test-driven-development`, or explicit TDD criteria,
   require the expected red failure, minimal green, refactor/final green, exact
   commands and supported touched-test quality audit. Missing TDD evidence blocks
   approval. A missing audit baseline is not a reason to skip.
4. **Proportionality:** load the standalone skill. Flag unjustified mechanism with
   its missing consumer and a complete simpler replacement preserving acceptance
   coverage. Size and ambition alone are not findings.

Cite each blocking finding to a stable plan section (`### N.N`) or a concrete
substitute-scope item, child criterion, file or commit. When it cannot be mapped,
explain the omitted requirement or scope drift. Include a Relevant memory/lesson
column in finding triage, and recall review context before finalizing decisions.

## Local audit and recovery

The agent-supported local command is `uv run gobby test-quality audit <touched-test-paths>`.
It analyzes test structure; it does not run tests. A zero exit without
`--fail-on-new` does not establish zero findings: read the report. To enforce the
baseline comparison, supply `--baseline <path> --fail-on-new`; missing baselines
treat current issues as new. A zero-analyzable-file audit fails. Unsupported
language evidence outside Gobby requires focused repo-native validation.

Do not manufacture green evidence, hide new weaknesses in a baseline, or treat
suppression as a fix without its explicit rationale. Fix encountered failures
under the [task found-work workflow](../tasks/implementation.md). Run mutations
only in isolated fixtures/test daemons, never against live user state. In Gobby,
use the isolated PostgreSQL test hub and `GOBBY_TEST_PROTECT=1`; full pytest
requires an explicit request.

Details: [audit findings](../../../../../../../../docs/guides/test-quality.md#audit-findings),
[baselines](../../../../../../../../docs/guides/test-quality.md#baselines),
[suppressions](../../../../../../../../docs/guides/test-quality.md#suppressions),
and [focused mutation testing](../../../../../../../../docs/guides/test-quality.md#focused-mutation-testing).
