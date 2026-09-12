# Safari development installation

Build shared resources with `npm ci && npm run build:extension` from
`gobby-annotate/`, then open `safari/GobbyAnnotate.xcodeproj` in Xcode. The project
contains macOS and iOS app/extension targets. App identifier is `ai.gobby.annotate`;
the extension uses `ai.gobby.annotate.Extension`. Configure your development team
externally for every target and register the shared `group.ai.gobby.annotate`
App Group. Do not commit personal signing-team values.

Select Xcode per command instead of changing the machine's global developer path:

```sh
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild \
  -project safari/GobbyAnnotate.xcodeproj -scheme 'GobbyAnnotate (macOS)' \
  -destination 'platform=macOS,arch=arm64' -configuration Debug \
  -derivedDataPath safari/build CODE_SIGNING_ALLOWED=NO build
```

This checks compilation only. For development installation, select your team and
Mac/iPhone/iPad destination in Xcode and Run a signed build. Enable the extension
in Safari Settings on Mac or Settings → Apps → Safari → Extensions on iOS/iPadOS.
Grant access to the site you want to annotate, then activate from Safari's
extension controls. Developer-mode/unsigned-extension options on Mac may help
local debugging but do not replace signed App Group verification on devices.

Exports travel as ordered, bounded native-message chunks into App Group storage.
Only a completed transfer becomes a visible ZIP. Open the containing app to see
staged exports, then use Save on Mac or Share/Save to Files on iPhone/iPad. Transfer
the resulting file to the coding computer and place it in its configured MCP
capture root. The app's staged list does not mean that transfer has happened.

If a transfer is interrupted, its partial data stays hidden. Drafts remain in the
extension; use the app's clear-interrupted action and export again. Native-message
failures commonly indicate the app/extension signing or App Group entitlement
does not match. Storage/save/share errors are reported without deleting drafts.

Safari captures the real visible viewport, including its current orientation,
zoom, and keyboard constraints. Test both themes at 440×956 portrait, 932×430
landscape, desktop, and 320px width, with actual touch and software-keyboard
interaction on iPhone and iPad. Also verify same-origin frames, inaccessible
frames, open shadows, nested scrolling, capture-time navigation, extension reload,
storage errors, and cancelled export. Simulator/emulated viewport checks alone
do not establish physical-device behavior.
