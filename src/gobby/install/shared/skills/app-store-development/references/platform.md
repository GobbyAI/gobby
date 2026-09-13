# APIs, execution and dependencies

Load when a feature changes platform access, execution lifetime, dynamic behavior,
or third-party code. Apply the shared evidence contract to consequential findings.

## Trace the execution boundary

Map each operation to its process: app UI, native extension, browser background,
content script, system transfer service or backend. Record who owns durable state
and how work resumes after termination. A Swift actor protects one process only.

Check public API availability for the actual target and extension point. Keep
extension-safe build settings and compiler diagnostics enabled. Private selectors,
responder-chain escapes and dynamic lookup do not make unavailable APIs supported.
For downloads, distinguish user data/content from executable functionality and
check the applicable exceptions in [Guidelines 2.5.1–2.5.4 and 4.7](https://developer.apple.com/app-store/review/guidelines/).
Do not call every downloaded file prohibited code or assume remote feature changes
are allowed because they execute in JavaScript.

## Background work

An app's background mode is not an unlimited runtime grant for its extensions.
Use a documented mechanism that fits the operation. Browser JavaScript and native
extension requests can end; persist progress before acknowledging completion.

For native network transfers inspect [background URLSession](https://developer.apple.com/documentation/foundation/downloading-files-in-the-background)
and [sharedContainerIdentifier](https://developer.apple.com/documentation/foundation/urlsessionconfiguration/sharedcontaineridentifier).
Check file-backed upload constraints, session identifiers, completion callbacks,
and containing-app recovery for the chosen extension/OS. A shared container is
required for extension URL sessions. This facility does not keep arbitrary JS,
ZIP encoding, or UI alive. Stage bounded durable work and test interruption,
duplicate delivery, cancellation, retries, low storage and user force-quit behavior.

These persistence/testing choices are engineering recommendations. Claims about
specific APIs must cite current availability; archived
[extension scenarios](https://developer.apple.com/library/archive/documentation/General/Conceptual/ExtensibilityPG/ExtensionScenarios.html)
are historical context and need current confirmation.

## Dependency and platform inventory

Inspect lockfiles, binary frameworks, target membership, licenses and transitive
SDK behavior. Source dependencies alone do not prove what the archive embeds.
Check [third-party SDK requirements](https://developer.apple.com/support/third-party-SDK-requirements/)
for the actual SDK versions, binary signatures and privacy manifests, including
repackaged dependencies. Remove unused sensitive capabilities or update the
dependency instead of adding fabricated declarations.

For macOS distinguish Mac App Store sandbox/signing constraints from Developer ID
distribution and notarization. Neither notarization nor an unsigned development
run is App Store approval. Use [Safari distribution](https://developer.apple.com/documentation/safariservices/distributing-your-safari-web-extension)
for containing-app and extension signing; recheck current submission SDK/tooling
requirements separately. Do not transfer macOS process privileges to iOS/iPadOS.
