# Data, permissions and accounts

Load for capture, storage, export, analytics, protected resources, login or deletion.

## Inventory actual data flows

Record each data type, producer, device/server destination, recipient, retention,
identity linkage and purpose. Include transitive SDKs, diagnostics and exports.
Inspect runtime traffic when available. Screenshots can contain secrets even when
form values and HTML are excluded. Recommend preview, deletion and minimization
appropriate to the feature; do not silently add cloud collection or telemetry.

Use [App Privacy Details](https://developer.apple.com/app-store/app-privacy-details/)
to classify collection: local-only processing is not automatically off-device
collection. An explicit user export needs analysis of who receives/accesses it;
neither local-first branding nor the existence of a share sheet proves the label.

ATT is a separate question. Check the current
[tracking definition and exceptions](https://developer.apple.com/app-store/user-privacy-and-data-use/).
Local processing or first-party aggregate analytics alone does not establish
tracking. Inspect cross-company advertising/measurement linkage, data brokers,
IDFA access and SDK behavior. An analytics SDK can track even when the host does
not use its advertising feature. Do not prompt ATT just because analytics exists,
or treat consent as permission for fingerprinting. Record denied/restricted paths.

## Keep disclosures distinct and consistent

- Protected-resource usage descriptions and authorization: explain the actual
  purpose and handle denial using [Apple's resource-access guidance](https://developer.apple.com/documentation/uikit/requesting-access-to-protected-resources).
  Website access is not camera/Photos permission; request the capability actually used.
- Privacy manifests: inspect app, extension and SDK target contents. For
  [required-reason APIs](https://developer.apple.com/documentation/bundleresources/describing-use-of-required-reason-api),
  choose approved reasons matching actual use in the owning bundle. SDKs cannot
  borrow the host's declaration. Never invent a reason code.
- [Manifest data declarations and Xcode privacy report](https://developer.apple.com/documentation/bundleresources/describing-data-use-in-privacy-manifests):
  reconcile the aggregate report with runtime behavior and App Store Connect
  answers. A report is input evidence, not proof that the answers are correct.
- Privacy policy and store disclosures: explain collection, sharing, retention,
  deletion and contact routes accurately. Keep the policy accessible in the app
  and submission materials; a privacy manifest is not a substitute.

## Accounts and deletion

Before adding login, determine whether the feature actually needs an account.
For social/third-party login evaluate current Guideline 4.8 scope and exceptions;
do not claim every login requires Sign in with Apple.

When account creation exists, use Apple's
[account deletion guidance](https://developer.apple.com/support/offering-account-deletion-in-your-app/).
Make initiation discoverable in the app and remove the account and associated data,
subject to applicable retention obligations. Deactivation alone is insufficient.
A direct website link to the actual completion page is permitted; do not reject
solely because completion leaves the app. Verify authentication, confirmation,
completion timing, subscription handling and token revocation where applicable.
Do not add unnecessary support calls or email barriers. Distinguish deletion of
local drafts from deletion of an account; local tools need no invented account flow.
