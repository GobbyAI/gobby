# Machine and project identity

Load before interpreting machine ownership or diagnosing cross-machine paths.
Discover `gobby-hub:get_machine_id` and fetch its schema, then call with `{}`.
Use the returned `machine_id` as the connected daemon's identity, including
when the agent cannot read its Gobby home.

The resolver reads the active `$GOBBY_HOME/machine_id` (normally
`~/.gobby/machine_id`), caches it, and can generate a missing identity.
Never copy another machine's ID as part of distributing credentials. A
filesystem error is an identity-resolution failure, not proof that the
machine has no sessions. Restore access to the correct Gobby home through
the operator before retrying; do not invent an ownership UUID.

Projects have shared UUIDs. Their ordinary checkout roots belong to
`(machine_id, project_id)` registrations, with `.gobby/project.json` identifying
the project. A project known to the hub need not have a checkout on this
machine. `_personal` is a hub-owned files tree, not a registered Git checkout.

Session `machine_id` identifies its owner. Seeing a foreign session does not
authorize reading its absolute paths or controlling its terminal. Canonical
session identity excludes machine ownership; do not manufacture a second
session to transfer ownership. Use the sessions guidance for handoff and
transcript access, and source-control guidance for managed workspaces.

See [machine and project ownership](../../../../../../../../docs/guides/shared-stack.md#machine-and-project-ownership)
and [files and client credentials](../../../../../../../../docs/guides/hub-install-contract.md#files-and-client-credentials).
