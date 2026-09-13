# Apple skill behavioral scenarios

Task: gobby-22302. Defined before skill authoring. Run each request independently
with the three new skills excluded, then in a fresh context with them loaded.
Preserve raw responses; do not supply expected answers to the evaluating agent.

Score each response on two independent axes:
- Decision (1 point): supported scoped decision, preserving functionality without
  the proposed shortcut or false restriction.
- Evidence (1 point): behavior, rule or limitation, URL, verification date,
  platform/version/storefront (unknown allowed), confidence, and remedy are all
  recorded. A citation alone is insufficient.

Single paired runs measure these prompts only. Do not invent baseline failures.

1. Ship tomorrow: iOS Safari extension native handler will use UIApplication.shared via responder-chain tricks to open containing app automatically after annotation. Approve?
2. Use <all_urls> always and have app+extension overwrite same App Group JSON queue. Simplest reliable approach?
3. Keep iOS extension background JS alive while a 300 MB export uploads, mark done immediately, retry if killed. Is that sufficient?
4. Local-only screenshot redaction plus aggregate first-party analytics: must prompt ATT because analytics exists?
5. Account deletion starts in Settings then links directly to authenticated deletion page on our website. Reject solely for leaving app?
6. US and EU digital subscriptions: avoid IAP using one external purchase link everywhere; no time to inspect entitlements. Accept?
7. Native UI lacks recommended 44-point targets and Dynamic Type; label each automatic App Store rejection?
8. Safari extension containing app only provides enablement help and settings. Inherently violates minimum functionality?
9. Source builds unsigned for simulator, no archive/signing/runtime evidence. Mark submission-ready?
10. Current Apple page unavailable; 2016 archive and a forum post conflict about app launch. Decide conclusively that all extension-to-app handoff is forbidden?
