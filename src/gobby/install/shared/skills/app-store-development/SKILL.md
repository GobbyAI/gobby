---
name: app-store-development
description: "Design or change Apple-app features with App Store policy implications: APIs, lifecycle, privacy, accounts, payments, user content, or dependencies. Use early for iOS/iPadOS and macOS feature decisions; ordinary edits need only applicable checks."
version: "1.0.0"
category: development
triggers: apple-policy, app-store-development, ios-privacy, storekit-policy
---

# App Store development

Preserve the intended feature while choosing supported platform mechanisms.
Initial scope is iOS/iPadOS apps and Safari web extensions, with explicit macOS
distinctions. Other platforms need their own documentation and availability checks.

1. Identify the changed behavior, OS/deployment target, extension type, distribution
   channel and relevant storefront. Inspect actual code/data flows before deciding.
2. Load [evidence](references/evidence.md) for consequential decisions:
   `get_skill_file(name="app-store-development", path="references/evidence.md")`.
   Recheck relevant live Apple policy during architecture decisions; record unknowns.
3. Load only applicable topics below, normally at most three references. Turn each
   concrete risk into a supported alternative and a focused verification step.
   An ordinary edit does not require a submission audit.

| Feature touches | Exact reference call |
| --- | --- |
| Public APIs, execution, downloaded code, dependencies | `get_skill_file(name="app-store-development", path="references/platform.md")` |
| Data, permissions, analytics, accounts/deletion | `get_skill_file(name="app-store-development", path="references/privacy-accounts.md")` |
| Payments, subscriptions, shared user content, special categories | `get_skill_file(name="app-store-development", path="references/commerce-content.md")` |

Use `gobby-skills` for these calls: lease `get_skill_file` with
`get_tool_schema` first when unleased. Read each result completely; follow
`page.next_cursor` using only `cursor` until null. If delivery fails, report the
missing evidence and continue only independent work, without inventing a rule.

For Swift implementation load `get_skill(name="swift")`. For native UI load
`get_skill(name="impeccable")`, read the project design contract, and load
`get_skill_file(name="impeccable", path="references/ios.md")` for iOS/iPadOS.
Keep design recommendations distinct from explicit rejection grounds.
For a consequential native UI finding cite the relevant
[Apple HIG](https://developer.apple.com/design/human-interface-guidelines/accessibility)
or exact project design requirement, and mark unread sources unverified.
For Safari work load `get_skill(name="safari-extension-development")` early.
For a requested release audit or rejection load `get_skill(name="app-store-review")`.

Report consequential findings with behavior, rule/limitation, URL, verification
date, platform/version/storefront, confidence and remedy. Label explicit Apple
requirements, platform limitations, engineering recommendations, and unresolved
interpretations separately. Never promise approval. Skill use grants no authority
to upload, submit, change App Store Connect, or communicate with Apple.
