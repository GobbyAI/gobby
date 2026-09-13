# Variables

Use variables to configure defaults and coordinate state within a session. First
choose the layer: a **definition** supplies a default; a **session value** overrides
that default; an **agent-step value** belongs to an existing step instance.
Changing one layer does not reset the others. Tool schemas are authoritative for
parameters, and a documented operation grants no permission to bypass enforcement.

## Variable Lifecycle

Enabled global definitions are overlaid by enabled definitions for the session's
project, then by stored session values. Each key is replaced as a whole; nested
objects are not recursively merged. Reading does not materialize defaults into
storage. Some atomic list/set mutations intentionally materialize their layered
input before saving it.

Definition changes advance the variables revision; the runtime defaults cache is
keyed by project and revision. An existing stored override still wins after a
default changes. A disabled project definition contributes no default, so an
enabled global definition of that name can become visible again.

The database is the authority for installed state. A bundled YAML value alone
never proves what is enabled or what a session currently sees.

## Initialization

Bundled files under `src/gobby/install/shared/workflows/variables/` use grouped YAML:

```yaml
tags: [session-defaults, initialization]
variables:
  example_limit:
    value: 8
    description: Example retry limit
    enabled: true
```

Each entry becomes a typed `session_variable_defaults` row. This is a format
example, not a claim that `example_limit` is installed. Bundled sync refreshes
managed rows, preserves user/custom rows and pinned enabled toggles, and restores
managed deleted rows. A successful authoritative scan can retire missing managed
entries; partial/error scans must not be treated as complete cleanup.

### Runtime Resolution

`SessionVariableManager.get_variables()` layers project/global defaults beneath
stored values. Agent activation computes identity and selection metadata, applies
explicit non-underscore `workflows.variables`, then fills selected defaults only
where that activation payload has no value. Bootstrap preserves existing session
values; persona changes and spawned-agent initial values have their own merge
boundaries. See [agents](./agents.md) before changing a persona or spawn payload.

### Variable Selectors

Agent `workflows.variable_selectors` controls which defaults are materialized by
activation. Omitted/null means all enabled defaults; include/exclude selectors
resolve a set of names, with exclusions winning. It is **not a read-access filter**:
ordinary session reads still layer all applicable enabled defaults.

```yaml
workflows:
  variable_selectors:
    include: ["tag:session-defaults"]
    exclude: ["name:example_limit"]
```

Explicit agent variables starting with `_` are skipped. Do not use selectors or
agent overrides to defeat runtime-owned enforcement.

## Mutation

Agents use the top-level `get_variable` and `set_variable` MCP tools for session
values, without a progressive-discovery lease. Always pass the intended session.
Omit `name` on reads for the full effective map. A named read returns both `value`
and `exists`: a present null value differs from an absent key.

```python
get_variable(session_id="#123", name="example_limit")
# Mutating example: use only for an authorized session setting.
set_variable(session_id="#123", name="example_limit", value=4)
```

Session writes accept JSON-compatible values. Strings representing booleans,
null/none, integers or floats are coerced; JSON-looking strings containing arrays
or objects are not parsed. Send actual arrays/objects. Values are literals here,
not rule expressions. `session_task` references are resolved to task UUIDs in the
target session's project; unresolved references fail without writing.

### Expression Detection

