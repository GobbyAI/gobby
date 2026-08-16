Plan artifact: `.gobby/plans/hub-owned-files-home.md`

# Hub-Owned Files Home

**Plan ID:** hub-owned-files-home
**Epic:** #20330
**Contract:** `docs/architecture/hub-owned-files-home.md`

## Overview
`kind: framing`

Implement the reviewed architecture: one bind directory on the hub host holds `USER.md`, `_personal`, and the wiki home. The hub-local daemon (`datastore_mode: local`) is the only POSIX writer. Remote daemons never create `~/.gobby/personal` or `~/wiki`; they proxy file HTTP to `hub_daemon_url`.

## Constraints
`kind: framing`

Decision Record (settled in #20238 / `docs/architecture/hub-owned-files-home.md`; not reopened here):

1. One hub-host bind directory. Not `$GOBBY_HOME/personal`. Not a named Docker volume inside Postgres/Qdrant/Falkor.
2. Compose/pack/hub-backup may inventory that bind dir as `gobby_files` for lifecycle only.
3. Hub-local daemon is the only process that touches the POSIX tree.
4. No per-node mount, Syncthing, or replica. Nodes use hub HTTP.
5. `files_home` is required in `datastore_mode: local`. `hub_daemon_url` is required in `datastore_mode: remote`. The inverse is a hard `BootstrapConfigError`.
6. Tree: `<files_home>/{USER.md,_personal/{.gobby,notes,reminders,attachments},wiki/{wikis.json,personal,<topic>}}`.
7. Wiki home is not a vault. Flatten today’s `~/wiki/topics/<name>` to `<files_home>/wiki/<name>`. Personal vault is `wiki/personal`.
8. Reserved names: `USER.md`, `_personal`, `wiki` at files_home; reserved vault name `personal`.
9. No dual-read of old daemon-home paths after a successful migrate.
10. `_personal` stays checkout-free. Do not `register` a `project_checkouts` row. Path helper is this plan’s; #19651 must not invent a per-machine personal root.
11. `<checkout>/wiki` stays until #18779. Fixture `gwiki --out` stays #19664.
12. `~/.gobby/comms_attachments` stays until Stage 3 / #17488.
13. No full pytest suite. `GOBBY_TEST_PROTECT=1` plus focused pytest, Ruff, Mypy, and the named gwiki/gcore tests. Every touched hand-maintained production source stays under 1,000 lines.

## D1: Production project vault cutover
`kind: deferred`

```yaml
deferral:
  task_ref: "#18779"
  reason: "Empty-vault production activation writes project vaults into wiki/<project.name> after this home exists. This plan only names the destination."
  owner: "repository-intelligence"
  original_acceptance_items:
    - D1.1
```

## D2: Fixture vaults and information model
`kind: deferred`

```yaml
deferral:
  task_ref: "#19664"
  reason: "Isolated gwiki --out fixture vaults and the redesigned information model stay in the wiki-output epic."
  owner: "repository-intelligence"
  original_acceptance_items:
    - D2.1
```

## D3: Comms attachments and machine-local execution
`kind: deferred`

```yaml
deferral:
  task_ref: "#17488"
  reason: "Telegram receive-side files and cron-vs-Telegram routing are Stage 3 machine-local execution, not hub user documents."
  owner: "remote-stack"
  original_acceptance_items:
    - D3.1
```

## P1: Bootstrap and path contract
`kind: framing`

**Goal:** Local-mode daemons resolve one absolute `files_home`. Remote-mode daemons have no files tree and know the hub owner URL.

### 1.1 Add files_home and hub_daemon_url resolution [category: code]
`kind: deliverable`

Targets:
- `src/gobby/config/bootstrap.py::BootstrapConfig`
- `src/gobby/config/bootstrap.py::BootstrapConfig.to_config_dict`
- `src/gobby/config/bootstrap.py::load_bootstrap`
- `src/gobby/config/bootstrap.py::_parse_datastore_mode`
- `src/gobby/config/bootstrap.py::_parse_optional_daemon_url`
- `src/gobby/paths.py::*` — scope-reason: add get_files_home, require_files_home, and typed files-home errors beside get_gobby_home
- `src/gobby/install/shared/config/bootstrap.yaml::*` — scope-reason: add files_home to the installed local-mode template
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: regenerate after the installed bootstrap template gains files_home
- `crates/gcore/src/bootstrap.rs::*` — scope-reason: tolerate files_home the way datastore_mode is already tolerated
- `tests/config/test_files_home.py`

Add bootstrap fields:

```python
files_home: str | None = None          # absolute path; local mode only
hub_daemon_url: str | None = None      # http(s) origin of the files-owner daemon
```

`load_bootstrap` rules:

- `datastore_mode: local`: `files_home` required, non-empty, absolute (no `~`, no relative). Missing/relative/`~` → `BootstrapConfigError`. `hub_daemon_url` if present → `BootstrapConfigError` (this process is the owner).
- `datastore_mode: remote`: `hub_daemon_url` required. Parse it as an HTTP(S) origin: scheme `http` or `https`, host required, optional port, no userinfo, no query, no fragment, path empty or `/` only. Store the origin with no trailing slash. Any other shape → `BootstrapConfigError`. `files_home` if present → `BootstrapConfigError`.
- Never default `files_home` to `$GOBBY_HOME` or `$HOME/.gobby/...`.

```python
def get_files_home() -> Path | None:
    """Local-mode files_home, or None on a remote-mode daemon."""

def require_files_home() -> Path:
    """Return files_home or raise FilesHomeNotOnThisDaemonError / FilesHomeError."""
```

`get_files_home` reads the already-loaded bootstrap (same source as `get_gobby_home` uses env/home). Do not mkdir. Missing directory is `FilesHomeError` at `require_files_home`, not a silent create.

Rust `bootstrap.rs` must tolerate the new keys the way it already tolerates `datastore_mode` (endpoint parse does not fail). Add `reads_bootstrap_with_files_home`.

`to_config_dict` emits both fields so `DaemonConfig` can carry them. If `DaemonConfig` needs explicit fields, add them in this leaf; do not leave bootstrap values stranded.

**Acceptance:**

- 1.1.1 - Local bootstrap requires absolute `files_home` and refuses `hub_daemon_url`. test: `tests/config/test_files_home.py`.
- 1.1.2 - Remote bootstrap requires `hub_daemon_url` and refuses `files_home`. test: `tests/config/test_files_home.py`.
- 1.1.3 - `require_files_home` returns the configured path in local mode and raises `FilesHomeNotOnThisDaemonError` in remote mode. test: `tests/config/test_files_home.py`.
- 1.1.4 - Installed bootstrap template documents `files_home` for local mode. file: `src/gobby/install/shared/config/bootstrap.yaml`.
- 1.1.5 - Rust bootstrap parse ignores `files_home` without dropping daemon endpoint defaults. test: `crates/gcore/src/bootstrap.rs::reads_bootstrap_with_datastore_mode`.
- 1.1.6 - Remote `hub_daemon_url` accepts `http(s)://host[:port]` and refuses userinfo, query, fragment, and a non-root path. test: `tests/config/test_files_home.py`.

## P2: Hub-owner personal tree and USER.md
`kind: framing`

**Goal:** On the hub owner, personal identity and the working profile live under `files_home`. Nodes do not materialize a personal directory.

### 2.1 Retarget personal identity and USER.md [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/storage/projects.py::personal_project_path`
- `src/gobby/storage/projects.py::ensure_personal_project`
- `src/gobby/storage/projects.py::ensure_personal_project_identity`
- `src/gobby/hooks/event_handlers/_session_start/profile.py::read_user_profile_content`
- `src/gobby/hooks/event_handlers/_session_start/profile.py::seed_user_profile_content`
- `tests/storage/test_project_manager.py::TestConstants.test_personal_project_path`
- `tests/storage/test_project_manager.py::TestPersonalProjectEnsure.test_ensure_personal_project_creates_folder_and_repo_path`
- `tests/hooks/test_session_user_profile.py::*` — scope-reason: retarget every profile fixture off $GOBBY_HOME/personal

```python
def personal_project_path(gobby_home: Path | None = None) -> Path:
    """Hub-owner _personal directory. Raises FilesHomeNotOnThisDaemonError on a node."""
    return require_files_home() / "_personal"
```

`ensure_personal_project_identity` writes `<files_home>/_personal/.gobby/project.json` with `id=PERSONAL_PROJECT_ID`, `name=_personal`. It does not mkdir under `$GOBBY_HOME/personal`. It does not call checkout `register` / `rebind`.

`ensure_personal_project` upserts the sentinel row. It must not create a `project_checkouts` row. If `projects.repo_path` still exists (pre-#19651 P6), write the hub `_personal` path as unread campaign data on the hub owner only — never a per-machine `$GOBBY_HOME/personal`. After that column is gone, stop writing it. Do not add a compatibility reader for the old path.

`read_user_profile_content` on the hub owner reads `require_files_home() / "USER.md"`. Missing file → `""`. It does not read `$GOBBY_HOME/personal/USER.md`. Node behavior is § 5.1; this leaf may raise `FilesHomeNotOnThisDaemonError` on a node and leave the fetch to 5.1, or return `""` if 5.1 has not landed — pick the raise so a node cannot silently look local. Update `seed_user_profile_content` to propagate empty on that error until 5.1 wires the fetch.

Do not create `notes/` / `reminders/` here except as empty dirs from the migrate leaf.

**Acceptance:**

- 2.1.1 - `personal_project_path` is `<files_home>/_personal` and never `$GOBBY_HOME/personal`. test: `tests/storage/test_project_manager.py::TestConstants.test_personal_project_path`.
- 2.1.2 - Identity marker is written under files_home and checkout register is not called. test: `tests/storage/test_project_manager.py::TestPersonalProjectEnsure.test_ensure_personal_project_creates_folder_and_repo_path`.
- 2.1.3 - Hub-owner profile reads `<files_home>/USER.md` only. test: `tests/hooks/test_session_user_profile.py::test_read_user_profile_content_reads_personal_user_md`.
- 2.1.4 - Node `read_user_profile_content` does not read a daemon-home file. test: `tests/hooks/test_session_user_profile.py::test_read_user_profile_content_reads_personal_user_md`.

## P3: Chat attachments
`kind: framing`

**Goal:** Chat upload bytes reconstruct under `_personal/attachments` on the hub owner.

### 3.1 Reconstruct chat attachment paths [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/chat_attachments.py::_attachment_dir`
- `src/gobby/servers/routes/chat_attachments.py::upload_attachment`
- `src/gobby/servers/routes/chat_attachments.py::get_attachment_content`
- `src/gobby/servers/routes/chat_attachments.py::delete_attachment`
- `tests/servers/websocket/test_chat_attachments.py::*` — scope-reason: retarget every attachment path assertion off $GOBBY_HOME/projects

```python
def _attachment_dir(project_id: str, attachment_id: str) -> Path:
    return (
        require_files_home()
        / "_personal"
        / "attachments"
        / _safe_path_part(project_id, "project")
        / attachment_id[:2]
        / attachment_id
    )
```

Stop treating DB `local_path` as a machine-absolute canonical location. Persist a files_home-relative path or reconstruct from `(project_id, attachment_id, filename)` on every read. Prefer reconstruct so laptop/desktop path strings never leak into the hub DB.

Remote-mode upload/download is § 5.1. This leaf’s `_attachment_dir` raises `FilesHomeNotOnThisDaemonError` on a node.

Do not move `~/.gobby/comms_attachments`.

**Acceptance:**

- 3.1.1 - Upload writes under `<files_home>/_personal/attachments/<project>/<id[:2]>/<id>/`. test: `tests/servers/websocket/test_chat_attachments.py`.
- 3.1.2 - Download/delete resolve the same reconstructed path and do not require a stored absolute `local_path`. test: `tests/servers/websocket/test_chat_attachments.py`.
- 3.1.3 - Node `_attachment_dir` does not create `$GOBBY_HOME/projects/.../attachments`. test: `tests/servers/websocket/test_chat_attachments.py`.

## P4: Wiki home
`kind: framing`

**Goal:** On the hub owner, topic and personal vaults are children of `<files_home>/wiki`. The home itself is not a vault.

### 4.1 Point gwiki and daemon wiki scope at files_home/wiki [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `crates/gwiki/src/scope.rs::resolve_hub_path`
- `crates/gwiki/src/scope.rs::resolve_topic`
- `crates/gwiki/src/scope.rs::default_hub_path`
- `crates/gwiki/src/scope.rs::resolves_global_topic`
- `crates/gwiki/src/scope.rs::rejects_invalid_topic_names`
- `crates/gwiki/src/models.rs::validate_topic_name`
- `src/gobby/servers/routes/wiki.py::_resolve_scope`
- `src/gobby/servers/routes/wiki.py::_gateway`
- `tests/wiki/test_wiki_files_home.py`

`resolve_hub_path` order:

1. `GOBBY_WIKI_HUB` if set (tests/explicit override).
2. Else `wiki.hub_path` / `gwiki.hub_path` config if set.
3. Else, when this process is the hub owner, `<files_home>/wiki`.
4. Else do **not** fall back to `~/wiki`. Error: configure `GOBBY_WIKI_HUB` or run on the files owner.

`resolve_topic` uses `hub.join(topic)` — not `hub.join("topics").join(topic)`. `wikis.json` stays at the wiki home. Reserved topic name `personal` is allowed only as the personal vault; creating a second topic named `personal` is a typed refusal. Refuse `_personal` and `wiki` as topic names. The refusal is the same typed `WikiError::InvalidScope` family `rejects_invalid_topic_names` already uses for `.` / `..` / separators. `validate_topic_name` is the shared guard. Daemon `_resolve_scope` maps that error at the topic entry point and does not construct a local gateway for a reserved name.

Table-driven cases for `personal`, `_personal`, and `wiki`: topic creation/resolution returns the reserved-name error at both the Rust resolver and `_resolve_scope`. An explicit personal-scope request (project id `PERSONAL_PROJECT_ID` / `_personal`) still resolves exactly to `<files_home>/wiki/personal`.

Personal scope (project id `PERSONAL_PROJECT_ID` / `_personal`) resolves to `<files_home>/wiki/personal`, never `_personal/wiki`. Project production scope still uses checkout-adjacent `resolve_vault_dir(project_root)` until #18779.

Daemon `_resolve_scope` / `_gateway` must use the same hub path for topic and personal scopes. A topic request on the hub owner must not look in `~/wiki/topics`.

Wiki home must not carry `_gwiki/scope.json`. Creating it is a bug.

**Acceptance:**

- 4.1.1 - Topic `foo` resolves to `<files_home>/wiki/foo`, not `.../wiki/topics/foo`. test: `crates/gwiki/src/scope.rs::resolves_global_topic`.
- 4.1.2 - Default hub path is files_home/wiki on the owner and does not silently become `~/wiki`. test: `tests/wiki/test_wiki_files_home.py`.
- 4.1.3 - Personal wiki scope is `<files_home>/wiki/personal`. test: `tests/wiki/test_wiki_files_home.py`.
- 4.1.4 - Daemon topic/personal routes use that same home. test: `tests/wiki/test_wiki_files_home.py`.
- 4.1.5 - Topic names `personal`, `_personal`, and `wiki` are reserved-name errors at the resolver and daemon topic entry; explicit personal scope still resolves to `<files_home>/wiki/personal`. test: `crates/gwiki/src/scope.rs::rejects_invalid_topic_names`.

## P5: Node access
`kind: framing`

**Goal:** A remote-mode daemon never opens a local files tree. Its wiki, attachment, and profile surfaces proxy to the hub owner.

### 5.1 Proxy file HTTP to hub_daemon_url [category: code] (depends: P3, 4.1)
`kind: deliverable`

Targets:
- `src/gobby/hooks/event_handlers/_session_start/profile.py::read_user_profile_content`
- `src/gobby/hooks/event_handlers/_session_start/profile.py::seed_user_profile_content`
- `src/gobby/servers/routes/wiki.py::_resolve_scope`
- `src/gobby/servers/routes/wiki.py::_read`
- `src/gobby/servers/routes/wiki.py::_write_call`
- `src/gobby/servers/routes/chat_attachments.py::upload_attachment`
- `src/gobby/servers/routes/chat_attachments.py::get_attachment_content`
- `src/gobby/servers/routes/chat_attachments.py::delete_attachment`
- `src/gobby/utils/daemon_client.py::DaemonClient`
- `src/gobby/utils/daemon_client.py::DaemonClient.from_url`
- `src/gobby/utils/daemon_client.py::DaemonClient.call_http_api`
- `src/gobby/utils/durable_file.py::durable_replace`
- `src/gobby/servers/routes/hub_files_proxy.py`
- `tests/servers/routes/test_hub_files_proxy.py`
- `tests/hooks/test_session_user_profile.py::*` — scope-reason: retarget every profile read off daemon-home and add the hub-fetch and hub-write cases

On `datastore_mode: remote`, profile, wiki, and attachment consumers share one hub request path: `DaemonClient.from_url(hub_daemon_url)` (existing daemon auth headers) plus a raw sibling on `DaemonClient` for non-JSON bodies. Do not add a second HTTP client stack. Join as `{origin}{path}` after the stored origin (no trailing slash). Bounded timeout (the existing `DaemonClient` timeout, overridable per call). Forward method, path, query, body, and auth. Preserve upstream status, raw bytes, `Content-Type`, and `Content-Disposition`. Network/auth/timeout failures are typed errors, never a local-file fallback.

- `read_user_profile_content` GET `/api/files/user-md`. 404/empty body → `""`.
- `write_user_profile_content` (same module as `read_user_profile_content`): hub-local mode uses `durable_replace` on `require_files_home() / USER.md`; remote mode PUT `/api/files/user-md` with `{"content": "..."}`. Never mkdir a node-local personal tree.
- Wiki routes that would touch a vault proxy the inbound method/path/query/body to `/api/wiki/...` and return the hub status/body. Do not construct a local `GwikiGateway` for topic or personal scopes.
- Chat attachment upload/download/delete proxy to the matching hub routes, including multipart upload and binary download. Do not call `require_files_home` on the node.

Hub-owner surfaces in `hub_files_proxy.py` (or a sibling files route module under 1,000 lines):

- `GET /api/files/user-md` → `{"content": "<profile text or empty>"}` from `require_files_home()` plus the profile filename.
- `PUT /api/files/user-md` authenticated; body `{"content": "..."}`; atomic replace via `durable_replace`. Empty content is a valid write (clears the working profile).

The wiki router module is already large — do not add proxy helpers there if that crosses 1,000; extract.

No caching of the working profile or vault bytes on the node disk.

Intro/onboarding: hub-local writes the files_home profile through `write_user_profile_content`. Remote mode uses the PUT path. It must not instruct a node to write `USER.md` or `_personal` under `$GOBBY_HOME`.

**Acceptance:**

- 5.1.1 - Remote profile seed fetches hub USER.md and never reads `$GOBBY_HOME/personal/USER.md`. test: `tests/hooks/test_session_user_profile.py::test_seed_user_profile_content_merges_profile`.
- 5.1.2 - Remote wiki topic/personal requests are proxied and do not create `~/wiki`. test: `tests/servers/routes/test_hub_files_proxy.py`.
- 5.1.3 - Remote attachment upload/download/delete proxy and do not write `$GOBBY_HOME/projects`. test: `tests/servers/routes/test_hub_files_proxy.py`.
- 5.1.4 - Hub `GET /api/files/user-md` returns the files_home profile. test: `tests/servers/routes/test_hub_files_proxy.py`.
- 5.1.5 - Hub PUT writes USER.md atomically; remote `write_user_profile_content` updates the hub copy and creates no node-local USER.md or personal directory. test: `tests/hooks/test_session_user_profile.py`.
- 5.1.6 - Origin join, timeout/auth errors, multipart upload, and binary download preserve status, bytes, Content-Type, and Content-Disposition. test: `tests/servers/routes/test_hub_files_proxy.py`.

## P6: Migrate and inventory
`kind: framing`

**Goal:** One hub-local move of existing bytes; pack/backup see the bind dir; docs match the new paths.

### 6.1 Add hub-local files migrate [category: code] (depends: P4)
`kind: deliverable`

Targets:
- `src/gobby/cli/files.py`
- `src/gobby/files_migrate.py`
- `tests/cli/test_files_migrate.py`

Hub-local only (`require_files_home`). Command: `gobby files migrate`. No dual-write. No start-time implicit migrate.

Preflight: discover every present source/destination pair first. If any destination exists and is not already the complete expected migrated layout, refuse with zero source mutations. Only after that pass apply the nine-step sequence in `docs/architecture/hub-owned-files-home.md` section Migration verbatim: skip missing sources; refuse remote mode. Destinations use relative registry entries (no absolute laptop paths). Checkout vaults and comms attachment storage stay where that section leaves them.

After a successful move, personal and topic vault metadata `root` values point at the new wiki home children. The wiki registry contains only wiki-home-relative child paths.

Idempotent: a second run is success only when sources are gone and the destination already is the complete expected layout. Partial failure must not invent a reader for the old locations.

**Acceptance:**

- 6.1.1 - First migrate moves the six source classes into the contract tree. test: `tests/cli/test_files_migrate.py`.
- 6.1.2 - Second migrate is a no-op success. test: `tests/cli/test_files_migrate.py`.
- 6.1.3 - Remote mode refuses. test: `tests/cli/test_files_migrate.py`.
- 6.1.4 - Checkout wiki and comms_attachments are untouched. test: `tests/cli/test_files_migrate.py`.
- 6.1.5 - A non-migrated destination conflict refuses before any source mutation. test: `tests/cli/test_files_migrate.py`.
- 6.1.6 - After a successful migrate, personal and topic scope metadata name the new wiki-home roots. test: `tests/cli/test_files_migrate.py`.
- 6.1.7 - After a successful migrate, the wiki registry holds only wiki-home-relative child paths. test: `tests/cli/test_files_migrate.py`.
- 6.1.8 - Second-run success requires the complete expected migrated layout, not a partial tree. test: `tests/cli/test_files_migrate.py`.

### 6.2 Inventory bind dir and update operator docs [category: docs] (depends: 6.1)
`kind: deliverable`

Targets:
- `src/gobby/cli/pack.py::pack`
- `src/gobby/cli/pack.py::_do_pack`
- `src/gobby/cli/pack.py::unpack`
- `src/gobby/cli/pack.py::_archive_would_overwrite`
- `src/gobby/cli/pack.py::_safe_archive_target`
- `src/gobby/cli/hub_backup/_stores.py::tar_volumes`
- `src/gobby/cli/hub_backup/cli.py::_run_backup`
- `src/gobby/cli/hub_backup/cli.py::_archive_volumes`
- `src/gobby/cli/hub_backup/cli.py::restore_hub_backup`
- `src/gobby/data/docker-compose.services.yml::*` — scope-reason: declare the files_home bind as a lifecycle entry
- `crates/gcore/assets/docker-compose.services.yml`
- `src/gobby/install/shared/skills/intro/SKILL.md`
- `docs/architecture/hub-owned-files-home.md`
- `docs/guides/system-requirements.md`
- `tests/cli/test_hub_files_restore.py`

Declare `gobby_files` as a **bind** of the configured `files_home` (not a named volume attached to postgres/qdrant/falkor). No service in compose consumes it except backup/pack helpers that archive the bind path.

`pack` includes the bind directory as `gobby/files/` in the tarball when `files_home` is set. Do not add `personal/` from `$GOBBY_HOME` to `PACK_FILES`. `unpack` extracts `gobby/files/` members into configured `files_home` (via `_safe_archive_target`), not under `$GOBBY_HOME/files`. `_archive_would_overwrite` treats those members as files_home collisions and keeps the existing confirm/`--force` policy.

`HUB_VOLUMES` stays datastore named volumes. `_run_backup` / `_archive_volumes` take a separate bind-archive step for `files_home`. `restore_hub_backup` recognizes that archive member and restores it into configured `files_home` with the same confirm/`--force` collision policy unpack uses. Do not append `gobby_files` to `HUB_VOLUMES`.

Intro skill and system-requirements: hub-local profile path is `<files_home>/USER.md`. Remote intro uses the § 5.1 PUT path, not `~/.gobby/personal/USER.md`.

**Acceptance:**

- 6.2.1 - Pack archives `files_home` and not `$GOBBY_HOME/personal`. file: `src/gobby/cli/pack.py`.
- 6.2.2 - Compose/hub-backup treat `gobby_files` as a bind-dir lifecycle entry, not a datastore named volume. file: `src/gobby/data/docker-compose.services.yml`.
- 6.2.3 - Intro skill writes `<files_home>/USER.md` on the hub owner and documents the remote PUT path. file: `src/gobby/install/shared/skills/intro/SKILL.md`.
- 6.2.4 - Pack unpack and hub-backup restore put USER.md, a `_personal` attachment, and a wiki file back under configured `files_home` and apply the existing overwrite policy. test: `tests/cli/test_hub_files_restore.py`.

## V2 End-to-end verification
`kind: verification`

Focused proof, hub-owner plus remote-mode:

- Local bootstrap + `require_files_home`.
- Personal marker and USER.md under files_home.
- Attachments reconstruct under `_personal/attachments`.
- Topic/personal wiki paths under `files_home/wiki`.
- Remote daemon proxies and writes no daemon-home copies.
- Reserved topic names refuse; explicit personal scope still resolves.
- Remote USER.md write updates the hub copy only.
- Pack/hub-backup restore round-trips files_home bytes.
- Migrate is one-shot, preflight-safe, and leaves checkout wiki / comms attachments alone.

```bash
GOBBY_TEST_PROTECT=1 uv run pytest tests/config/test_files_home.py tests/storage/test_project_manager.py tests/hooks/test_session_user_profile.py tests/servers/websocket/test_chat_attachments.py tests/wiki/test_wiki_files_home.py tests/servers/routes/test_hub_files_proxy.py tests/cli/test_files_migrate.py tests/cli/test_hub_files_restore.py -v
cargo test -p gobby-wiki --lib scope -- --nocapture
cargo test -p gobby-core reads_bootstrap_with_files_home -- --nocapture
uv run ruff check src/gobby/paths.py src/gobby/config/bootstrap.py src/gobby/storage/projects.py src/gobby/hooks/event_handlers/_session_start/profile.py src/gobby/servers/routes/chat_attachments.py src/gobby/servers/routes/wiki.py src/gobby/cli/pack.py
uv run mypy src/gobby/paths.py src/gobby/config/bootstrap.py src/gobby/storage/projects.py
```

## V1 Plan Changelog
`kind: verification`

**Round 1** `kind: enhancement`

- enhancer_run: 53cbc46d-f860-4ddc-ab47-b25947518fa2
- enhancer_session: 73f8b6c0-cca8-402c-b959-2e33a02fbf66
- converged: false
- suggestions_presented: 5
- accepted:
  - E1 / better / testability — migrate preflight, zero-mutation collision, metadata rewrite, relative wikis.json, complete-layout idempotency
  - E2 / better / clarity — one DaemonClient hub-request path with origin, timeout, and header/byte fidelity
  - E3 / better / clarity — pack unpack and hub-backup restore of the gobby_files bind
  - E4 / better / clarity — authenticated USER.md write plus remote intro path
  - E5 / better / testability — reserved topic-name refusal plus personal-scope positive control
- declined: none
- resolution_notes: Folded all five into § 1.1, § 4.1, § 5.1, § 6.1, and § 6.2. Hub HTTP reuses DaemonClient; atomic USER.md write reuses durable_replace; restore uses unpack and restore_hub_backup rather than a new archive format.

## T1 Task Mapping
`kind: framing`

| Plan Item | Task Ref | Status |
|-----------|----------|--------|
| Epic | #20330 | open |
