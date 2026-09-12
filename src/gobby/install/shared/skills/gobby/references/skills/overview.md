# Skill management

Load when discovering, loading, authoring, installing, or managing instruction
bundles. Use `gobby-skills`; a connected MCP server is not an installed skill.
Start with installed discovery, then load the selected entrypoint completely.
Tool schemas own arguments; bootstrap `list_skills`, `search_skills`, and
`get_skill` are exempt from schema discovery. Other known tools require a
current-context schema lease before their first call.

Use the catalog's topic menu for the operation: discovery/loading/levels and
references for instruction delivery; hubs/installation for acquisition;
authoring/scripts/lifecycle for changes. A menu is metadata and executes nothing.
Load only applicable topics, normally at most three for one workflow. The router
and this overview do not satisfy an operation-specific reference requirement.

Keep caller session context on the outer `call_tool`. Respect project overrides
and internal visibility. Review provenance before mutations; templates do not
prove installed state. On lookup failure, inspect the error and discover the
actual installed name. Never treat a truncated or failed page as loaded.

Guide: [Skills](../../../../../../../../docs/guides/skills.md#what-a-skill-is).

_Last verified: 2026-09-12_
