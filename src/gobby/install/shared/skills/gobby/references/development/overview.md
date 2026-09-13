# Development

Load for contributor obligations, channel implementation, or native component
navigation. This overview performs no operation and loads no topic.

| Topic | Load for | Invocation |
| --- | --- | --- |
| Obligations | Preflight, focused tests and completion evidence | `$gobby development references obligations` |
| Channels | Build or validate a communications/provider adapter | `$gobby development references channels` |
| Native components | Select Rust ownership, contracts and validation | `$gobby development references native-components` |

For a development request, load obligations first, then the applicable topic.
Keep reusable language, design, testing and review methodology in standalone
skills. Load their complete entrypoints through `gobby-skills`; references use
`get_skill_file` after its schema gate and require every cursor page.

For Apple-app feature decisions with policy implications load
`get_skill(name="app-store-development")` early. For Safari extension work load
`get_skill(name="safari-extension-development")`. For a requested release audit or
rejection load `get_skill(name="app-store-review")`; ordinary edits need only
applicable development checks. These remain dynamically discovered standalone skills.

This capability supplies contributor procedures. It has no dedicated internal
MCP service: `mcp_proxy/tools/internal.py` implements registry infrastructure.
Public operations remain with their owning capability. Tool schemas determine
parameters; a guide, menu or available endpoint grants no extra authority.

Human entrypoints: [testing](../../../../../../../../docs/guides/testing.md),
[adapter fidelity](../../../../../../../../docs/guides/adapter-fidelity.md), and
[native development](native-components.md).
