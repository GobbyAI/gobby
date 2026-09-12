# Drafting a plan
Load before writing or revising a plan narrative.

## Discover
Read the [Plan-Coverage Contract](../../../../../../../../docs/contracts/plan-coverage.md) and [coverage](coverage.md). Load standalone `restraint` and `elicit`. Resolve repository facts with gcode before asking about material choices; use the grill-me protocol and confirm the Decision Record.

## Author
1. Map outcomes and dependencies. One deliverable section becomes exactly one manifest entry and one implementation leaf.
2. Use `.gobby/plans/<slug>.md` as the sole authority when writable. Every user-facing canonical plan body starts with `Plan artifact: \`.gobby/plans/<slug>.md\``. Provider display mirrors must stay synchronized or point at that file; never overwrite it with a stale mirror.
3. Include a real `**Plan ID:** <id>` outside code fences before review/approval; never leave it blank or unknown. Give each section concrete Targets, its own Research context, implementation detail, acceptance items, and focused verification. Capture observed entry points, exact file-qualified symbols, call paths, reusable helpers, fixtures, constraints, rejected alternatives with reasons, and observed versus planned checks. Copy shared findings into every executor's section.
4. Enumerate behaviors and lifecycle owners. Split independently implementable/testable/committable outcomes even when they share a feature name or file. Keep one behavior's implementation, consumers, failure cases, and tests together.
5. Record a **Granularity:** decision for more than six acceptance items, more than six distinct hand-maintained production Target files, or two independently testable state machines/lifecycle owners. Counts trigger inspection; they are not parser limits. Never hide obligations to fall below them.
6. Run project-aware base validation after each accepted revision:
   `uv run gobby plans validate <plan-file> -p <project-root>`.
   A narrative first draft has no M1; expansion validation is explicitly unrun. A manifest-bearing revision needs both base and expansion validation.

## Constraints
Resolve material questions before finalization. Do not invent planning tasks, review tasks, TDD wrapper leaves, or per-file chores. Standalone test infrastructure can be its own outcome. Revisions sweep the whole plan for the same finding class, retain every acceptance obligation, and update dependent references; the manifest must be re-derived through approval after narrative changes.

## Write restrictions and recovery
If canonical writes are unavailable, preserve the complete latest Markdown draft in `set_handoff(clear_session=false)` current_state, decisions/approvals in key_decisions, unresolved questions in notes, continuation in next_steps. Load session handoff guidance first. Restore with argumentless `get_handoff`. No scratch store, indirect writer, or subagent may bypass restrictions.
Materialize the complete draft once writable; only then validate/review/register/expand. A compaction-triggered interrupted set_handoff is the daemon boundary, not a user refusal.
After drafting, enhancement, each finalized review, and approval, offer the applicable checkpoint choices: continue interactively, run enhancement, run adversarial review, approve for implementation, stop. Stop preserves the current authority and starts no next phase.

See [Specification writing](../../../../../../../../docs/guides/spec-writing.md#plan-shape).

_Last verified: 2026-09-12_
