# Shared storage and interrupted work

Load for App Groups, native messaging transfers, local queues, exports and
background work. These are engineering recommendations except where a cited API
imposes a requirement. Choose the least mechanism that solves the actual race.

## Separate persistence domains

WebExtension browser storage, the native extension's sandbox, the app sandbox and
the App Group are distinct. JS cannot directly read an App Group path. Verify
the app and extension's signed App Group entitlements and shared-container lookup.
Use [Apple's messaging and data-sharing model](https://developer.apple.com/documentation/safariservices/messaging-between-the-app-and-javascript-in-a-safari-web-extension).

Both native processes may write simultaneously. A singleton, Swift actor or
serial JS queue does not coordinate other processes. Avoid read-modify-overwrite
of shared JSON without cross-process coordination. Choose transactional storage,
appropriate file coordination/locking, or immutable per-transfer files with
atomic publication and a clear owner. Atomic replacement alone prevents torn
files, not lost read-modify-write updates. Include deletion/cleanup in the protocol.

Apple's [archived shared-container guide](https://developer.apple.com/library/archive/documentation/General/Conceptual/ExtensibilityPG/ExtensionScenarios.html)
requires synchronization and lists coordination options. Verify their present
API availability and lifecycle constraints before implementation; do not copy
historical code blindly.

## Transfer state must survive termination

Define receiving, validated/staged, and delivered states separately. Pick stable
transfer IDs and bound chunk count, bytes and total storage. Validate ordering,
duplicates, missing chunks, retry acknowledgments, path traversal and payload
integrity. Publish only a fully validated snapshot; never show partial files as
complete. Preserve source drafts until successful export and explicit user deletion.

For a file protocol, a temporary per-transfer directory and atomic final publish
can suffice if cleanup/read/write ownership is coordinated. For mutable queues,
use transactions or a proven interprocess locking design. Test simultaneous
transfers, app reads and deletions; a happy-path encode/decode test misses these races.

Do not keep browser background JS alive to finish a large ZIP or network upload.
Inspect target-specific background support, including
[persistent background availability](https://developer.apple.com/documentation/webkit/wkwebextension/haspersistentbackgroundcontent).
Checkpoint bounded work and handle process death between every stage. If native
network transfer is actually needed, load
`get_skill_file(name="app-store-development", path="references/platform.md")`
for URLSession constraints. An export-to-Files feature does not need an invented
network service or background mode.

## Verify the user-visible path

Exercise termination during chunks, after validation, before/after publication,
and while the containing app consumes the export. Cover lock contention, disk
full, cancellation, retries and cold app startup on iPhone, iPad and Mac.
Show “staged on this device” separately from “saved/shared” or “delivered to the
coding computer.” A share-sheet dismissal alone does not prove remote receipt.
Keep local recovery available if a foreground handoff is unavailable or declined.
