# Gobby Annotate: Apple development corrections

Reviewed 2026-09-13 for task #22302 using app-store-development,
safari-extension-development and app-store-review, plus Impeccable's iOS reference.
Plan: `.gobby/plans/gobby-annotate.md`, Git blob
`44dd828c4b2787be5f9e1ca085b837ce0538fc32`.

Scope: desktop Safari/macOS and Safari on iPhone/iPad; deployment and Safari
versions are unspecified in the plan. Storefront is not applicable to its
development-installable deliverable; any future store distribution remains
unreviewed. This is a correction list, not an app implementation or submission
audit. No archive, signed entitlement, or runtime device evidence was supplied.

## Corrections within the existing feature intent

### 1. Specify the containing-app handoff (§2.5, §2.6, §3.5)

**Behavior:** The plan stages a bundle in native storage and gives the containing
app Save/Share UI, but never specifies how users reach that UI from Safari.
This is an underspecified transition, not evidence that the plan chose a hack.

**Classification/authority:** Platform limitation and unresolved direct-launch
capability. [Safari native messaging](https://developer.apple.com/documentation/safariservices/messaging-between-the-app-and-javascript-in-a-safari-web-extension)
reaches a native extension handler; it does not establish foreground app launch.
[NSExtensionContext.open](https://developer.apple.com/documentation/foundation/nsextensioncontext/open(_:completionhandler:))
support depends on the extension point. Verified 2026-09-13, including the latter's
Markdown. Confidence high on the distinction, unresolved for a direct Safari
launch API at the plan's unspecified deployment target.

**Correction:** Make explicit app opening the baseline: finish durable staging,
show “Open Gobby Annotate to save or share,” and list the export on cold app launch.
Retain a direct user-initiated link only after verifying a documented Safari route
for each actual target. Do not use UIApplication.shared, responder-chain tricks,
or private selectors. Do not generalize this into a prohibition on all handoff.

**Verification:** On installed Mac, iPhone and iPad builds, stage with the app
closed, open it manually, save/share, cancel, return to Safari, and recover the
same draft/export. No success message may imply delivery to the coding computer.

### 2. Define interprocess storage ownership (§2.6)

**Behavior:** App Group chunk staging and app export-list access can race. The
plan bounds chunks and forbids exposing partial transfers, but omits coordination.

**Classification/authority:** Engineering recommendation based on Apple's
[shared-container synchronization guidance](https://developer.apple.com/library/archive/documentation/General/Conceptual/ExtensibilityPG/ExtensionScenarios.html)
(archived, checked 2026-09-13) and current messaging's independent sandbox model.
Applies to macOS/iOS/iPadOS app plus native extension; confidence high that
interprocess ownership is needed, exact implementation unverified.

**Correction:** Specify unique transfer IDs, bounded temporary storage, validation
and atomic publish into immutable completed exports. Coordinate cleanup/deletion
with readers/writers. If shared metadata is mutable, use transactions or a proven
cross-process lock; an actor or atomic JSON replacement alone does not prevent
lost updates. Keep JS IndexedDB separate from App Group files.

**Verification:** Extend ExportStore tests with concurrent transfers, app reads,
deletion, duplicate chunks, disk full and process interruption before/after publish.
Inspect signed App Group memberships in the installed development builds.

### 3. Make browser termination recovery explicit (§2.4–§2.6)

**Behavior:** The background context coordinates IndexedDB and creates/forwards
exports. Suspension acceptance exists, but the restart/checkpoint contract is thin.

**Classification/authority:** Platform constraint plus engineering recommendation.
Current [WebKit persistence documentation](https://developer.apple.com/documentation/webkit/wkwebextension/haspersistentbackgroundcontent)
excludes persistent background content on iOS/iPadOS. Checked its Markdown
2026-09-13; confidence high, exact Safari versions still need selection/testing.

**Correction:** Persist transfer identity and acknowledged progress, make retries
idempotent and distinguish receiving/staged/delivered. Resume or restart from the
durable draft after termination. Keep the agreed 128 MiB uncompressed limit.
Do not add a network upload/background mode: the plan explicitly uses local export.
Native URLSession would only be relevant to a separately intended network feature.

**Verification:** Terminate during encoding and each native-message phase, relaunch,
retry, and confirm no partial export or false completion; preserve drafts.

### 4. Separate native UI from the browser overlay (§2.1, §2.4, §2.6, V1)

**Behavior:** Web controls have detailed responsive rules; ExportView's native
Save/Share UI lacks equivalent native verification.

**Classification/authority:** Project design obligation and engineering
recommendation, not automatic App Store rejection. `.impeccable.md` and
`impeccable:references/ios.md` were fully read 2026-09-13. Applies to native
iOS/iPadOS UI; macOS keeps its AppKit/SwiftUI conventions. Confidence high.

**Correction:** Reuse system native controls/navigation, scalable text, safe areas,
44-point native hit areas, accessible names/status and native share-sheet behavior.
Preserve deutan-safe status cues and both appearances. The browser overlay keeps
its CSS-pixel/web accessibility contract; do not replace it with a native UI.

**Verification:** Exercise large Dynamic Type, VoiceOver, keyboard access on iPad,
rotation, dismissal, empty/error states, Dark Mode and Reduce Motion in ExportView.
Label failures by actual usability impact and project requirement; cite any separate
Apple rule before claiming a rejection ground.

### 5. Specify minimal grants and denied paths (§2.1–§2.3, §3.5)

**Behavior:** User-triggered MV3 activation is already correct, but exact Safari
permission choices and revocation acceptance are absent.

**Classification/authority:** Explicit Safari policy plus platform behavior:
[Guideline 4.4.2](https://developer.apple.com/app-store/review/guidelines/) and
[permission documentation](https://developer.apple.com/documentation/safariservices/managing-safari-web-extension-permissions).
Verified 2026-09-13. Applies to all planned Safari platforms; confidence high on
least access, pending actual target API tests.

**Correction:** Prefer activeTab and scoped/optional hosts where they support
selection and visible-tab capture. Justify every broader grant with a required
operation. Declare nativeMessaging for the native handler and relay content-script
messages through extension context. Do not assume Chrome capture/injection support
or request camera/Photos access merely because the product creates screenshots.

**Verification:** Test grant/deny/revoke, fresh navigation, restricted pages,
inaccessible frames and relevant profile/private-browsing behavior on real Safari.
Retain the existing explicit “save without screenshot” recovery path.

### 6. Document actual local data handling (§1.2, §2.4–§2.6, §3.5)

**Behavior:** The plan excludes telemetry/cloud sync/form values/whole-page HTML,
but screenshots, URLs and selected text can still carry sensitive material.

**Classification/authority:** Engineering privacy recommendation; future disclosure
classification depends on actual recipients under [App Privacy Details](https://developer.apple.com/app-store/app-privacy-details/)
and [tracking guidance](https://developer.apple.com/app-store/user-privacy-and-data-use/).
Checked 2026-09-13; all planned platforms, confidence high on stated local intent,
runtime/dependency behavior unverified.

**Correction:** Preserve screenshot review and deletion, document local retention
and explicit user export destinations, and inspect dependencies for unexpected
networking. Explain that excluding form values does not redact screenshot pixels.
Do not add ATT, login/account deletion, or UGC moderation solely for this private
local workflow. Before any later submission, reconcile privacy policy, actual
data flows, required-reason APIs and manifests for app/extension/SDK bundles.

**Verification:** Inspect export payloads and runtime network behavior; test draft
and staged-file deletion. Confirm the UI truthfully describes storage and sharing.
No “Data Not Collected” store answer is certified by this plan review.

## Preserve the release boundary

The plan explicitly promises development-installable builds and excludes store
submission. Keep that scope. [Safari distribution documentation](https://developer.apple.com/documentation/safariservices/distributing-your-safari-web-extension)
(checked 2026-09-13) distinguishes unsigned simulator/Mac development tests from
signed device/distribution requirements. Confidence high; no release artifact was
reviewed. V1 should record actual installed device evidence rather than infer it
from source files or simulator tests. A later release needs its own archive,
entitlements, SDK requirements, metadata, privacy and reviewer-access audit.

No artificial native-feature quota is needed: Guideline 4.4 recognizes help/settings
containers. Annotate already plans useful export management. This review neither
promises approval nor changes the App Store Connect account or contacts reviewers.
