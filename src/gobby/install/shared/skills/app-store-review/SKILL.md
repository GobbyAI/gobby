---
name: app-store-review
description: "Review an Apple-app release or investigate an App Store rejection using code, the actual archive, runtime behavior, privacy disclosures, metadata and reviewer access. Use for release readiness or rejection work, not every ordinary edit."
version: "1.0.0"
category: development
triggers: app-store-review, app-store-rejection, apple-release-readiness
---

# App Store review

Scope the audit: iOS/iPadOS or macOS, extension type, deployment/Safari versions,
storefronts, distribution channel, release/build identity, and available evidence.
For rejection work read the exact message and rejected build before proposing fixes.

Load the shared evidence contract and the release procedure:

- `get_skill_file(name="app-store-development", path="references/evidence.md")`
- `get_skill_file(name="app-store-review", path="references/release.md")`

For privacy reconciliation load
`get_skill_file(name="app-store-development", path="references/privacy-accounts.md")`.
For payments/user content load
`get_skill_file(name="app-store-development", path="references/commerce-content.md")`.
For Safari-specific findings load `get_skill(name="safari-extension-development")`.
Select topics as needed, normally at most three references at once.

Calls use `gobby-skills`. Lease `get_skill_file` through `get_tool_schema` when
unleased, read each result completely, and follow `page.next_cursor` using only
`cursor` until null. Report unavailable sources or artifacts as evidence gaps.

Inspect the actual release archive when available. Source inspection and unsigned
builds establish only limited findings. Never label them submission-ready. Recheck
live Apple policy and dated submission requirements for this audit.

Deliver findings with behavior/artifact, rule/limitation, URL, verification date,
platform/version/storefront, confidence and remedy. Separate verified blockers,
engineering recommendations and unresolved interpretations. State exactly which
artifacts/runtime paths were checked and which remain unverified. Approval is
Apple's decision. Skill use does not authorize uploads, submissions, App Store
Connect changes, reviewer communications or credential sharing.
