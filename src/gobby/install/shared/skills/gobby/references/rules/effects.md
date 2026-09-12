# Rule effects

Load when choosing or configuring an effect. Inspect `get_rule` and validate
against `RuleEffect`. Use `block` for prevention, `set_variable` for state,
`inject_context` for guidance, `observe` for observations, and `mcp_call` for
authorized side effects. The guide covers all 15 types, including response
metadata, `run_command`, and trusted `proxy_hook` transformations.

`block` requires a reason. Native `tools` and `mcp_tools` (`server:tool`, including
`server:*`) are alternative selectors. Command selectors apply to shell matches or when no
tool selector is present; non-shell native and MCP matches skip command regexes.
Without tool selectors the effect matches the event broadly. Use shell regexes
only for command payloads. Double-quoted YAML needs doubled backslashes;
single-quoted YAML retains them literally. Read the guide's executable-segment,
heredoc and quoted-data masking rules before matching shell text.

`set_variable` requires a variable; specify the intended value even though the
model permits `None`. Expression-like strings evaluate; Jinja values render and
coerce first. Evaluation errors skip writes. Custom rules cannot write reserved
runtime variables. `delivery: on_receipt` stages supported tracking writes with
the delivered payload; eager persistence is default.

`inject_context` uses Jinja with event/context/helpers. `observe` appends to
`_observations`, category `general` by default. `set_display_content` supplies
display metadata. None proves instructions were read. `load_skill` emits a fetch
directive, not content: follow it through every cursor page. Exact references
load independently of the router and sibling topics.

`mcp_call` requires server/tool. Top-level string arguments render; do not assume
recursive rendering of nested containers. Calls defer by default. With
`inject_result=true`, `background=false`, and a dispatcher, execution is inline;
`block_on_failure`/`block_on_success` control inline outcomes. `success_variable`
requires inline result injection. Failure does not automatically stop subsequent
siblings; explicitly gate dependent work. Background calls cannot establish a
synchronous prerequisite.

`rewrite_input` renders updates and preserves Gobby MCP routing by merging into
inner arguments. Response effects require an adapter that consumes their metadata;
they do not create universal permission/retry behavior.

`run_command` sends hook JSON on stdin to an argv command, optionally a paired
skill/script. Missing executable, failure, timeout, or malformed output fails
open; it is not a blocking enforcement primitive. `proxy_hook` resolves a trusted
internal handler on `before_tool`, not arbitrary command/plugin execution.
Original-input denials precede it and transformed input is checked again.
Documentation or schema validation grants no executable authority.

Recover through effect diagnostics and isolated failure tests. Load
[enforcement](enforcement.md) for ordering and delivered acknowledgments.

Guide: [Effect notes](../../../../../../../../docs/guides/rules.md#effect-notes).

_Last verified: 2026-09-12_
