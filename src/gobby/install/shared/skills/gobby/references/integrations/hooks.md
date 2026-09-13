# Hooks

Load when wiring provider hooks, diagnosing missing events, or reviewing Git
verification stages. Start with operator `gobby hooks list`, `gobby hooks status
--json`, and `ghook --diagnose --cli=NAME --type=EVENT`. Listings do not prove a
rule is enabled; inspect its installed row through the rules capability.

1. Identify the provider, exact native event, managed project, binary and daemon
   endpoint. Use the installed provider carrier; do not invent event aliases.
2. Diagnose token availability, project identity and recent delivery failures.
   Native envelopes require schema 1 and `hook-response.v1`; use ghook rather
   than replacing authenticated durable delivery with an ad hoc POST.
3. Distinguish transport failure from an explicit daemon denial. Lifecycle
   criticality is provider-specific. Stop is not transport-critical; planned
   shutdown, ingress backpressure, and adapter timeout have separate recovery.
4. Preserve spool files, receipts and quarantine evidence. Successful provider
   output precedes inbox settlement; when the daemon returns a receipt, it must
   be acknowledged. HTTP 2xx alone is insufficient. Repair the receiver before
   retrying or clearing state.
5. For Git verification, inspect `.gobby/project.json` and run `gobby hooks run
   pre-commit --dry-run` before an authorized stage run. `hooks test` evaluates
   a synthetic event and may trigger real effects; use an isolated daemon.

`gobby hooks disable`/`enable` are operator controls on the project-root boolean
flag. `GOBBY_HOOKS_DISABLED=1` disables native dispatch through the environment.
Do not use either to evade agent gates. Unmanaged projects skip native dispatch;
AGY workspace resolution and managed environment identity can establish scope.
Codex Interrupt is interruption evidence and emits no hook stdout.

Use semantic `turn_start`/`turn_end` for rules. Raw provider Stop is not an agent
termination command: a spawned agent still ends through `end_agent_run`.
Read [hook schemas](../../../../../../../../docs/guides/hook-schemas.md),
[ghook recovery](../../../../../../../../docs/guides/ghook-user-guide.md#troubleshooting),
and [hook commands](../../../../../../../../docs/guides/cli-commands.md#gobby-hooks-and-gobby-webhooks).
