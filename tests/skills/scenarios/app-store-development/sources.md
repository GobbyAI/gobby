# Apple source and provenance evidence

Task #22302. Checked 2026-09-13. Scope: initial iOS/iPadOS apps and Safari web
extensions, with macOS distinctions. This log records authoring evidence; future
architecture/release decisions must recheck live sources.

| Source | Observed evidence and use |
| --- | --- |
| [Review Guidelines](https://developer.apple.com/app-store/review/guidelines/) | Live page readable. Used section references for APIs, execution, commerce, extension containers, permissions and privacy; linked exceptions remain live decision inputs. 4.4 explicitly accommodates help/settings containers. |
| [Account deletion](https://developer.apple.com/support/offering-account-deletion-in-your-app/) | Live page readable; directly linked website completion is expressly permitted. |
| [Tracking and data use](https://developer.apple.com/app-store/user-privacy-and-data-use/) | Live page readable; tracking classification depends on actual data/SDK use, not the word analytics. |
| [App Privacy Details](https://developer.apple.com/app-store/app-privacy-details/) | Live page readable; distinguish collection, recipients, linkage and purpose. |
| [Safari permissions](https://developer.apple.com/documentation/safariservices/managing-safari-web-extension-permissions) | Apple search result contained complete relevant permission guidance, including activeTab, optional permissions and MV3 host_permissions. |
| [Safari native messaging](https://developer.apple.com/documentation/safariservices/messaging-between-the-app-and-javascript-in-a-safari-web-extension) | Apple search result contained relevant article sections: three independent components, JS-to-handler direction, macOS reverse messaging and no containing-iOS-app-to-JS messaging. |
| [Safari creation](https://developer.apple.com/documentation/safariservices/creating-a-safari-web-extension) and [distribution](https://developer.apple.com/documentation/safariservices/distributing-your-safari-web-extension) | Relevant Apple article sections readable in search. Simulator/unsigned development and signed distribution distinguished. |
| [NSExtensionContext.open](https://developer.apple.com/documentation/foundation/nsextensioncontext/open(_:completionhandler:)) | Browser returned JS shell; linked Markdown failed in web tool (unsupported content type). Direct curl of .md succeeded. Documentation says support is determined by extension point; this does not prove a Safari foreground launch route. |
| [Persistent background content](https://developer.apple.com/documentation/webkit/wkwebextension/haspersistentbackgroundcontent) | JS shell; direct .md curl succeeded. Current WebKit document limits persistent content to macOS and describes errors on iOS/iPadOS. API property availability itself is newer than initial Safari web extensions; do not use its availability as Safari's minimum version. |
| [Background transfers](https://developer.apple.com/documentation/foundation/downloading-files-in-the-background) | JS shell; linked Markdown failed in web tool, direct .md curl succeeded. File-backed uploads, delegate callbacks, separate-process transfer and relaunch handling confirmed. |
| [Shared session container](https://developer.apple.com/documentation/foundation/urlsessionconfiguration/sharedcontaineridentifier) | Apple search content confirms extension session requires valid shared-container identifier. |
| [Archived shared-container guidance](https://developer.apple.com/library/archive/documentation/General/Conceptual/ExtensibilityPG/ExtensionScenarios.html) and [architecture](https://developer.apple.com/library/archive/documentation/General/Conceptual/ExtensibilityPG/ExtensionOverview.html) | Readable, explicitly archived. Supports historical process/synchronization model; not treated as current Safari API availability. |
| [Protected resources](https://developer.apple.com/documentation/uikit/requesting-access-to-protected-resources) | Relevant Apple sections readable; actual resource and dependency behavior drive purpose-string checks. |
| [Required-reason APIs](https://developer.apple.com/documentation/bundleresources/describing-use-of-required-reason-api) | Relevant Apple article readable in search; reasons must reflect actual use in owning bundles. |
| [Manifest data and privacy report](https://developer.apple.com/documentation/bundleresources/describing-data-use-in-privacy-manifests) | Relevant Apple article readable in search, including archive-generated report aggregation. |
| [SDK requirements](https://developer.apple.com/support/third-party-SDK-requirements/) | Live page readable; use its current SDK list and conditions, not a copied list. |
| [Upcoming requirements](https://developer.apple.com/news/upcoming-requirements/) and [submission overview](https://developer.apple.com/help/app-store-connect/manage-submissions-to-app-review/overview-of-submitting-for-review/) | Live pages readable. Linked as refresh points; no submission deadline or SDK floor frozen into skills. |

Two guessed SFSafariWebExtensionHandler documentation URLs returned internal errors.
They were not used to claim an API or prohibition. A future investigation must
use real current symbols and extension-point evidence. No release archive or
signed device build was inspected for this skill-authoring task.

## External coverage comparisons

- [safaiyeh/app-store-review-skill](https://github.com/safaiyeh/app-store-review-skill/blob/main/SKILL.md):
  compared coverage for privacy, metadata, runtime and platform breadth. The raw
  LICENSE was fetched and identifies MIT, copyright 2026 safaiyeh. No wording,
  examples or code was adapted, so this implementation contains no MIT-derived
  substantial portion requiring a bundled notice. In particular, fixed reviewer
  hardware claims and blanket deprecated-API/native-functionality assertions were
  not adopted. Future adaptation must retain the MIT notice and verify each claim.
- [dpearson2699/swift-ios-skills](https://github.com/dpearson2699/swift-ios-skills/blob/main/skills/app-store-review/SKILL.md):
  compared topic coverage only. Raw LICENSE confirmed PolyForm Perimeter 1.0.0
  with noncompete restrictions. No text, examples or implementation copied/adapted.
  Original guidance was authored from Apple sources above.

Existing Swift and Impeccable references are reused by exact loading calls; their
instructions and attribution files remain unchanged.
