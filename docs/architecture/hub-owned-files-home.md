# Hub-owned files home

Wiki vaults, the `_personal` life-admin tree, and `USER.md` are hub
semantics. In a two-or-more-machine install there is one copy, on the hub
host. Workstations and laptops do not grow a second `~/wiki`,
`~/.gobby/personal`, or `USER.md`.

This document is the reviewed contract for #20238. Implementation is the
sibling epic **Implement hub-owned files home** under #17435.

## Why this exists

evolution.md story B: a home server runs the hub daemon and the data stack;
nodes own checkouts, agents, and PTYs. Story C: the machine never holds hub
state.

Today those stories are only half-true. Tasks, memory, and session metadata
already live in the shared datastores. The working profile, topic vaults,
personal vault, notes, reminders, and chat uploads still resolve to
daemon-home or `$HOME` paths on whichever machine happened to write them.

That is the same class of bug as treating a checkout path as project identity.
The files are not execution artifacts. They are user content that must
outlive a closed laptop.

## The Docker-volume question

Putting this tree “on the hub, like the volumes” is the right ownership
instinct. A named Docker volume on the current compose stack is the wrong
object.

Compose runs only PostgreSQL, Qdrant, and FalkorDB. There is no Gobby files
container. A volume attached to those services has no consumer: they will
not serve Markdown, and a remote-mode laptop cannot mount Hub-PC’s volume.

Shared-remote-stack’s line “the hub never executes client-filesystem work”
is about opaque foreign checkout paths (`/Users/josh/Projects/gobby`), git,
worktrees, and agents. It is not a ban on the hub hosting *its own*
document tree.

M0 (“shared databases, per-machine full daemons”) is also the wrong
template. These files are story B / Stage 3 semantics, not M0 execution.

**Locked mechanism**

- One bind directory on the hub host (for example `/var/lib/gobby/files`).
- Compose may *declare* that path as `gobby_files` so pack / hub-backup /
  restore inventory it beside datastore volumes. That is a lifecycle entry,
  not “put Markdown inside `gobby_postgres_data`.”
- The only POSIX writers are hub-host processes that hold the singleton:
  the live `datastore_mode: local` daemon (`role=daemon`; later `gdaemon`
  in `hub` mode) or an authorized stopped-daemon campaign
  (`role=maintenance`), including direct `gwiki` mutators. Nodes never
  write the tree.
- Node / remote-mode daemons never create `~/.gobby/personal`, never default
  `~/wiki`, and never cache a canonical copy. They read and write through
  the hub owner’s HTTP surfaces, proxied if the UI still talks to a local
  daemon.
- Standalone (story A) uses the same tree on the one machine. It is still
  not `$GOBBY_HOME/personal`.
- No file-sync product and no per-node mount. A Tailscale/NFS view on every
  laptop recreates “a wiki on every machine.”

Hosted story C is the same contract with gobby.ai holding the tree.
Customers receive the hub API, not a Docker volume.

## Three data classes

