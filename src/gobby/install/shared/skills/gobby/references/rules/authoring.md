# Authoring rules

Load before creating or changing a rule. Discover installed names with
`list_rules`, then inspect the selected row with `get_rule`. Establish the
behavior to enforce or track, its trigger, conditions, effect, and audience.
Ask only for missing decisions; an existing task can supply them.

MCP rule creation requires `manage_workflow_definitions` or the operator's
terminal. Updating, toggling or deleting a rule also accepts
`manage_rule_enforcement`. Other seats can inspect rules and prepare YAML for an
authorized session; loading this reference does not grant mutation authority.

Use kebab-case names and groups as an authoring convention. Grouped YAML has
`group`, optional `tags`/`sources`/`audience`, and a named `rules` mapping.
YAML sync accepts single `effect` shorthand; stored/API bodies require a
nonempty `effects` list with at most one `block`. Row metadata (description,
enabled, priority, tags) is separate from the body. Lower priority runs earlier;
100 is the default. Early initialization, gates, then tracking/guidance is an
authoring convention, not a reserved priority range.

After schema discovery, this illustrates creation in isolated state; reading
it does not authorize execution:

```python
call_tool("gobby-workflows", "create_rule", {
    "name": "example-edit-observation",
    "definition": {
        "event": "after_tool",
        "group": "example-custom",
        "when": "event.data.get('tool_name') == 'Edit'",
        "effects": [{"type": "observe", "message": "Edit completed"}],
    },
}, session_id="#YOUR_SESSION")
```

`group` belongs inside `definition`. Creation validates the body, creates an
enabled global installed row, and rejects a live duplicate. `project_path`
selects export location, not row scope; `make_template` requests global export.
Dev mode skips auto-export. Confirm returned row and export separately: an
export warning does not undo a database write.

`update_rule` accepts selected row metadata or a complete replacement body,
not a partial body merge. Explicit metadata wins over embedded update metadata.
Empty updates and invalid bodies fail. Keep bundled customization in a distinct
custom rule and supported selectors; see [overrides](overrides.md).

Operator `gobby rules import FILE` accepts grouped `.yaml`/`.yml` and scopes
imports to the registered current project, otherwise globally. Inspect errors
and counts: multi-rule import can be partial. `gobby rules export --group GROUP`
emits one YAML document for review/backup; check scope before import.

Validate YAML syntax and the rule schema. The shared engine preflight checks
conditions, templates, regexes and effect preparation without running external
effects. Create, update, sync, restore and enable use the same validation boundary;
disabled rules are checked too. Missing runtime context produces unresolved warnings,
while invalid syntax or unsupported engine constructs reject the definition.

Do not add Python tests for a rule's matching policy, wording, exemptions or recovery
decisions, or copy those policies into Python or synthetic YAML fixtures. Users may
change or create rules without authoring tests. Bundled and user-created rules use
the same generic validation checks; engine and helper tests use independent synthetic
definitions to cover reusable mechanics. Read back the installed row and inspect
validation warnings before reporting success.
Never test executable or blocking examples against the user's live session.

Guide: [YAML and fields](../../../../../../../../docs/guides/rules.md#yaml-format).

_Version: 2.0.0_
_Last verified: 2026-11-03_
