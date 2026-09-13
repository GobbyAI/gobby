# Reconcile a release

Load for release audits and rejection response preparation. Apply the shared
evidence contract; never promote a source-only assessment into archive validation.

## Establish the artifact

Identify the exact .xcarchive/exported app or IPA, build/version, source revision,
configuration, SDK/Xcode and deployment targets. Record archive location/hash and
audit date. If absent, finish useful source review and name archive/runtime checks
as unverified. Simulator success and unsigned macOS Safari testing are insufficient.

Inspect the main app and every embedded extension/framework separately. On macOS,
read-only tools can include `plutil -p`, `codesign -d --entitlements :-`,
`codesign --verify --strict --verbose=2`, `security cms -D -i` for an embedded
provisioning profile, and `otool -L` for linked libraries. Use the proper bundle
path for the platform; do not assume every distribution embeds the same profile.
Extract supplied archives into temporary audit storage. Do not re-sign or upload
them as part of inspection. Report command failures instead of bypassing checks.

Reconcile:

- Team/application IDs, signing identities, distribution method, profiles where
  applicable, sandbox/capability entitlements and App Group memberships.
- Embedded extension points, target bundle IDs, versions, architectures, minimum
  OS, resource membership and extension-safe APIs. Inspect actual Info.plists and
  compiled entitlements; project settings express intent only.
- Dependency inventory, embedded binaries and signatures, privacy manifests,
  required-reason API declarations, and Xcode's generated privacy report.
- [Upcoming requirements](https://developer.apple.com/news/upcoming-requirements/)
  and [submission help](https://developer.apple.com/help/app-store-connect/manage-submissions-to-app-review/overview-of-submitting-for-review/)
  for current SDK floors, metadata and dated submission obligations. Record
  the rule's effective date and applicability rather than copying old deadlines.

A valid signature proves integrity/signing facts, not correct behavior or approval.
Build, entitlement, privacy and runtime evidence must describe the same artifact.

## Reconcile behavior and submission materials

Exercise supported devices/OS versions and the current shipping Safari where
applicable. Record install/enablement, denied permissions, cold start, suspension,
offline/retry paths, app handoff, export recovery and account/purchase flows relevant
to this release. Check accessibility and native navigation with Impeccable's iOS
guidance. A HIG recommendation is not automatically a guaranteed rejection; label
the actual usability impact and any separately evidenced policy violation.
Give UI findings an authority too: cite the relevant
[Apple HIG](https://developer.apple.com/design/human-interface-guidelines/accessibility)
or exact project design requirement, and state when that source was not readable.

Compare the binary's behavior against privacy policy, App Privacy answers,
screenshots, description, age rating, support links, purchase descriptions and
review notes. Use [App Review preparation](https://developer.apple.com/distribute/app-review/)
and [App Store Connect Help](https://developer.apple.com/help/app-store-connect/).
Avoid guessing fixed screenshot sizes or reviewer device models from old checklists.
Supply a draft explanation for unusual extension enablement/handoff and, when
needed, a working demo account/demo mode and reachable services. Keep credentials
out of committed reports. A local-only feature needs no invented remote backend.

## Resolve rejection precisely

Tie the rejection text to the rejected version, cited rule and reproducible path.
Determine whether code, metadata, reviewer access or explanation needs correction.
Propose the smallest complete fix and verify the affected path in the replacement
artifact. If evidence conflicts, prepare a factual clarification draft with the
specific question and supporting sources; do not send it without authorization.

End with checked facts, remaining blockers/evidence gaps, and recommendations.
“No issue found in inspected scope” is a bounded claim. Never guarantee approval
or describe unperformed archive/device/submission checks as passed.