| Class | Examples | Where |
| --- | --- | --- |
| Hub semantics | tasks, memory, sessions, `USER.md`, wiki vaults, `_personal` files, chat uploads | Hub host / hub API |
| Machine execution | checkouts, worktrees, clones, agents, PTYs, Telegram receive-side until Stage 3 | Owning node |
| Datastores | Postgres, Qdrant, FalkorDB | Hub compose stack (#19585) |

## Tree

```text
<hub-files>/                    # bind dir on hub host only
  USER.md                       # global working profile
  _personal/                    # life-admin; not a git repo; not a vault
    .gobby/project.json         # id PERSONAL_PROJECT_ID, name _personal
    notes/
    reminders/
    attachments/
      <project-id>/<id[:2]>/<id>/<filename>
  wiki/                         # wiki home; not a vault
    wikis.json                  # gwiki registry (paths relative to wiki home)
    personal/                   # personal vault
    <topic>/                    # topic vaults (flattened off ~/wiki/topics/)
```

Reserved names at `<hub-files>`: `USER.md`, `_personal`, `wiki`.
Reserved vault name: `personal`. Creating a topic or project vault named
`personal`, `_personal`, or `wiki` is a typed refusal.

Wiki home is an ordinary folder. It must not carry `_gwiki/scope.json`.
A child directory is a vault only when it carries `_gwiki/scope.json`.

Project production vaults (`<checkout>/wiki`) stay checkout-adjacent until
#18779. That cutover writes the redesigned vault to
`wiki/<project.name>` with `scope.json` identity `project:<uuid>`. #19664
fixture vaults keep using explicit `gwiki --out` paths.

## Resolution

- Bootstrap on the hub-local daemon: `files_home: <absolute path>`. Required
  when `datastore_mode: local`. Never default to `$GOBBY_HOME/...`.
- Remote-mode / node bootstrap has no `files_home` and must not mkdir one.
  File routes proxy to the hub owner (`hub_daemon_url`)
  or the client talks to the hub daemon directly.
- `read_user_profile_content` on the hub owner reads `<hub-files>/USER.md`.
  On a node it fetches that file from the hub owner. Absent file → empty
  string. It does not read `$GOBBY_HOME/personal/USER.md`.
- `personal_project_path()` on the hub owner is `<hub-files>/_personal`.
  Nodes do not resolve a personal filesystem root. `_personal` is not a git
  checkout and is not registered in `project_checkouts`.
- A present local bootstrap resolves wiki to `<hub-files>/wiki`.
  `wiki.hub_path` / `GOBBY_WIKI_HUB` may match that path exactly after
  normalization; any other override is a typed refusal. Topic resolution
  is `<hub-files>/wiki/<topic>` (today’s `topics/` prefix is removed).
  Personal scope is `<hub-files>/wiki/personal`, not `_personal/wiki`.
  Arbitrary overrides are missing-bootstrap fixture contexts only.
- Chat attachment bytes reconstruct as
  `<hub-files>/_personal/attachments/<project_id>/<id[:2]>/<id>/<filename>`.
  Absolute `local_path` is not canonical. Nodes upload and download through
  the hub owner.
- Missing hub tree is a typed failure. No silent recreate under
  `$GOBBY_HOME`.

Node access uses the existing wiki and attachment HTTP surfaces, not a
local tree:

- `GET /api/wiki/status|search|read|graph|pages|backlinks`
- `POST /api/wiki/index`
- chat attachment upload / download / delete

`wiki_ask` is gone (#20322). That does not remove the file surfaces.

## Migration

One-shot, hub-local, no dual-write. The operator provisions an existing
`files_home` root; writers never create that root.

1. Complete graph preflight of every present source/destination pair.
2. Publish every present source into an absent destination (profile,
   personal marker, leftover personal children, topic vaults, attachments,
   wiki registry).
3. Seed only still-missing baseline children
   `<hub-files>/{USER.md,_personal/{notes,reminders,attachments},wiki}`.
4. Rewrite published vault metadata and `wikis.json` to wiki-home-relative
   children.
5. Leave `<checkout>/wiki` for #18779.
6. Leave `~/.gobby/comms_attachments` for Stage 3.
7. After a successful move, ignore the old locations. No compatibility
   reader.

## Implementation owner

#20238 is this document. Code lives in **Implement hub-owned files home**
(#20330), a sibling epic under #17435. #18779 is blocked on that epic, not
on the design. #19651 remains checkout identity only and must not invent a
per-machine personal root.

`src/gobby/storage/projects.py` is shared with #19651. Files-home
implementation may proceed in parallel only while `PERSONAL_PROJECT_ID`
stays checkout-free. The path helper is owned here.

Expected implementation surfaces (not this task):

- `src/gobby/paths.py` — `get_files_home()` on the hub owner only
- `src/gobby/config/bootstrap.py` — `files_home` for local mode
- `src/gobby/storage/projects.py` — `personal_project_path` /
  `ensure_personal_project`
- `src/gobby/hooks/event_handlers/_session_start/profile.py`
- `src/gobby/servers/routes/chat_attachments.py`
- `crates/gwiki/src/scope.rs`
- intro skill and onboarding copy
- compose / pack / hub-backup inventory for the bind dir

## Out of scope

- Implementing #19651
- #19664 information model and fixture vaults
- #19585 datastore topology beyond a files bind inventory
- #18779 empty-vault production activation
- Stage 3 hub/node split, Telegram-vs-cron routing, comms attachments
- Hosted tenancy beyond keeping the contract hub-API-shaped

## Validity against 2026-08-16 `0.5.0`

Checked after the plan (written 2026-08-14 09:52) against HEAD
`bc8348ad2` plus the two days of commits after it.

Still true, same source bytes:

- `personal_project_path()` → `(gobby_home or get_gobby_home()) / "personal"`
- `read_user_profile_content()` → that path plus `USER.md`
- `_attachment_dir()` → `$GOBBY_HOME/projects/<id>/attachments/<id[:2]>/<id>`
- `ensure_personal_project()` still upserts `projects.repo_path`
- `resolve_topic` still uses `<hub>/topics/<name>` and `~/wiki` as default
- `BootstrapConfig.daemon_url` still exists
- `/api/wiki` still exposes status, index, search, read, graph, pages,
  backlinks

Changed around the plan, not against it:

- #19651 is now an epic with P1–P6 children (#20297–#20302). It still must
  not invent a personal checkout. The design already required that.
- #18902 (daemon-native gcode/gwiki) is closed. gwiki talks to hub
  datastores through grants; vault *files* are still machine-local. That
  makes a hub-owned tree more necessary, not less.
- `wiki_ask` was removed. Node access uses the remaining `/api/wiki/*`
  file routes.

No cited implementation file was rewritten. The contract stands.
