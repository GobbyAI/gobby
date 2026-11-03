# Rules

Load when authoring, inspecting, or diagnosing reactive hook policy. Start with
`gobby-workflows:list_rules(brief=true)` and `get_rule(name=...)`; fetch each
known tool's schema before its first call in the current context. Keep caller
session context on the outer `call_tool`. Installed rows and effective session
selectors determine enforcement; bundled YAML alone does not prove activation.

Choose an operation topic from the catalog: authoring, events, conditions,
effects, enforcement, overrides, or diagnostics. Menus execute nothing. Load
only applicable topics, normally at most three for one workflow. Loading this
overview does not satisfy an operation-specific reference requirement.

Establish intended behavior, event, condition, effect, and target audience before
changing policy. Validate YAML syntax and schema, then use the shared side-effect-free
engine preflight for bundled and user-created rules, including disabled definitions.
Keep Python tests focused on engine and helper mechanics with independent synthetic
definitions; never pin an individual rule's policy or copy it into a test fixture.
Rules react to events; pipelines own durable sequencing
and agents own worker lifecycle. Documentation grants no execution authority.

Use the six rule-management MCP tools for agent work. `gobby rules` and HTTP
management are operator interfaces. Recover missing names through installed
discovery; recover blocks by satisfying their prerequisites, not by disabling
policy or repeatedly retrying the same call.

Guide: [Rules](../../../../../../../../docs/guides/rules.md#public-tooling).

_Version: 1.0.0_
_Last verified: 2026-11-02_
