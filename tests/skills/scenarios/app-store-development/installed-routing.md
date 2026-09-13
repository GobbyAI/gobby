# Installed Apple routing verification — Gobby #22302

Verified 2026-09-13 through installed gobby-skills only. No repository/source fallback, implementation, archive audit, submission, or external communication.

## Help

Loaded installed Gobby router (1 complete page) and catalog.json (4 complete pages, final next_cursor=null). Apple skill names do not collide with catalog capability names, so Codex $gobby <name> dispatches get_skill(name=<name>).

Exact active-session help listing: list_skills(enabled=true, session_id="#13169", limit=100) returned count=0; canonical UUID 658f9cd9-bd25-4fdd-9708-5e134265a94f returned count=0 too. Both complete because 0<100. Default internal visibility and session filtering preserved. Thus no Apple standalone entry is advertised by help for this current session. Installed discovery reference confirms target sessions can restrict results to active skill names; the exact active filter state was not inspected. Do not label help as successfully advertising the three entries.

Diagnostic unscoped list_skills(enabled=true, limit=100) returned 39<100. Its oversized result was completely retrieved in four slices (0,6866,12866,18866 → null). These diagnostic installed Apple entries are available by explicit dispatch, not substituted into the session-filtered help menu:

- $gobby app-store-development: Design or change Apple-app features with App Store policy implications: APIs, lifecycle, privacy, accounts, payments, user content, or dependencies. Use early for iOS/iPadOS and macOS feature decisions; ordinary edits need only applicable checks.
  Identity: 217db0f3-0596-5f7e-9bdb-ac55331905e8; source=installed; enabled=true.
- $gobby app-store-review: Review an Apple-app release or investigate an App Store rejection using code, the actual archive, runtime behavior, privacy disclosures, metadata and reviewer access. Use for release readiness or rejection work, not every ordinary edit.
  Identity: 53be0897-4fd4-5d3a-9804-3423d77eaa92; source=installed; enabled=true.
- $gobby safari-extension-development: Design, implement or review Safari extensions on iOS/iPadOS and macOS. Use for website access, extension APIs, lifecycle, messaging, storage, containing-app handoff and distribution boundaries.
  Identity: 746f4469-24c8-58cd-81f1-8369ee6bed06; source=installed; enabled=true.

## Explicit routing

In requested order, get_skill(name="app-store-development"), get_skill(name="safari-extension-development"), and get_skill(name="app-store-review") all succeeded. Each entrypoint is 1 complete page, no continuation/truncation. Each declares exact installed reference calls and current-scope evidence requirements.

One selected reference per skill loaded completely (each 1 page, complete=true, next_cursor=null):

- app-store-development: references/evidence.md (shared evidence contract, prerequisite also reused for review).
- safari-extension-development: references/capabilities.md (API/permissions/handoff boundary).
- app-store-review: references/release.md (release/rejection audit procedure).

Installed gobby references/skills/discovery.md also loaded completely (1 page) to interpret session filtering. Shared-context warning prompted installed references/sessions/handoffs.md load (1 page), but no shared session compaction was performed.

No failed skill/file loads. Help filtering limitation remains explicit; no claims about Apple policy verification or actual app readiness were made. The summary above preserves tool outcomes, page counts, identities and diagnostic differences. Full raw tool envelopes remain in the evaluation transcript; duplicated skill bodies and unrelated catalog metadata are omitted from this committed report.
