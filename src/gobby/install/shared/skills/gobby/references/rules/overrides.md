# Enabled state, scope, and customization

Load before changing enabled state, scope, or selectors. Inspect installed
provenance with `list_rules`/`get_rule`. `toggle_rule(name, enabled)` changes the
installed row, not one session. `update_rule` changes metadata or a full body.
`delete_rule` soft-deletes; a bundled `gobby` tag requires `force=true`, and sync
can restore it. Deletion is not a durable policy toggle.

Runtime loads enabled global rows plus the event project's rows, then filters
agent scope and audience. Current agent-definition selectors take precedence
over fallback `_active_rule_names`; absent fallback adds no filter. There is no
general per-session rule-override API here. Use authorized agent-definition
selector configuration, following the agents reference library.

Bundled definitions remain Gobby-owned. Use a distinct custom name and select it
intentionally. Do not customize the bundled installed body even where low-level
updates accept it. Current rule sync skips user rules that collide with bundled names;
do not assume `override: true` makes same-name rule YAML supported. Verify the
domain loader instead of copying another domain's override layout.

Operator files currently use `.gobby/workflows/rules/` and
`~/.gobby/workflows/rules/`. Project imports are scoped by registered checkout.
Public MCP creation exposes no project row-scope argument; `project_path` selects
export destination only. Source/export location alone does not prove scope.

Bundled sync refreshes managed definitions and removes absent managed rows in
scanned scopes. Enabled defaults apply until explicitly pinned; toggles pin user
intent. Live custom rows stay protected. Restore does not imply every user choice
resets. Read back after sync; inspect source files when deleted rules reappear.
Row updates need no restart; engine code changes need a coordinated restart.

Operator HTTP bulk-toggle filters by source (`installed` or `project`), not
project ID. Inspect `count`, `partial`, and `failures`. Avoid broad mutations for
a single-rule repair.

Guide: [Activation](../../../../../../../../docs/guides/rules.md#activation-model).

_Last verified: 2026-09-12_
