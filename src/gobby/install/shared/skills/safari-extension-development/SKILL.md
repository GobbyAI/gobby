---
name: safari-extension-development
description: "Design, implement or review Safari extensions on iOS/iPadOS and macOS. Use for website access, extension APIs, lifecycle, messaging, storage, containing-app handoff and distribution boundaries."
version: "1.0.0"
category: development
triggers: safari-extension, safari-web-extension, native-messaging, safari-app-handoff
---

# Safari extension development

First identify Safari web extension versus macOS Safari app extension versus
Share/Action extension. Record OS/Safari/deployment target, manifest version,
extension point and distribution channel. Their APIs and lifecycles differ.

For consequential findings load the shared contract:
`get_skill_file(name="app-store-development", path="references/evidence.md")`.
Use current Apple documentation and exact SDK availability; do not infer iOS
capability from macOS samples or archived Share/Action examples.

| Changed behavior | Exact reference call |
| --- | --- |
| API, permissions, native messaging, app launch, distribution | `get_skill_file(name="safari-extension-development", path="references/capabilities.md")` |
| Shared state, transfers, lifecycle, interrupted export | `get_skill_file(name="safari-extension-development", path="references/storage-lifecycle.md")` |
| Privacy, analytics, capture disclosures | `get_skill_file(name="app-store-development", path="references/privacy-accounts.md")` |

All calls use `gobby-skills`. Lease `get_skill_file` with `get_tool_schema` when
unleased, load one page per result, and follow `page.next_cursor` using only
`cursor` until null. Normally load at most three relevant references at a time.
Report failed loads and unresolved evidence without inventing a platform rule.

For Swift code load `get_skill(name="swift")`. For native iOS UI load
`get_skill(name="impeccable")` and
`get_skill_file(name="impeccable", path="references/ios.md")` with project context.
For requested submission review load `get_skill(name="app-store-review")`.

Deliver a capability decision tied to the affected feature, a supported fallback,
and platform-specific tests. Record behavior, authority URL, verification date,
platform/version/storefront, confidence and remedy; distinguish policy, technical
limitations, recommendations and unknowns. No approval promise or authority to
upload, submit, mutate App Store Connect or contact others comes from this skill.
