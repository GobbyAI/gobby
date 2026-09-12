# Discover installed skills

Load when selecting instructions or diagnosing a missing skill.
Call `list_skills` for metadata or `search_skills(query="<task subject>")` for
ranked installed/project results. Search local first for repository conventions,
Gobby workflows, integrated tools, or unfamiliar project procedures; use hubs
for external language/framework knowledge when local guidance is insufficient.
Search proactively for unfamiliar technologies, domains and current tool usage;
do not rely on training data alone for language patterns or integrations.

Lists default to 50 results, search to `top_k=10`; these discovery endpoints
have no public cursor. Narrow category/tags/query or increase the requested
bound when needed. Internal skills are hidden unless `include_internal=true`;
exact `get_skill(name=...)` can load one. Search excludes disabled skills by
default; list can filter `enabled`. Passing a target session can restrict
results to its active skill names. A result or manifest supplies discovery
metadata, never completed instruction delivery.

Name-based loading prefers a real project-scoped row to global. A project row
pointing into the bundled template tree is rejected/ignored rather than allowed
to shadow fresh bundled content. Management by ID avoids same-name ambiguity.

If a name is absent, check spelling, scope, internal visibility and enabled
state before searching hubs. Do not infer a skill from an MCP service name.
Use HTTP/CLI listing only for operator management; it does not record a load.

Guide: [Skills](../../../../../../../../docs/guides/skills.md#internal-skills).

_Last verified: 2026-09-12_
