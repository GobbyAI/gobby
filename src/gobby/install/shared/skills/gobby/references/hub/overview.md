# Hub

Load when identifying the connected machine, selecting another project, or
querying activity across projects. `$gobby hub references` lists topics without
running their operations.

Discover `gobby-hub` tools with `list_tools`, then fetch each needed schema
before its first call. The five tools provide identity and queries; they do
not create projects, move checkouts, transfer claims, or control other machines.
`get_machine_id` may initialize a missing local identity file through the
daemon's normal identity resolver.

1. Load `identity.md` to distinguish machine ownership from project identity.
2. Load `projects.md` to discover project names/UUIDs or diagnose a checkout.
3. Load `queries.md` for cross-project task and session retrieval.
4. Load `aggregates.md` before interpreting hub counts.

Query results describe the connected hub, not every Gobby installation.
Retain project UUIDs when acting on results; a local `#N` alone can identify
the wrong project's task or session. Use the destination service's explicit
project context and normal lifecycle tools for subsequent work.

On `success=false`, inspect the error before interpreting absent results.
`Hub database not available` requires restoring the connected daemon's
database service; changing a query's project does not repair it. Installation,
lease promotion, secret distribution, and datastore recovery are operator
procedures in the [shared-stack guide](../../../../../../../../docs/guides/shared-stack.md).
Do not bypass a standby daemon's lease fence.
