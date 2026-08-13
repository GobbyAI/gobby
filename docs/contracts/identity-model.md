# Identity Model

Gobby's current identity hierarchy is:

1. User owns machines.
2. Machine creates or forwards sessions.
3. Session owns runtime state, task links, transcripts, memory links, and agent lineage.

The user seam lives only on `machines.owner_user_id`. Session, task, agent, memory,
and workflow tables should derive user ownership by joining `session.machine_id` to
`machines.id`, then reading `machines.owner_user_id`. Do not add parallel
`user_id` columns to those tables unless the identity contract changes.

## Machines

`machines.id` is an install-generated UUID primary key. The registry stores
optional descriptive metadata (`hostname`, `os`, `label`, `tailscale_name`)
plus `first_seen`, `last_seen`, and required UUID `owner_user_id`.

`owner_user_id` references `users.id` with restricted deletion. Installation
and authenticated enrollment establish ownership. Startup may idempotently
register the canonical local machine for the sole installed user. Hook and
session ingress only refresh known-machine metadata; an unknown UUID is never
auto-claimed.

## Daemon Access Credentials

Daemon auth establishes access to one operator-controlled daemon. The
install-scoped `local_cli_token` authorizes CLI, hook, MCP, HTTP, and direct
WebSocket clients. Canonical email credentials in `users` create
`gobby_session` browser sessions linked through `auth_sessions.user_id`.

The local token remains a machine-local access capability. Browser sessions
identify a canonical user. Neither path adds parallel user columns to task,
session, memory, agent, or workflow rows; those domains derive user identity
through machine ownership where needed.

See [Secrets Contract](./secrets.md#daemon-api-token) for token storage, header,
and rotation semantics.

## Sessions

`sessions.machine_id` is a nullable UUID foreign key to `machines.id` and remains
part of the session natural key. `NULLS NOT DISTINCT` keeps unattributed session
registration idempotent. Session APIs may filter by `machine_id` for multi-machine
views and shared Postgres hubs.

The daemon should not infer a remote client's user by writing user ownership onto
the session. The derivation is:

`session -> sessions.machine_id -> machines.id -> machines.owner_user_id`

## Future Fleets

Fleet daemons and nodes register as machines only with a required `owner_user_id`.
There is no pending or ownerless machine row. A node can host many sessions; a
user can own many machines; a shared stack can serve many users without changing
the session schema.
