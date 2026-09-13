# Capabilities, permissions and handoff

Load before choosing an extension API or app handoff. Verify the actual OS/Safari
and deployment target; feature detection is useful but does not replace API scope.

## Map the extension type

| Type | Boundary to verify |
| --- | --- |
| Safari web extension | WebExtension manifest/JS plus native extension handler and containing app; supported on iOS/iPadOS and macOS |
| Safari app extension | macOS SafariServices APIs and Info.plist configuration; not an iOS web-extension API substitute |
| Share/Action extension | Host-provided extension context and extension-point lifecycle; do not transfer its launch behavior to Safari |

Use [Safari extensions](https://developer.apple.com/safari/extensions/) and
[web extension creation](https://developer.apple.com/documentation/safariservices/creating-a-safari-web-extension)
as current entrypoints. Inspect API support for captureVisibleTab, scripting,
downloads, background pages/workers, messaging and storage independently. A
Chrome build succeeding does not verify Safari behavior or restricted pages.

## Request only necessary website access

Follow [Safari permissions](https://developer.apple.com/documentation/safariservices/managing-safari-web-extension-permissions).
Prefer activeTab, specific hosts and optional permissions when they satisfy the
feature. Distinguish MV3 host_permissions from API permissions and content-script
matches. Justify broader access with actual functionality; do not mandate
<all_urls> for convenience or ban it when narrower access cannot serve the feature.
Exercise grant, deny, revoke, navigation, inaccessible frames, restricted pages and
private/profile contexts on each supported target. Explicit invocation and user
gestures can change available access; a manifest declaration alone is insufficient.

## Native messaging is not foreground launch

[Apple's messaging documentation](https://developer.apple.com/documentation/safariservices/messaging-between-the-app-and-javascript-in-a-safari-web-extension)
separates browser JS, native app extension, and containing app:

- Background/extension pages use nativeMessaging to reach the native handler.
  Content scripts relay through extension messaging, not directly to native code.
- Treat native input as untrusted; validate message type, size, IDs and paths.
- Containing macOS app to JS messaging has a documented route. The current article
  says containing iOS apps cannot send messages to extension JS. Verify target
  availability before relying on connectNative or unsolicited reverse messages.
- A successful native response does not establish that the containing app is
  foregrounded, that its UI ran, or that a user received a file.

For app opening, identify the caller, exact API, extension point, OS version and
required user action. Check [NSExtensionContext.open](https://developer.apple.com/documentation/foundation/nsextensioncontext/open(_:completionhandler:))
and the extension-specific documentation/SDK. A URL scheme registration alone
does not prove the caller can open it. UIApplication.shared, responder-chain
tricks and private selectors are not supported extension launch mechanisms.
Do not generalize that failure into “all handoff is forbidden.”

If a supported direct link cannot be established for the chosen Safari target,
stage the export durably and tell the user to open the containing app explicitly.
Test cold/warm app state, cancellation and return to Safari. Investigate a
documented user-initiated route only when it preserves the feature; do not
promise an automatic foreground launch. Archived
[extension architecture](https://developer.apple.com/library/archive/documentation/General/Conceptual/ExtensibilityPG/ExtensionOverview.html)
and forum answers need age and scope labels, especially when current evidence conflicts.

## Container and distribution

[Guidelines 4.4/4.4.2](https://developer.apple.com/app-store/review/guidelines/)
recognize help/settings functionality in containing apps. Do not declare that
container inherently noncompliant or invent an unrelated native feature quota.
Evaluate the useful extension and its truthful enablement/marketing together.
Keep extensions free of advertising, marketing and IAP, and respect Safari UI.

Use [distribution documentation](https://developer.apple.com/documentation/safariservices/distributing-your-safari-web-extension)
for signing and device testing. Unsigned simulator/Mac development testing does
not establish distribution readiness. A release audit must inspect the actual
signed app and embedded extensions, not just the Xcode source project.
