# Intro

Load when creating or updating the user's global working profile or explaining
first-session onboarding. This menu performs no writes and loads no topic.

| Topic | Load for | Invocation |
| --- | --- | --- |
| Profile | Capture or revise durable working preferences | `$gobby intro references profile` |
| Onboarding | Resolve storage, installation and injection prerequisites | `$gobby intro references onboarding` |

The working profile is `<files_home>/USER.md` on the hub owner. It is global,
not repository content. `gobby-profiles` manages **build profiles**; use
[build profiles](../build/profiles.md) for that separate capability. There is no
personal-profile MCP tool. The supported profile transport is the owner's
`GET`/`PUT /api/files/user-md` or its local files-home path.

Use [profile](profile.md) for an actual intro request. Use
[onboarding](onboarding.md) if location, authentication or fresh-session injection
is uncertain. Tool calls used to inspect rules/configuration still require their
current schemas. Follow existing authorization boundaries for profile content;
the availability of a write route does not authorize unrelated deployment work.

Human starting point: [working profile](../../../../../../../../docs/guides/README.md#working-profile).
