# Hub-owned files home

The `_personal` life-admin tree, `USER.md`, and chat attachments belong to
one hub host. Nodes own checkout files, worktrees, agents, and PTYs; they
access hub-owned files through the hub API.

This is the surviving files contract from #20238 and #20330 after legacy
wiki retirement (#21771). The archived Git tag
`legacy-wiki-before-retirement-21771` retains the former vault contract.

## Storage and ownership

The files home is an existing absolute directory on the hub host, selected
by local bootstrap `files_home`. A standalone or laptop hub normally uses
`$GOBBY_HOME/files`; a dedicated server may use `/var/lib/gobby/files`.
Provision the directory before installation. Writers never silently
recreate a missing files-home root.

The directory is separate from PostgreSQL, Qdrant, and FalkorDB volumes.
Pack, hub-backup, and restore include it through their files-home archive
contract. Datastore containers do not serve these files.

Local hub processes hold the role-bearing singleton in `gobby.pid.lock`:

- The live daemon holds `role=daemon`.
- Stopped-daemon campaigns, including `gobby files migrate`, pack, unpack,
  hub-backup, and restore, hold `role=maintenance` while writing files.

`gobby start` acquires the daemon claim, or converts an install claim into
a service-start reservation, before starting managed services. `gobby stop`
refuses a maintenance claimant so it cannot interrupt a files campaign.

Remote bootstrap has no `files_home`. The owner origin is `hub_daemon_url`;
`daemon_url` identifies the current process. Remote file access uses the
owner's HTTP routes rather than a second canonical local copy.

## Current tree and routes

```text
<files_home>/
  USER.md
  _personal/
    .gobby/project.json
    notes/
    reminders/
    attachments/
      <project-id>/<id[:2]>/<id>/<filename>
```

`_personal` is not a Git checkout. Its marker uses `PERSONAL_PROJECT_ID`,
and identity creation does not register it in `project_checkouts`.

`read_user_profile_content()` reads `<files_home>/USER.md` on the owner;
a remote caller uses `/api/files/user-md`. An absent profile returns empty
content. Chat attachment upload, download, and deletion use
`/api/chat/attachments`; attachment bytes reconstruct from the project and
attachment identifiers rather than treating stored absolute paths as
canonical. Shared hub forwarding preserves authentication, streaming,
response handling, and proxy-loop protection.

`/api/files` remains the machine-local checkout browser. The future
`/api/hub/user`, `/api/hub/chat/attachments`, and top-level
`attachments/<project-id>/` destination is tracked in `ROADMAP.md`; current
clients use the routes and layout above.

## Migration

`gobby files migrate` runs only on the hub with the daemon stopped and an
already provisioned `files_home`. Upgrade or stop remote writers first.
Copy any remaining node-local profile, personal files, and attachments to
the hub's legacy source locations before migration; this command does not
collect files from other machines.

The campaign discovers `$GOBBY_HOME/personal` and project attachment
sources under `$GOBBY_HOME/projects`, preflights every source/destination
pair, and publishes only absent or identical destinations. Divergent
content refuses migration. The source is retired only after publication
and verification. Attachment locators are reconciled through the existing
storage manager.

Only missing baseline children are seeded: `USER.md`, the personal marker,
and `_personal/{notes,reminders,attachments}`. No wiki home, scope registry,
or vault is created. Legacy wiki recovery and deletion belong to the
separate inventory-bound retirement procedure.

## Source boundaries

- `src/gobby/paths.py`: owner-only files-home and profile resolution.
- `src/gobby/config/bootstrap.py`: local/remote bootstrap ownership.
- `src/gobby/storage/projects.py`: checkout-free personal identity.
- `src/gobby/files_migrate.py`: stopped-daemon migration and preservation.
- `src/gobby/servers/routes/files.py`: profile access.
- `src/gobby/files_home_proxy.py`: shared hub forwarding.
- `src/gobby/servers/routes/chat_attachments.py`: attachment HTTP access.
- `src/gobby/cli/hub_backup/files_home.py`: scoped files-home archive handling.

Communications attachments and the broader hub/node execution split remain
separate platform work. Canonical session summaries, revisions, handoffs,
and transcript archives retain their independent storage contracts.
