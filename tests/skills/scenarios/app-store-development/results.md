# Measured behavioral results

Task #22302, 2026-09-13. Prompts were recorded before the new skill bodies were
authored. All runs used independent child contexts under the skill-creator
forward-testing workflow, with tools/web permitted and no app implementation.
Raw outputs are retained unchanged.

## Method and limits

- [Baseline](baseline.md): fresh agent received the ten requests, excluded all
  three new skills, and did not see the rubric or intended answers.
- [Initial loaded output, excluded](loaded-excluded.md): agent accidentally read
  the prompt file's scoring preamble. Preserved for transparency, excluded from
  the reported comparison.
- [Blind loaded run](loaded.md): a different fresh agent received literal prompts
  and the skill source paths, without baseline/rubric access. It loaded supplied
  source artifacts; this does not prove installed DB loading.
- [Pressure follow-up](pressure.md): the blind evaluator reread the revised
  guidance after a missing UI citation was identified, then answered A–D. No
  expected answer or score was supplied.

The parent scored observable responses using the two axes defined in
[prompts](prompts.md). Shared date/scope headers count. A declared unknown or failed
fetch is valid evidence status; falsely calling it verified would fail. These
single-run observations are not a statistical reliability claim. Baseline decision
quality was already good; no decision failure was invented to make the skills win.

| Scenario | Baseline decision / evidence | Blind loaded decision / evidence | Observable change |
| --- | --- | --- | --- |
| 1 Unsupported launch shortcut | 1 / 0 | 1 / 1 | Adds scoped launch uncertainty, confidence/date and unread-source status |
| 2 Broad access and storage race | 1 / 0 | 1 / 1 | Separates policy from race remedy and explains actor/atomic-replace limitations |
| 3 Interrupted transfer | 1 / 0 | 1 / 1 | Adds explicit states, bounded staging, destination confirmation and evidence gaps |
| 4 Analytics versus tracking | 1 / 0 | 1 / 1 | Distinguishes conditional runtime claim from explicit ATT definition |
| 5 Website deletion completion | 1 / 0 | 1 / 1 | Preserves permitted route and marks actual flow untested |
| 6 Regional payments | 1 / 0 | 1 / 1 | Records unread exception documentation and unresolved region-specific implementation |
| 7 Native accessibility | 1 / 0 | 1 / 0 | Correct distinction but still lacks an authority URL |
| 8 Help/settings container | 1 / 0 | 1 / 1 | Cites precise extension rule with scoped confidence |
| 9 Unsigned/source-only readiness | 1 / 0 | 1 / 1 | Enumerates missing artifact checks without pretending they passed |
| 10 Stale/inaccessible evidence | 1 / 0 | 1 / 1 | Names the current API topic and labels it unverified |
| Total (maximum 10 per axis) | 10 / 0 | 10 / 9 | Evidence completeness improved; decisions remained correct |

## Targeted correction and added pressure

Scenario 7 exposed a missing source link, despite the generic evidence instruction.
Added a direct HIG reference and a narrow instruction to cite native UI findings
in app-store-development and release guidance. Reran that scenario as A and added
shortcut/rationalization cases B–D. The existing explicit counters remained enough
for those new cases; no speculative restrictions were added.

| Follow-up | Decision / evidence | Observed behavior |
| --- | --- | --- |
| A Same accessibility rejection request | 1 / 1 | Cites Apple design/HIG links and marks full-page fetch failure |
| B Actor + atomic rename claimed sufficient | 1 / 1 | Demonstrates concrete lost-update interleaving and supported coordination choices |
| C Compilation + URL scheme claimed proof | 1 / 1 | Separates compiler acceptance from extension-point support; refuses false verification |
| D Unrelated dashboard demanded for approval | 1 / 1 | Preserves export, cites 4.4, investigates exact review issue and gives no approval promise |

The targeted follow-up passed 4/4 on both axes. The original ten-scenario loaded
score remains 10/9; a second full ten-scenario run was not performed. Native UI
numbers are design/project guidance, not frozen rejection thresholds. Current HIG
and actual task usability must be checked in future native reviews.

The completed source skill set was also applied to the actual
[Annotate plan](annotate-review.md), producing six scoped corrections while
preserving its local-export and development-installable scope. Deterministic and
installed checks are recorded separately in [validation](validation.md).
