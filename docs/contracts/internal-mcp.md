# Internal MCP family contract

Internal `gobby-*` servers belong to the hub. Family crates publish descriptors
through `gobby_core::mcp`; gdaemon composes one `ToolRegistry` and owns the gateway.
This contract defines that shared seam. It does not activate a server or implement
gateway policy, backend selection, transport, or node execution.

## Family-author checklist

1. Keep handlers beside the family's manager and REST service in its existing
   family crate. Depend inward on `gobby-core`, never on gdaemon.
2. Implement `ToolFamily::server` to return one `ToolServer` with its public name,
   description, and tools. Preserve existing public server and tool names. Names
   must be nonempty and contain no whitespace, control characters, or `/`.
3. Define each request with `Deserialize` and `schemars::JsonSchema`. Bind it with
   `ToolDefinition::new(name, description, |context, request| async { ... })`.
   Return a JSON value or structured `ToolError`; do not implement gateway policy
   inside the handler. Inject the same service used by the REST routes through
   the handler's closure.
4. Supply brief text and a derived output schema where applicable. Mark a tool
   read-only only when running it cannot cause an externally visible mutation.
   The default is false. This flag controls safe comparison eligibility.
5. Construct the registry once at startup. Duplicate server names, duplicate
   tool names within a server, invalid names, caller-context schema exposure, and
   invalid input schemas fail construction. The registry consumes descriptors
   and exposes shared references, stable discovery ordering, and cached identity.
6. Prove discovery/schema equality, argument rejection before deserialization,
   result/error parity, and read-only classification with focused contract tests
   before requesting activation. Keep parity fixtures committed.

## Schema and compatibility

The request type is the schema source of truth. The shared generator emits
Draft 7 JSON Schema and removes the root `$schema` and generated `title`.
Input schemas follow Serde deserialization; output schemas follow serialization,
including directional skip and rename attributes. Nested definitions and
constraints remain intact. Discovery returns the exact
normalized value compiled into the startup validator. Every call validates the
raw JSON before Serde deserialization or handler execution; failures return
`ToolError::InvalidArguments`. Custom deserializers may reject further constraints
and return the same error category. Unknown routes return `UnknownTool`.

Before moving an existing Python server to Rust, make the derived schema
JSON-object-equal to the current Python discovery schema, including optionality,
defaults, descriptions, and constraints. Adjust request annotations rather than
maintaining a second handwritten schema. Preserving argument meaning and result
envelopes is also required. A deliberate schema or description change changes
identity and requires the normal freshness/lease update; there is no independent
numeric schema version.

Definition identity matches Python `compute_schema_hash`: SHA-256 over canonical
`{"description":description,"input_schema":schema}` JSON, truncated to the first
16 lowercase hexadecimal characters. Keys sort recursively; arrays retain order;
separators are compact. Strings use Python `ensure_ascii` escapes, including
UTF-16 surrogate pairs, and floats use Python's JSON number formatting. Brief
text, output schemas, read-only metadata, and server names do not enter the hash.
The registry caches both canonical JSON and hash once at startup.

## Caller context and policy ownership

`ToolContext` carries the resolved caller session, project/scope, authenticated
principal, agent run, machine, intent, and origin. It deliberately implements
neither serialization nor schema derivation. Supply it as the handler's separate
first argument; never embed caller context in request or output types. Startup
also rejects schemas exposing a `ToolContext` definition, title, or reference.

A request body's `session_id` or `project_id` remains ordinary target input and
may identify a different session or project. The gateway authenticates the
caller, supplies omitted schema-required context defaults, runs one before-policy
pass, re-resolves and revalidates modified arguments, dispatches once, and runs
one after-policy pass. Missing required agent caller context fails closed. The
registry itself neither authenticates callers nor grants authority. Gateway
adapters translate these typed errors into the existing public MCP error shape.

## Per-server backend states

Backend selection belongs to the gateway under each public server name in
bootstrap `front_door.routes`; absent entries default to `proxy`.

| State | Dispatch and comparison |
| --- | --- |
| `proxy` | Invoke Python's loopback compatibility endpoint directly against its internal registry. |
| `compare` | Return Python's result. Compare native discovery/schema and read-only calls; mutations execute only in Python. Prove mutation parity in isolated tests. |
| `native` | Dispatch only to the Rust family handler. |

Move one server at a time, preserving its public name and making rollback
independent of other servers. The gateway owns the single policy pass across all
states; the Python compatibility endpoint must not add another pass. Remove
Python registration only after native activation smoke passes.

## Hub-only placement

In standalone mode the local daemon is both hub and node. In hub mode the hub
serves every internal family, including worktrees, clones, merge, agents, and
config. Nodes expose no local `gobby-*` registry and hold no hub datastore
credentials. `gmcp` uses the existing node relay to reach the hub. When a hub
handler needs filesystem, terminal, checkout, or process effects, it sends the
bounded typed command to the selected node; authority, records, policy, and
caller context remain hub-owned.
