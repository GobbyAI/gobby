# Evidence for Apple decisions

Load for architecture decisions, policy findings, release audits, or conflicting
documentation. This is the shared evidence contract for all three Apple skills.

## Record a scoped finding

For each consequential finding record:

| Field | Required content |
| --- | --- |
| Behavior | Feature and code/plan/archive/runtime evidence affected |
| Classification | Explicit requirement, platform limitation, engineering recommendation, or unresolved interpretation |
| Authority | Guideline section or exact API/documentation topic and source URL |
| Verification | Date actually checked; whether the source was readable and current |
| Applicability | OS, deployment/Safari/SDK version, extension type, distribution channel, storefront; say unknown or not applicable instead of guessing |
| Confidence | High/medium/low with reason; separate policy certainty from untested implementation |
| Remedy | Supported approach preserving the feature, plus a concrete test or missing evidence |

A compact table or shared scope header is sufficient; avoid paperwork for unrelated
edits. Urgency does not turn assumptions into verification. Record limited review
as limited, even if a stakeholder asks for a green release verdict.

## Source hierarchy and freshness

- Reopen the relevant [Review Guidelines](https://developer.apple.com/app-store/review/guidelines/)
  at architecture and release decisions. Follow linked exception/entitlement pages.
- Use current Apple API documentation and installed SDK availability for platform
  capability; inspect the exact extension point and deployment target. A method's
  existence on a superclass does not prove that a particular extension supports it.
- Use [App Store Connect Help](https://developer.apple.com/help/app-store-connect/)
  for submission materials and [upcoming requirements](https://developer.apple.com/news/upcoming-requirements/)
  for dated changes. Do not freeze SDK floors, deadlines, payment exceptions or
  reviewer device models into permanent rules.
- Prefer Apple's linked Markdown documentation when a page renders only a
  JavaScript shell. Search official documentation or inspect SDK declarations if
  that fails. Record inaccessible sources; never label a failed fetch verified.
- Archived guides explain historical architecture, not necessarily present API
  availability. Dated WWDC sessions supply context. Forums/DTS answers can narrow
  an interpretation but are not universal policy. Preserve conflicts, dates and
  platform differences; propose a supported fallback and a bounded verification.

## Scope and restraint

Assess the changed feature, then broaden only for connected risks. Preserve useful
functionality: a documented user action or durable handoff may solve a lifecycle
problem without removing export. Absence of evidence is neither permission nor a
blanket prohibition. Source-only review cannot establish release readiness.

Guidance here is original, researched from Apple sources. External skills were
coverage comparisons only; their assertions and licenses are not Apple authority.
No source text or code was adapted from either comparison baseline. Provenance and
measured evaluations live in the repository's Apple skill scenario evidence.