Rule `set_variable` effects are a separate authoring surface. Strings with known
expression indicators are evaluated by `SafeExpressionEvaluator`; other strings
remain literal. Jinja-containing values are rendered and coerced on a separate
branch, **not** fed through a second expression evaluation. Failed expressions do
not write a value. See [set_variable effects](./rules.md#set_variable) and the
implementation in `src/gobby/workflows/engine/effects.py` for the current heuristic.

### Mutation Visibility

An accepted rule effect updates the evaluation snapshot for subsequent rules in
the same pass. Public read-then-write calls are not a compare-and-swap or atomic
increment API; concurrent clients can overwrite each other's whole-key updates.
Runtime list/set helpers serialize their mutations under a session advisory lock.

### Auto-Managed Variables

Runtime observers and rules own task attribution, validation/error tracking,
discovery leases, loaded instructions, tool-block counters and handoff delivery.
Never manufacture those values to satisfy a gate. Public runtime setters reject
reserved names and prefixes; the authoritative list is
`src/gobby/workflows/reserved_variables.py`. Examples include `unlocked_tools`,
`open_tool_errors`, `grok_pending_*`, `step_workflow_complete`, and `enforce_*`.
Requirement-list variables require arrays of non-empty instruction identifiers;
empty arrays are allowed by the value validator, not permission to remove gates.

`session_edited_files` records session history; `session_dirty_files` and its
checkout map track unreconciled edits. Hooks do not sample a git baseline;
`baseline_dirty_files` can be an unsampled marker until explicit capture. Task
validation comes from transcripts, not variable flags. Commits preserve evidence;
a later attributed edit makes an earlier validation stale.

## Using Variables in Conditions

Conditions can access flattened names or `variables.get('name', default)`. Prefer
`.get` for optional values. Flattened names share a namespace with runtime context;
do not assume a user value can replace a helper or event field. The safe evaluator
uses an AST, not Python `eval`. Block-condition errors fail closed; ordinary
non-block effects skip on condition errors. See
[variable safety](./workflow-rules.md#variable-safety-in-when) and
[condition expressions](./rules.md#condition-expressions).

## Using Variables in Templates

Context injection and block reasons use Jinja templates. Use defaults for optional
values and keep secrets out of injected text. See
[inject_context](./rules.md#inject_context) and [block](./rules.md#block).

## LazyBool Pattern

Some condition values defer expensive work until their boolean value is needed.
Boolean short-circuiting avoids evaluating an unused lazy value. This is runtime
context, not a persisted variable type. See `LazyBool` in
`src/gobby/workflows/safe_evaluator.py`.

## Built-in Condition Helpers

Use the verified [rule condition reference](./rules.md#condition-expressions).
Helper inventories and full schemas belong with their implementation rather than
being copied into variable defaults.

### Task Helpers

Task state/tree checks read lifecycle state; a variable assignment cannot close a task.

### Stop Signal Helper

Pending stop signals belong to agent lifecycle operations, not a reset flag.

### MCP Tracking Helpers

MCP outcome helpers distinguish successful calls, failures and returned values.
A list/menu does not prove an operation or instruction load occurred.

### Progressive Discovery Helpers

Schema leases and discovery observations are different state. Get a known unleased
tool's schema directly; list tools only when discovering a name or auditing inventory.

### Other Helpers

See `condition_helpers.py`, `engine/templating.py` and `safe_evaluator.py` under
`src/gobby/workflows/` for task, path, message and instruction predicates.

## Default Variables Reference

Discover actual definitions with `gobby-workflows:list_variables` and inspect one
with `get_variable_definition`. Read the session separately with top-level
`get_variable`. Compare `enabled`, `project_id`, `source`, tags and value before
explaining behavior. Definition lists may include multiple project scopes; the MCP
list schema exposes `enabled`, not a project filter.

A read-only audit on 2026-09-12 found global `max_stop_attempts` value `8`, enabled,
source `installed`, tagged `gobby`; this is dated evidence, not a permanent default
or proof that any stop rule is enabled. Runtime state can override it.

### Internal Variables (Set by Rules/Engine)

Internal state has per-event, per-turn, per-context or session lifetime. Ordinary
resume/restart preserves leases and existing variables; context loss clears
instruction/schema tracking through the normal reset flow. A found-work shirk
alert is session-lifetime, not a turn-start reset. Do not clear task ownership,
dirty-path history, pending errors, feedback acknowledgments or handoff receipts
by hand. See [session boundaries](../contracts/session-boundary.md) and
[engine reset behavior](./workflow-rules.md#turn-start-reset).

## Managing Variables

### CLI

`gobby variables get [NAME] --session ID --json` and
`gobby variables set NAME VALUE --session ID` are **operator** commands using local
DB access. Agents use the tracked runtime tools. Omitted session uses CLI current
session resolution; explicit IDs are safer for diagnosis. These commands expose
session scope only. CLI parsing handles booleans, null/none and numbers, then falls
back to strings; it does not decode list/object JSON. See [CLI](./cli-commands.md#variables).

### MCP Tools

`gobby-workflows` exposes definition `list_variables`, `get_variable_definition`,
`create_variable`, `update_variable`, `delete_variable`, and `export_variable`.
Fetch each schema before use. Top-level runtime get/set are separate and expose
session scope only on the stdio carrier; the daemon HTTP MCP server also accepts
`scope=step`. `get_step_status` is shared agent-step diagnostic guidance.

The registered definition create/update schemas currently expose `value` as a
**string**, even though internal functions accept richer values; those functions
do not apply runtime string coercion. Do not send `"false"` expecting a boolean
default. Use typed user YAML or the authorized operator definition HTTP surface
when a typed default is needed. Definition update treats `None` as unchanged;
it is not a public MCP null-reset mechanism.

Creation rejects existing names, including bundled collisions. It creates a
global custom user row, so file sync never refreshes or prunes it: `project_path`
selects the export destination, **not** the DB project scope, and `make_template`
selects the global export directory.
Update changes value/description; deletion soft-deletes a definition, not a
session key. Export returns a single named YAML document. It is not a complete
backup of scope, tags or enabled state. Auto-export may be skipped (no destination
or development mode) or fail after DB success; verify the file separately.

### Defaults and Overrides

Customize through a user definition, project override or authorized session value.
Bundled source files and Gobby-owned installed definitions remain Gobby-owned;
permissive low-level mutators and deletion `force` do not grant authority to edit
them in place. Keep the [template contract](../../src/gobby/install/shared/AGENTS.md).
Variables' actual user loader uses `.gobby/workflows/variables/` and
`~/.gobby/workflows/variables/`, not an inferred generic install path.

A project user file uses the single-definition format:

```yaml
name: example_limit
type: variable
variable: example_limit
value: 4
description: Project retry limit
```

The imported-definition loader supplies scope from its root and preserves a global
row when creating a same-named project row. Global imports use global scope.
Explicit exports and auto-exports use this named format; grouped bundled YAML is
not accepted by this user loader. To recover an older grouped auto-export, export
its live definition again or convert each entry into a separate named file, then
validate/reload in isolation. Reload is shared workflow maintenance, not an
automatic consequence of listing or reading a file.

### HTTP and Scope

Operator `/api/variables` definition routes provide list/create, update, toggle,
soft-delete and template restore. Create supports `project_id`; list supports
project/enabled/deleted filters. Update distinguishes omitted `value` from null.
There is no single-definition GET route: use list or MCP get-definition. There
are no variable move-scope or undelete HTTP routes. Template restore resolves a
live row and resets value/description; it does not undelete a row or reset sessions.

`POST /api/sessions/{session_id}/variables/get` and `/set` use
`scope: session|step` (default session). Step scope reads/writes the existing
agent-step instance's variable map; no instance is an error, not an instruction
to create one. Agent credentials are bound to their claimed session and cannot
use another session or the operator definition API. See
[HTTP endpoints](./http-endpoints.md#variable-semantics).

### Resets and Recovery

There is no public generic unset/reset-session-variables operation. Setting null
stores a present null override and continues masking a default. Setting a current
default value copies it into an override; later default edits will not follow.
Use the owning lifecycle tool for runtime-managed reset state, and ordinary
`set_variable` only for an authorized user setting. Never delete a definition to
try to clear a stored session value.

If a value seems wrong: confirm target session and scope, read its effective
value and `exists`, inspect project/global definitions and enabled state, then
inspect agent activation or persisted overrides. For a reserved-variable error,
use the owning task/agent/handoff operation. For missing definitions or import
errors, check the single named YAML shape, destination scope and sync result.
Do not repair a value issue by resetting the user's whole session.

## File Locations

| Path | Authority |
|---|---|
| `src/gobby/mcp_proxy/tools/workflows/_variables.py` | Definition and runtime operations |
| `src/gobby/workflows/state_manager.py` | Layered session values and atomic mutations |
| `src/gobby/storage/definitions/variables.py` | Definition scope, revision and persistence |
| `src/gobby/workflows/sync_variables.py` | Grouped bundled defaults |
| `src/gobby/workflows/imports.py` | Named global/project user imports |
| `src/gobby/workflows/template_writer.py` | User-file serialization |
| `src/gobby/servers/routes/variable_definitions.py` | Operator definition API |
| `src/gobby/servers/routes/sessions/variables.py` | Session-bound runtime HTTP API |

## See Also

- [Workflows overview](./workflows-overview.md)
- [Rules](./rules.md)
- [Agents](./agents.md)

_Last verified: 2026-09-12_
