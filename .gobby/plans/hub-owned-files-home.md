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
5. `files_home` is required in `datastore_mode: local` when a bootstrap file is present. `hub_daemon_url` is required in `datastore_mode: remote` when a bootstrap file is present. The inverse is a hard `BootstrapConfigError`.
6. Tree: `<files_home>/{USER.md,_personal/{.gobby,notes,reminders,attachments},wiki/{wikis.json,personal,<topic>}}`.
7. Wiki home is not a vault. Flatten today’s `~/wiki/topics/<name>` to `<files_home>/wiki/<name>`. Personal vault is `wiki/personal`.
8. Reserved names: `USER.md`, `_personal`, `wiki` at files_home; reserved vault names `personal`, `_personal`, and `wiki`. Topic refusal is this plan. Project-vault refusal is inherited by #18779.
9. No dual-read of old daemon-home paths after a successful migrate.
10. `_personal` stays checkout-free. Do not `register` a `project_checkouts` row. Path helper is this plan’s; #19651 must not invent a per-machine personal root.
11. `<checkout>/wiki` stays until #18779. Fixture `gwiki --out` stays #19664.
12. `~/.gobby/comms_attachments` stays until Stage 3 / #17488.
13. No full pytest suite. `GOBBY_TEST_PROTECT=1` plus focused pytest, Ruff, Mypy, and the named gwiki/gcore tests. Every touched hand-maintained production source stays under 1,000 lines. Record current and projected counts in the owning leaf when a file is already above 800 lines.
14. Companion coverage ledger: `.gobby/plans/hub-owned-files-home.coverage-ledger.yaml`. It is required before expansion.
15. Cross-store transactional restore (one journal that atomically publishes Postgres plus files) is out of scope. Hub-backup already restores datastores sequentially. Files restore uses the same sequential model plus preflight and the existing confirm/`--force` collision policy. Operator rerun is the recovery path.
16. Missing bootstrap file still returns pre-DB port defaults via `_default_bootstrap_config`. That default is not a files owner: `require_files_home` and local owner writes fail until a present local bootstrap names an absolute `files_home`.
17. Local install that creates a bootstrap file takes an existing absolute `files_home` from the operator (`--files-home`). Writers persist that path; they do not create the root.
18. `gobby files migrate` runs only while the hub daemon is stopped. File writers live in the daemon; a stopped daemon is the exclusive fence. No live-daemon files-home reader-writer lock.

## D1: Production project vault cutover
`kind: deferred`

```yaml
deferral:
  task_ref: "#18779"
  reason: "Empty-vault production activation writes project vaults into wiki/<project.name> after this home exists. That epic inherits refusal of reserved vault names personal, _personal, and wiki as project names. This plan only names the destination and refuses those names at the topic entry."
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

**Goal:** Local-mode daemons resolve one absolute `files_home`. Remote-mode daemons have no files tree and know the hub owner URL. Both language projections carry the same typed fields.

### 1.1 Add files_home and hub_daemon_url resolution [category: code]
`kind: deliverable`

Targets:
- `src/gobby/config/bootstrap.py::BootstrapConfig`
- `src/gobby/config/bootstrap.py::BootstrapConfig.to_config_dict`
- `src/gobby/config/bootstrap.py::load_bootstrap`
- `src/gobby/config/bootstrap.py::_parse_datastore_mode`
- `src/gobby/config/bootstrap.py::_parse_optional_daemon_url`
- `src/gobby/config/bootstrap.py::_default_bootstrap_config`
- `src/gobby/config/bootstrap_io.py::write_bootstrap_yaml`
- `src/gobby/config/postgres_bootstrap.py::write_postgres_defaults`
- `src/gobby/config/app.py::DaemonConfig`
- `src/gobby/cli/install_setup.py::ensure_daemon_config`
- `src/gobby/paths.py::*` — scope-reason: add get_files_home, require_files_home, verified-root device/inode identity, and typed files-home errors beside get_gobby_home
- `src/gobby/install/shared/config/bootstrap.yaml::*` — scope-reason: add files_home to the installed local-mode template
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: regenerate after the installed bootstrap template gains files_home
- `crates/gcore/src/bootstrap.rs::*` — scope-reason: typed FilesHomeView for datastore_mode, files_home, and hub_daemon_url consumed by gwiki
- `tests/config/test_files_home.py`
- `tests/config/test_bootstrap.py::*` — scope-reason: every local/remote fixture must carry the newly required mode-specific field
- `tests/config/test_app_config.py::*` — scope-reason: replace missing/default local bootstrap expectations that omit files_home
- `tests/utils/test_daemon_url.py::*` — scope-reason: present-file daemon-url bootstraps carry the required mode-specific field
- `tests/config/test_bootstrap_postgres.py::*` — scope-reason: present-file postgres-bootstrap mappings carry files_home
- `tests/config/test_bootstrap_falkordb.py::*` — scope-reason: present-file falkordb-bootstrap mappings carry files_home

Add bootstrap fields:

```python
files_home: str | None = None          # absolute path; local mode only
hub_daemon_url: str | None = None      # http(s) origin of the files-owner daemon
```

`load_bootstrap` rules when the bootstrap file exists:

- `datastore_mode: local`: `files_home` required, non-empty, absolute (no `~`, no relative). Missing/relative/`~` → `BootstrapConfigError`. `hub_daemon_url` if present → `BootstrapConfigError` (this process is the owner).
- `datastore_mode: remote`: `hub_daemon_url` required. Parse it as an HTTP(S) origin: scheme `http` or `https`, host required, optional port, no userinfo, no query, no fragment, path empty or `/` only. Store the origin with no trailing slash. Any other shape → `BootstrapConfigError`. `files_home` if present → `BootstrapConfigError`. Refuse an origin that equals this process’s own advertised daemon origin (`daemon_url` or `http(s)://{bind_host}:{daemon_port}`).
- Never default `files_home` to `$GOBBY_HOME` or `$HOME/.gobby/...`.

Missing bootstrap file: keep today’s `_default_bootstrap_config()` for pre-DB port readers. Do not invent `files_home`. `require_files_home` on that default raises `FilesHomeError`. Tests that construct a **present** local or remote bootstrap must include the required field; replace stale missing/default-is-owner expectations.

```python
def get_files_home() -> Path | None:
    """Local-mode files_home, or None on a remote-mode daemon."""

def require_files_home() -> Path:
    """Return files_home or raise FilesHomeNotOnThisDaemonError / FilesHomeError."""
```

`get_files_home` reads the already-loaded bootstrap (same source as `get_gobby_home` uses env/home). Do not mkdir. `require_files_home` re-stats the configured path on every call and records `(st_dev, st_ino)`. Missing directory is `FilesHomeError`, not a silent create. A later publication that sees a different directory identity at the same path is also `FilesHomeError`.

Writers that emit or rewrite a local bootstrap mapping (`write_bootstrap_yaml`, `write_postgres_defaults`, `ensure_daemon_config`, installed template) must persist `files_home` when `datastore_mode` is local, and persist `hub_daemon_url` when remote. A writer must not persist a local mapping that would fail `load_bootstrap`. When `ensure_daemon_config` creates a local bootstrap, it takes a caller-supplied existing absolute `files_home` and writes that value into the template copy or generated fallback. It must not emit a placeholder that fails `load_bootstrap`. `to_config_dict` emits both fields; `DaemonConfig` carries them so bootstrap values are not stranded.

Present-file local/remote fixtures in the existing bootstrap, app-config, daemon-url, postgres-bootstrap, and falkordb-bootstrap suites include the required field. True missing-file default cases stay owner-less.

Rust `FilesHomeView` in `bootstrap.rs` is a validated typed view: `datastore_mode`, `files_home: Option<PathBuf>`, `hub_daemon_url: Option<String>`. Same present-file rules as Python (absolute owner path; remote origin; no tilde/relative; inverse field is an error). Endpoint parse (`DaemonEndpoint`) still falls back to host/port defaults when those keys are absent. Add `reads_bootstrap_with_files_home` covering owner, remote, missing file, relative, tilde, and endpoint-default cases. gwiki consumes this view in § 4.1; tolerance-only unknown-key ignore is not enough.

**Acceptance:**

- 1.1.1 - Local bootstrap file requires absolute `files_home` and refuses `hub_daemon_url`. test: `tests/config/test_files_home.py`.
- 1.1.2 - Remote bootstrap file requires `hub_daemon_url` and refuses `files_home`. test: `tests/config/test_files_home.py`.
- 1.1.3 - `require_files_home` returns the configured path in local mode, raises `FilesHomeNotOnThisDaemonError` in remote mode, and raises `FilesHomeError` when the directory is missing. test: `tests/config/test_files_home.py`.
- 1.1.4 - Installed bootstrap template documents `files_home` for local mode. file: `src/gobby/install/shared/config/bootstrap.yaml`.
- 1.1.5 - Rust `FilesHomeView` accepts owner and remote cases and refuses missing/relative/tilde owner paths; daemon endpoint defaults still load. test: `crates/gcore/src/bootstrap.rs::reads_bootstrap_with_files_home`.
- 1.1.6 - Remote `hub_daemon_url` accepts `http(s)://host[:port]`, refuses userinfo/query/fragment/non-root path, and refuses this process’s own origin. test: `tests/config/test_files_home.py`.
- 1.1.7 - Present-file local/remote fixtures in the existing bootstrap suites include the required field; missing-file defaults are not treated as a files owner. test: `tests/config/test_bootstrap.py`.
- 1.1.8 - Local bootstrap writers persist `files_home`; remote writers persist `hub_daemon_url`. test: `tests/config/test_files_home.py`.
- 1.1.9 - Creating a local bootstrap through `ensure_daemon_config` persists the caller-supplied absolute `files_home` and does not emit a mapping that fails `load_bootstrap`. test: `tests/config/test_files_home.py`.
- 1.1.10 - Present-file fixtures in the daemon-url, postgres-bootstrap, and falkordb-bootstrap suites include the required mode-specific field. test: `tests/utils/test_daemon_url.py`.

## P2: Hub-owner personal tree and USER.md
`kind: framing`

**Goal:** On the hub owner, personal identity and the working profile live under `files_home`. Nodes keep the shared checkout-free sentinel row and never materialize a personal directory.

### 2.1 Retarget personal identity and USER.md [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/storage/projects.py::personal_project_path`
- `src/gobby/storage/projects.py::ensure_personal_project`
- `src/gobby/storage/projects.py::ensure_personal_project_identity`
- `src/gobby/hooks/event_handlers/_session_start/profile.py::*` — scope-reason: owner USER.md read/seed/write and remote raise; no daemon-home fallback
- `src/gobby/cli/install.py::*` — scope-reason: gate filesystem identity to local mode during install
- `src/gobby/storage/hub/runtime.py::*` — scope-reason: hub-open keeps the sentinel row and skips remote filesystem identity
- `src/gobby/cli/utils_config.py::*` — scope-reason: config open keeps the sentinel row and skips remote filesystem identity
- `tests/storage/test_project_manager.py::TestConstants.test_personal_project_path`
- `tests/storage/test_project_manager.py::TestPersonalProjectEnsure.test_ensure_personal_project_creates_folder_and_repo_path`
- `tests/hooks/test_session_user_profile.py::*` — scope-reason: retarget every profile fixture off $GOBBY_HOME/personal
- `tests/cli/test_install_coverage.py::*` — scope-reason: install identity stays local-only
- `tests/cli/test_install_front_door.py::*` — scope-reason: front-door install identity stays local-only
- `tests/cli/test_cli_utils.py::*` — scope-reason: config-open keeps the sentinel and skips remote filesystem identity
- `tests/storage/test_personal_remote_init.py`

```python
def personal_project_path(gobby_home: Path | None = None) -> Path:
    """Hub-owner _personal directory. Raises FilesHomeNotOnThisDaemonError on a node."""
    return require_files_home() / "_personal"
```

`ensure_personal_project_identity` is local-owner only. It writes `<files_home>/_personal/.gobby/project.json` with `id=PERSONAL_PROJECT_ID`, `name=_personal`. It does not mkdir under `$GOBBY_HOME/personal`. It does not call checkout `register` / `rebind`. On remote it must not be invoked; install / hub-open / config-open call it only when `datastore_mode == "local"`.

Local full install takes `--files-home` naming an existing absolute directory (no root create). Persist that bootstrap via `ensure_daemon_config` before `ensure_personal_project_identity` and before managed-service startup. A bootstrap write failure leaves no personal identity marker and starts no managed services. Remote install does not take `--files-home`.

`ensure_personal_project` always upserts the shared checkout-free sentinel row. It must not create a `project_checkouts` row. Filesystem marker creation is local-only. If `projects.repo_path` still exists (pre-#19651 P6), write the hub `_personal` path as unread campaign data on the hub owner only — never a per-machine `$GOBBY_HOME/personal`. After that column is gone, stop writing it. Do not add a compatibility reader for the old path.

`read_user_profile_content` on the hub owner reads `require_files_home() / "USER.md"`. Missing file → `""`. It does not read `$GOBBY_HOME/personal/USER.md`. On a node it raises `FilesHomeNotOnThisDaemonError` so § 5.1 can fetch. `seed_user_profile_content` propagates empty on that error until 5.1 wires the fetch.

Do not create `notes/` / `reminders/` here except as empty dirs from the migrate leaf.

**Acceptance:**

- 2.1.1 - `personal_project_path` is `<files_home>/_personal` and never `$GOBBY_HOME/personal`. test: `tests/storage/test_project_manager.py::TestConstants.test_personal_project_path`.
- 2.1.2 - Identity marker is written under files_home and checkout register is not called. test: `tests/storage/test_project_manager.py::TestPersonalProjectEnsure.test_ensure_personal_project_creates_folder_and_repo_path`.
- 2.1.3 - Hub-owner profile reads `<files_home>/USER.md` only. test: `tests/hooks/test_session_user_profile.py::test_read_user_profile_content_reads_personal_user_md`.
- 2.1.4 - Node `read_user_profile_content` does not read a daemon-home file. test: `tests/hooks/test_session_user_profile.py::test_read_user_profile_content_reads_personal_user_md`.
- 2.1.5 - Remote install, hub-open, and config-open upsert the sentinel row and do not create a node-local `_personal` tree. test: `tests/storage/test_personal_remote_init.py`.
- 2.1.6 - Local install still writes the files_home identity marker. test: `tests/cli/test_install_coverage.py`.
- 2.1.7 - Local install persists `files_home` before personal identity or managed-service startup; an injected bootstrap write failure performs neither. test: `tests/cli/test_install_coverage.py`.
- 2.1.8 - `--files-home` must name an existing absolute directory. test: `tests/cli/test_install_front_door.py`.

## P3: Chat attachments
`kind: framing`

**Goal:** Chat upload bytes reconstruct under `_personal/attachments` on the hub owner. Every reader, deleter, and cleanup job uses one owner-only resolver.

### 3.1 Reconstruct chat attachment paths [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/servers/chat_attachment_files.py::*` — scope-reason: one owner-only resolver and unlink that never follows a stale absolute local_path
- `src/gobby/servers/routes/chat_attachments.py::_attachment_dir`
- `src/gobby/servers/routes/chat_attachments.py::upload_attachment`
- `src/gobby/servers/routes/chat_attachments.py::get_attachment_content`
- `src/gobby/servers/routes/chat_attachments.py::delete_attachment`
- `src/gobby/servers/routes/chat.py::*` — scope-reason: conversation delete uses the owner resolver
- `src/gobby/servers/websocket/handlers/session_lifecycle.py::*` — scope-reason: session cleanup uses the owner resolver
- `src/gobby/servers/websocket/chat_attachments.py::prepare_message_attachments`
- `src/gobby/servers/websocket/chat_attachments.py::_bind_attachments_sync`
- `src/gobby/storage/chat_attachments.py::*` — scope-reason: hub-owned rows are fetched and cleaned by attachment id without machine_id equality; unlink before row delete
- `src/gobby/runner_maintenance/storage_hygiene.py::cleanup_chat_attachments_loop`
- `src/gobby/runner_maintenance/storage_hygiene.py::_remove_stale_chat_attachment_file`
- `src/gobby/utils/durable_file.py::durable_replace`
- `tests/servers/websocket/test_chat_attachments.py::*` — scope-reason: retarget every attachment path assertion off $GOBBY_HOME/projects
- `tests/servers/routes/test_chat_attachments.py::*` — scope-reason: HTTP upload/download/delete use reconstructed files_home paths
- `tests/test_runner_chat_attachments_cleanup.py::*` — scope-reason: stale-upload cleanup uses the owner resolver and skips remote unlink

```python
def resolve_attachment_dir(project_id: str, attachment_id: str) -> Path:
    root = require_files_home()  # re-stat; missing root is FilesHomeError
    return (
        root / "_personal" / "attachments"
        / _safe_path_part(project_id, "project")
        / attachment_id[:2]
        / attachment_id
    )
```

`_attachment_dir` delegates to that resolver. Stop treating DB `local_path` as a machine-absolute canonical location. Persist a files_home-relative path or reconstruct from `(project_id, attachment_id, filename)` on every read. Prefer reconstruct so laptop/desktop path strings never leak into the hub DB.

Conversation delete, websocket lifecycle cleanup, and stale-upload hygiene call the same resolver/unlink. They must not `Path(record.local_path).unlink`. Stored-chat WebSocket prepare/bind (`prepare_message_attachments`) looks up and binds by attachment id; it must not require `machine_id == require_machine_id()`. Terminal-proxy session attachments stay machine-local and are not this resolver.

Remote-mode cleanup returns before any shared-row mutation. On the owner, unlink through the reconstructed resolver first; `FileNotFoundError` counts as completed cleanup; any other unlink failure keeps the row. Delete the row only after unlink succeeds. Remote daemons never delete the only hub locator.

Writes under files_home may create descendant directories only when the files_home root still exists with the same `(st_dev, st_ino)` recorded by `require_files_home`. They must not `mkdir` the configured root itself. `durable_replace` under files_home uses that no-root-create, revalidated identity. Lost or replaced bind → `FilesHomeError`, not a new local directory of canonical data.

Remote-mode upload/download/delete is § 5.1. This leaf’s resolver raises `FilesHomeNotOnThisDaemonError` on a node.

Do not move `~/.gobby/comms_attachments`.

**Acceptance:**

- 3.1.1 - Upload writes under `<files_home>/_personal/attachments/<project>/<id[:2]>/<id>/`. test: `tests/servers/websocket/test_chat_attachments.py`.
- 3.1.2 - Download/delete resolve the same reconstructed path and do not require a stored absolute `local_path`. test: `tests/servers/websocket/test_chat_attachments.py`.
- 3.1.3 - Node resolver does not create `$GOBBY_HOME/projects/.../attachments`. test: `tests/servers/websocket/test_chat_attachments.py`.
- 3.1.4 - Conversation delete, session cleanup, and stale-upload hygiene unlink via the resolver, not `record.local_path`. test: `tests/servers/routes/test_chat_attachments.py`.
- 3.1.5 - Remote cleanup does not unlink a node-local file. test: `tests/test_runner_chat_attachments_cleanup.py`.
- 3.1.6 - A vanished files_home root during upload or USER.md write raises `FilesHomeError` and does not recreate the bind path. test: `tests/servers/routes/test_chat_attachments.py`.
- 3.1.7 - Stored-chat WebSocket prepare/bind finds a hub row by attachment id after a remote upload. test: `tests/servers/websocket/test_chat_attachments.py`.
- 3.1.8 - Remote cleanup mutates no shared attachment row; owner unlink failure keeps the row and a later run finishes the delete. test: `tests/test_runner_chat_attachments_cleanup.py`.
- 3.1.9 - A replaced directory at the configured files_home path raises `FilesHomeError` for upload and USER.md write. test: `tests/servers/routes/test_chat_attachments.py`.

## P4: Wiki home
`kind: framing`

**Goal:** On the hub owner, topic and personal vaults are children of `<files_home>/wiki`. The home itself is not a vault. Ongoing registry writes keep the migrated relative shape.

### 4.1 Point gwiki and daemon wiki scope at files_home/wiki [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `crates/gwiki/src/scope.rs::resolve_hub_path`
- `crates/gwiki/src/scope.rs::resolve_topic`
- `crates/gwiki/src/scope.rs::default_hub_path`
- `crates/gwiki/src/scope.rs::resolves_global_topic`
- `crates/gwiki/src/scope.rs::rejects_invalid_topic_names`
- `crates/gwiki/src/models.rs::validate_topic_name`
- `crates/gwiki/src/registry.rs::register_scope`
- `crates/gwiki/src/commands/init.rs::*` — scope-reason: init uses relative registry writes after flatten
- `src/gobby/servers/routes/wiki.py::*` — scope-reason: topic/personal entry uses files_home wiki home and reserved-name mapping
- `src/gobby/wiki/scope_resolution.py::resolve_wiki_scope`
- `src/gobby/wiki/scope_resolution.py::resolve_scope_identity`
- `src/gobby/wiki/scope_resolution.py::_resolve_project_root_sync`
- `tests/wiki/test_wiki_files_home.py`
- `tests/wiki/test_scope_resolution.py::*` — scope-reason: reserved names, personal sentinel without repo_path, and relative registry at the shared resolver

`resolve_hub_path` order:

1. `GOBBY_WIKI_HUB` if set (tests/explicit override).
2. Else `wiki.hub_path` / `gwiki.hub_path` config if set.
3. Else, when Rust `FilesHomeView` says this process is the hub owner, `<files_home>/wiki`.
4. Else do **not** fall back to `~/wiki`. Error: configure `GOBBY_WIKI_HUB` or run on the files owner.

`resolve_topic` uses `hub.join(topic)` — not `hub.join("topics").join(topic)`. The wiki registry file stays at the wiki home. Reserved topic name `personal` is allowed only as the personal vault; creating a second topic named `personal` is a typed refusal. Refuse `_personal` and `wiki` as topic names. The refusal is the same typed `WikiError::InvalidScope` family `rejects_invalid_topic_names` already uses for `.` / `..` / separators. `validate_topic_name` is the shared guard. Daemon `_resolve_scope` maps that error at the topic entry point and does not construct a local gateway for a reserved name.

Table-driven cases for `personal`, `_personal`, and `wiki`: topic creation/resolution returns the reserved-name error at the Rust resolver, `_resolve_scope`, and the Python daemon topic entry. An explicit personal-scope request (project id `PERSONAL_PROJECT_ID` / `_personal`) still resolves exactly to `<files_home>/wiki/personal`.

Personal scope (project id `PERSONAL_PROJECT_ID` / `_personal`) resolves to `<files_home>/wiki/personal`, never `_personal/wiki`. Project production scope still uses checkout-adjacent `resolve_vault_dir(project_root)` until #18779.

`register_scope` serializes child paths relative to the wiki-home registry parent and rejects escapes. It must not persist an absolute `scope.root()` or a `topics/` prefix. Update in-crate registry fixtures that still use `hub/topics/<name>`.

`resolve_wiki_scope` / `resolve_scope_identity` recognize `PERSONAL_PROJECT_ID` and `_personal` as a first-class owner scope and do not request `repo_path`. Reserved topic names `personal`, `_personal`, and `wiki` refuse in that shared resolver so HTTP, MCP, scheduled registration, and parked-row recovery share one check. Remote topic/personal owner dispatch runs before any local path-existence check.

Daemon `_resolve_scope` / `_gateway` must use the same hub path for topic and personal scopes. A topic request on the hub owner must not look in `~/wiki/topics`.

Wiki home must not carry vault metadata. Creating it is a bug. Init and `register_scope` fail when the owner `files_home` directory is missing; they do not create that root.

**Acceptance:**

- 4.1.1 - Topic `foo` resolves to `<files_home>/wiki/foo`, not `.../wiki/topics/foo`. test: `crates/gwiki/src/scope.rs::resolves_global_topic`.
- 4.1.2 - Default hub path is files_home/wiki on the owner and does not silently become `~/wiki`. test: `tests/wiki/test_wiki_files_home.py`.
- 4.1.3 - Personal wiki scope is `<files_home>/wiki/personal`. test: `tests/wiki/test_wiki_files_home.py`.
- 4.1.4 - Daemon topic/personal routes use that same home. test: `tests/wiki/test_wiki_files_home.py`.
- 4.1.5 - Topic names `personal`, `_personal`, and `wiki` are reserved-name errors at the Rust resolver. test: `crates/gwiki/src/scope.rs::rejects_invalid_topic_names`.
- 4.1.6 - The same three names refuse at the Python daemon topic entry; explicit personal scope still resolves to `<files_home>/wiki/personal`. test: `tests/wiki/test_wiki_files_home.py`.
- 4.1.7 - New topic registration writes wiki-home-relative child paths and rejects escapes. test: `crates/gwiki/src/registry.rs`.
- 4.1.8 - Remote-mode gwiki without `GOBBY_WIKI_HUB` refuses instead of defaulting to `~/wiki`. test: `tests/wiki/test_wiki_files_home.py`.
- 4.1.9 - Personal sentinel resolves without `repo_path`; reserved topic names refuse in the shared Python resolver used by HTTP, MCP, and scheduled registration. test: `tests/wiki/test_scope_resolution.py`.

## P5: Node access
`kind: framing`

**Goal:** A remote-mode daemon never opens a local files tree. Every wiki, attachment, and profile surface proxies to the hub owner through one request-aware boundary.

### 5.1 Proxy file HTTP to hub_daemon_url [category: code] (depends: P3, 4.1)
`kind: deliverable`

Targets:
- `src/gobby/hooks/event_handlers/_session_start/profile.py::*` — scope-reason: remote GET/PUT USER.md through DaemonClient; no node-local tree
- `src/gobby/wiki/owner_dispatch.py`
- `src/gobby/servers/routes/wiki.py::*` — scope-reason: every topic/personal handler including trust/refresh/export/sync-sessions/upkeep/librarian/recap uses owner_dispatch
- `src/gobby/mcp_proxy/tools/wiki.py::gateway`
- `src/gobby/wiki/scheduled_jobs.py::_gateway_for_resolved`
- `src/gobby/servers/websocket/chat_attachments.py::prepare_message_attachments`
- `src/gobby/servers/websocket/chat_attachments.py::_bind_attachments_sync`
- `src/gobby/servers/routes/chat_attachments.py::upload_attachment`
- `src/gobby/servers/routes/chat_attachments.py::get_attachment_content`
- `src/gobby/servers/routes/chat_attachments.py::delete_attachment`
- `src/gobby/storage/chat_attachments.py::*` — scope-reason: bind/fetch/delete hub attachments by id, not current-process machine_id
- `src/gobby/utils/daemon_client.py::*` — scope-reason: async raw request sharing URL/auth/timeout/error mapping with call_http_api
- `src/gobby/utils/durable_file.py::durable_replace`
- `src/gobby/servers/routes/hub_files_proxy.py`
- `src/gobby/servers/routes/__init__.py`
- `src/gobby/servers/_app_routes.py::register_routes`
- `tests/servers/routes/test_hub_files_proxy.py`
- `tests/hooks/test_session_user_profile.py::*` — scope-reason: retarget every profile read off daemon-home and add the hub-fetch and hub-write cases
- `tests/servers/routes/test_wiki_routes.py::*` — scope-reason: status/attach/ingest take the shared dispatch path
- `tests/wiki/test_scheduled_jobs.py::*` — scope-reason: scheduled topic/personal jobs do not construct a node-local gateway

wiki.py is 605 lines. Extract owner resolution and remote dispatch into `src/gobby/wiki/owner_dispatch.py` (new). HTTP handlers (including `status`, `attach`, `ingest`), MCP `gateway`, and scheduled `_gateway_for_resolved` call that module. Checkout project scopes stay local until #18779. Do not add dispatch branches that grow `scheduled_jobs.py` (947 lines). Projected: wiki.py < 700, scheduled_jobs.py ≤ 947, owner_dispatch.py < 400.

On `datastore_mode: remote`, profile, wiki, and attachment consumers share one hub request path: `DaemonClient.from_url(hub_daemon_url)` plus an **async** raw method on `DaemonClient` that uses the same URL join, auth headers, timeout, and error types as `call_http_api`. File upload/download uses a streaming request on that same client (not a fully-buffered body) and `StreamingResponse` backpressure on the way back. Async routes must await that method (or offload the entire sync lifecycle to a worker thread). Do not call sync `call_http_api` on the event loop. Do not add a second HTTP client stack. Join as `{origin}{path}` after the stored origin (no trailing slash). Forward method, path, query, body, and auth. Preserve FileResponse semantics already used by attachment download: status, raw bytes, `Content-Type`, `Content-Disposition`, `Content-Length`, `Content-Range`, `Accept-Ranges`, `ETag`, and inbound `Range` / `If-None-Match` / `If-Modified-Since` when FileResponse already honors them. Enforce existing attachment size limits while streaming. Cancellation closes the client. Network/auth/timeout failures are typed errors, never a local-file fallback.

Hop bound: send `X-Gobby-Files-Proxy-Hop: 1` on every proxied files/wiki/attachment request. If that header is already present, refuse (no second hop). Combined with § 1.1 self-origin refusal this covers same-origin, alias, remote-to-remote, and two-node cycles. Before first use, the node may GET a cheap owner probe that reports local files ownership; a remote target is a typed error.

- `read_user_profile_content` GET `/api/files/user-md`. 404/empty body → `""`.
- `write_user_profile_content`: hub-local mode uses a no-root-create `durable_replace` on `require_files_home() / USER.md`; remote mode PUT `/api/files/user-md` with `{"content": "..."}`. Never mkdir a node-local personal tree.
- Wiki topic/personal operations go through `owner_dispatch` and use `/api/wiki/...` HTTP routes. Inventory every MCP and scheduled topic/personal command and map each to an existing route or a sibling hub route. Commands without a route today: trust, refresh, export, sync-sessions, upkeep, librarian, recap. Do not add a second RPC protocol. HTTP, MCP, and scheduled callers share `owner_dispatch`. Do not construct a local `GwikiGateway` for topic or personal scopes on a node. File-bearing attach/ingest send streamed bytes plus filename/mime metadata; the hub never interprets a node filesystem path.
- Chat attachment upload/download/delete proxy to the matching hub routes, including multipart upload and binary download. Do not call `require_files_home` on the node.

Attachment identity: hub-owned rows are created, bound, fetched, and deleted by `(project_id, attachment_id)`. `machine_id` may be stored as the authenticated caller for audit. Bind/fetch/delete must not require `machine_id == require_machine_id()`. A remote upload then WebSocket bind on the node must find the hub row.

Hub-owner surfaces in `hub_files_proxy.py` (keep this module and `files.py` separate; `files.py` is checkout browsing):

- `GET /api/files/user-md` → `{"content": "<profile text or empty>"}` from `require_files_home()` plus the profile filename.
- `PUT /api/files/user-md` authenticated; body `{"content": "..."}`; atomic replace via no-root-create `durable_replace`. Empty content is a valid write (clears the working profile).

Export `create_hub_files_proxy_router` from `src/gobby/servers/routes/__init__.py` and include it in `register_routes`. Add an assembled FastAPI app test that both endpoints exist.

No caching of the working profile or vault bytes on the node disk.

**Acceptance:**

- 5.1.1 - Remote profile seed fetches hub USER.md and never reads `$GOBBY_HOME/personal/USER.md`. test: `tests/hooks/test_session_user_profile.py::test_seed_user_profile_content_merges_profile`.
- 5.1.2 - Remote wiki topic/personal requests, including status/attach/ingest, are proxied and do not create `~/wiki`. test: `tests/servers/routes/test_hub_files_proxy.py`.
- 5.1.3 - Remote attachment upload/download/delete proxy and do not write `$GOBBY_HOME/projects`. test: `tests/servers/routes/test_hub_files_proxy.py`.
- 5.1.4 - Hub `GET /api/files/user-md` returns the files_home profile. test: `tests/servers/routes/test_hub_files_proxy.py`.
- 5.1.5 - Hub PUT writes USER.md atomically; remote `write_user_profile_content` updates the hub copy and creates no node-local USER.md or personal directory. test: `tests/hooks/test_session_user_profile.py`.
- 5.1.6 - Origin join, timeout/auth errors, multipart upload, and binary download preserve status, bytes, Content-Type, and Content-Disposition. test: `tests/servers/routes/test_hub_files_proxy.py`.
- 5.1.7 - MCP and scheduled topic/personal jobs use `owner_dispatch` and do not construct a node-local gateway. test: `tests/wiki/test_scheduled_jobs.py`.
- 5.1.8 - Assembled FastAPI app exposes GET and PUT `/api/files/user-md`. test: `tests/servers/routes/test_hub_files_proxy.py`.
- 5.1.9 - Async hub requests yield the event loop; cancellation cleans up the client. test: `tests/servers/routes/test_hub_files_proxy.py`.
- 5.1.10 - Repeated hop, self-origin, remote-to-remote, and two-node cycles refuse with a typed error. test: `tests/servers/routes/test_hub_files_proxy.py`.
- 5.1.11 - Remote upload then WebSocket bind finds the hub attachment row. test: `tests/servers/routes/test_hub_files_proxy.py`.
- 5.1.12 - wiki.py and scheduled_jobs.py stay under 1,000 lines after dispatch extraction. behavior: "touched production sources stay under 1,000 lines" in `docs/architecture/hub-owned-files-home.md`.
- 5.1.13 - Every inventoried MCP and scheduled topic/personal command has a hub `/api/wiki/...` route and uses `owner_dispatch` remotely. test: `tests/servers/routes/test_wiki_routes.py`.
- 5.1.14 - Remote attach/ingest send streamed bytes plus metadata and do not forward a node filesystem path. test: `tests/servers/routes/test_hub_files_proxy.py`.
- 5.1.15 - Attachment proxy streams with backpressure; range, slow-consumer, cancellation, and maximum-size cases hold. test: `tests/servers/routes/test_hub_files_proxy.py`.

## P6: Migrate and inventory
`kind: framing`

**Goal:** One hub-local move of existing bytes with resume-on-rerun; pack/backup see the bind dir; operator docs match the new paths.

### 6.1 Add hub-local files migrate [category: code] (depends: P4)
`kind: deliverable`

Targets:
- `src/gobby/cli/files.py`
- `src/gobby/files_migrate.py`
- `src/gobby/cli/__init__.py::cli`
- `tests/cli/test_files_migrate.py`

Hub-local only (`require_files_home`). Command: `gobby files migrate`, registered on the root Click command in `cli`. No dual-write. No start-time implicit migrate. Refuse unless the hub daemon is stopped. File writers live in the daemon; a stopped daemon is the exclusive fence. Do not add a live-daemon files-home reader-writer lock.

After validating the pre-provisioned `files_home` root (do not create it), initialize the baseline tree if missing: empty profile file, personal marker and notes/reminders/attachments directories, wiki home directory, and a baseline wiki-home registry. Then move present sources. Missing sources skip; empty-source hubs still get the baseline.

Source classes (skip missing; each is independently resumable):

1. `$GOBBY_HOME/personal/USER.md` → `<files_home>/USER.md`
2. `$GOBBY_HOME/personal/.gobby` → `<files_home>/_personal/.gobby`
3. `$GOBBY_HOME/personal/wiki` → `<files_home>/wiki/personal`
4. leftover `$GOBBY_HOME/personal/{notes,reminders}` and any other leftover children (except already-handled USER.md / `.gobby` / `wiki`) → `<files_home>/_personal/<name>`
5. each `~/wiki/topics/<name>` → `<files_home>/wiki/<name>`
6. `$GOBBY_HOME/projects/<id>/attachments/**` → `<files_home>/_personal/attachments/<id>/...`
7. the existing hub wiki registry beside `~/wiki/topics/` → transform every registration to wiki-home-relative children; refuse malformed or unknown entries before any source mutation; publish the registry only after verified vault moves

There is no separate persisted notes/reminders subsystem in production code today. If those directories exist under the old personal tree, class 4 preserves their bytes. If they do not exist, skip. Checkout vaults and comms attachment storage stay put.

Preflight: discover every present source/destination pair first. Refuse with zero source mutations when a destination exists and is not a recognized migrate destination (unexpected extra content). A recognized partial destination (only expected children, some sources still present) is resume, not refuse.

Apply the architecture Migration steps for each remaining source: copy or rename onto the same filesystem when possible; on EXDEV copy then verify then delete the source. Delete a source only after the destination bytes verify. After each published class, rewrite that class’s vault metadata / wiki registry to wiki-home-relative child paths (no absolute laptop paths). Refuse remote mode.

After a successful full run, personal and topic vault metadata `root` values point at the new wiki home children. The wiki registry contains only wiki-home-relative child paths.

Idempotent: a second run is success when remaining sources are gone and the destination is the complete expected layout. A crash after the first class leaves a recognized partial; the next run resumes remaining classes. Partial failure must not invent a reader for the old locations.

**Acceptance:**

- 6.1.1 - First migrate moves every present source class, including leftover personal notes/reminders when they exist. test: `tests/cli/test_files_migrate.py`.
- 6.1.2 - Second migrate after a complete destination is a no-op success. test: `tests/cli/test_files_migrate.py`.
- 6.1.3 - Remote mode refuses. test: `tests/cli/test_files_migrate.py`.
- 6.1.4 - Checkout wiki and comms_attachments are untouched. test: `tests/cli/test_files_migrate.py`.
- 6.1.5 - Unrecognized destination content refuses before any source mutation. test: `tests/cli/test_files_migrate.py`.
- 6.1.6 - After a successful migrate, personal and topic scope metadata name the new wiki-home roots. test: `tests/cli/test_files_migrate.py`.
- 6.1.7 - After a successful migrate, the wiki registry holds only wiki-home-relative child paths. test: `tests/cli/test_files_migrate.py`.
- 6.1.8 - A recognized partial destination resumes remaining sources instead of refusing. test: `tests/cli/test_files_migrate.py`.
- 6.1.9 - Injected failure after the first published class leaves the remaining source intact; the next run finishes it. test: `tests/cli/test_files_migrate.py`.
- 6.1.10 - `gobby files migrate --help` is reachable from the root command. test: `tests/cli/test_files_migrate.py`.
- 6.1.11 - The existing hub wiki registry is an explicit source; malformed entries refuse before mutation; a crash before registry publication leaves remaining sources intact. test: `tests/cli/test_files_migrate.py`.
- 6.1.12 - Empty-source migrate still creates the baseline profile file, personal children, wiki directory, and registry without creating the files_home root. test: `tests/cli/test_files_migrate.py`.
- 6.1.13 - Migrate refuses while the hub daemon is running. test: `tests/cli/test_files_migrate.py`.

### 6.2 Inventory bind dir in pack and hub-backup [category: code] (depends: 6.1)
`kind: deliverable`

Targets:
- `src/gobby/cli/pack.py::pack`
- `src/gobby/cli/pack.py::_do_pack`
- `src/gobby/cli/pack.py::unpack`
- `src/gobby/cli/pack.py::_archive_would_overwrite`
- `src/gobby/cli/pack.py::_safe_archive_target`
- `src/gobby/cli/hub_backup/files_home.py`
- `src/gobby/cli/hub_backup/cli.py::_run_backup`
- `src/gobby/cli/hub_backup/cli.py::_archive_volumes`
- `src/gobby/cli/hub_backup/cli.py::restore_hub_backup`
- `src/gobby/cli/hub_backup/_stores.py::tar_volumes`
- `src/gobby/data/docker-compose.services.yml::*` — scope-reason: declare the files_home bind as a lifecycle entry
- `src/gobby/cli/installers/compose_env.py::resolve_compose_runtime`
- `tests/cli/test_hub_files_restore.py`
- `tests/cli/test_pack.py::*` — scope-reason: exact pack inventories, summaries, overwrite policy, and bootstrap fixtures
- `tests/cli/hub_backup/test_cli.py::*` — scope-reason: exact backup artifacts, restore order, summaries, and bootstrap fixtures
- `tests/cli/test_compose_bind_address.py::*` — scope-reason: compose environment projects GOBBY_FILES_HOME; drop deleted gcore asset comparison
- `tests/cli/installers/test_compose_env.py::*` — scope-reason: compose environment fixtures include files_home
- `tests/cli/installers/test_postgres_compose_template.py::*` — scope-reason: Python compose asset is the only template; drop deleted gcore copy
- `tests/install/test_daemon_runtime_pins.py::*` — scope-reason: runtime pins do not require the deleted gcore compose asset

`hub_backup/cli.py` is 930 lines. Do not add archive/restore bodies there. Put files-home archive, restore, collision, and confirm/`--force` in `src/gobby/cli/hub_backup/files_home.py`. `cli.py` only calls it. Projected: `cli.py` ≤ 950, `files_home.py` < 300, `pack.py` (714) < 850.

Declare `gobby_files` as a **bind** of the configured `files_home` (not a named volume attached to postgres/qdrant/falkor). No service in compose consumes it except backup/pack helpers that archive the bind path. `resolve_compose_runtime` projects a validated `GOBBY_FILES_HOME` from the local bootstrap. Datastore volume enumeration stays unchanged. The Python compose asset is the only template. The gcore compose copy remains absent; compose-template and runtime-pin suites treat that absence as required.

`pack` includes the bind directory as `gobby/files/` in the tarball when `files_home` is set. Do not add `personal/` from `$GOBBY_HOME` to `PACK_FILES`. `unpack` extracts `gobby/files/` members into configured `files_home` (via `_safe_archive_target`), not under `$GOBBY_HOME/files`. Files-home members use fixed-size chunk copy into same-filesystem temporary files, then fsync and atomically replace; they must not `read()` a whole member into memory. Preflight explicit member-count and aggregate uncompressed-size limits before any files_home mutation. `_archive_would_overwrite` treats those members as files_home collisions and keeps the existing confirm/`--force` policy. An interrupted member is not authoritative; operator rerun uses that collision policy. No cross-store journal.

`HUB_VOLUMES` stays datastore named volumes. `_run_backup` / `_archive_volumes` take a separate bind-archive step for `files_home`. `restore_hub_backup` recognizes that archive member and restores it into configured `files_home` with the same confirm/`--force` collision policy unpack uses. Do not append `gobby_files` to `HUB_VOLUMES`. Restore stays sequential with the existing datastore restores (no cross-store journal). Preflight all artifacts and collisions before mutating; operator rerun is the recovery path.

Update existing pack and hub-backup suites so exact inventories, JSON summaries, restore order, overwrite behavior, and bootstrap fixtures include the files bind. Keep the new end-to-end round trip.

**Acceptance:**

- 6.2.1 - Pack archives `files_home` and not `$GOBBY_HOME/personal`. file: `src/gobby/cli/pack.py`.
- 6.2.2 - Compose/hub-backup treat `gobby_files` as a bind-dir lifecycle entry, not a datastore named volume. file: `src/gobby/data/docker-compose.services.yml`.
- 6.2.3 - Pack unpack and hub-backup restore put USER.md, a `_personal` attachment, and a wiki file back under configured `files_home` and apply the existing overwrite policy. test: `tests/cli/test_hub_files_restore.py`.
- 6.2.4 - Existing pack inventory/summary/overwrite contracts include the files bind. test: `tests/cli/test_pack.py`.
- 6.2.5 - Existing hub-backup artifact/order/summary contracts include the files bind archive. test: `tests/cli/hub_backup/test_cli.py`.
- 6.2.6 - `hub_backup/cli.py` stays under 1,000 lines; files-home archive/restore lives in the extracted module. behavior: "touched production sources stay under 1,000 lines" in `docs/architecture/hub-owned-files-home.md`.
- 6.2.7 - `resolve_compose_runtime` exports validated `GOBBY_FILES_HOME`; datastore volume enumeration is unchanged. test: `tests/cli/test_compose_bind_address.py`.
- 6.2.8 - files_home unpack and restore use chunked temp-file publication and preflight member-count plus aggregate size limits. test: `tests/cli/test_hub_files_restore.py`.

### 6.3 Update operator docs for files_home [category: docs] (depends: 6.2)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/intro/SKILL.md`
- `docs/architecture/hub-owned-files-home.md`
- `docs/guides/system-requirements.md`
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: regenerate after the intro skill is the last bundled-content edit
- `tests/install/test_bundled_content_manifest.py::*` — scope-reason: integrity digest matches the intro edit

Intro skill and system-requirements: hub-local profile path is `<files_home>/USER.md`. Remote intro uses the § 5.1 PUT path, not `~/.gobby/personal/USER.md`. Architecture doc stays the contract; only path examples that still show `$GOBBY_HOME/personal` as live must match this plan. § 1.1 may regenerate the bundled-content digest for `bootstrap.yaml`; this leaf is the last bundled-content edit, so it owns the final digest after the intro change.

**Acceptance:**

- 6.3.1 - Intro skill writes `<files_home>/USER.md` on the hub owner and documents the remote PUT path. file: `src/gobby/install/shared/skills/intro/SKILL.md`.
- 6.3.2 - System-requirements documents the hub-local files_home profile path. file: `docs/guides/system-requirements.md`.
- 6.3.3 - Bundled-content digest matches the intro skill after this leaf. test: `tests/install/test_bundled_content_manifest.py`.

## V2 End-to-end verification
`kind: verification`

Focused proof, hub-owner plus remote-mode:

- Local bootstrap + `require_files_home`; present-file fixtures carry the required field, including daemon-url and postgres/falkordb bootstrap suites.
- Personal marker and USER.md under files_home; remote startup keeps the sentinel and skips filesystem identity.
- Attachments reconstruct under `_personal/attachments`; cleanup unlinks on the owner before row delete and is a no-op remotely.
- Topic/personal wiki paths under `files_home/wiki`; registry writes stay relative; personal sentinel resolves without repo_path.
- Remote daemon proxies HTTP, MCP, and jobs (including the inventoried command matrix) and writes no daemon-home copies.
- Reserved topic names refuse at Rust and Python; explicit personal scope still resolves.
- Remote USER.md write updates the hub copy only.
- Remote upload then WebSocket bind finds the hub row.
- Hop/self-origin/cycles refuse.
- Pack/hub-backup restore round-trips files_home bytes with chunked publication; existing inventory contracts stay green.
- Compose runtime exports `GOBBY_FILES_HOME`; the gcore compose copy stays absent.
- Migrate inventories leftover personal notes/reminders and `~/wiki/wikis.json`, initializes an empty baseline, resumes a recognized partial, refuses while the daemon is running, and leaves checkout wiki / comms attachments alone.

```bash
GOBBY_TEST_PROTECT=1 uv run pytest tests/config/test_files_home.py tests/config/test_bootstrap.py tests/config/test_app_config.py tests/utils/test_daemon_url.py tests/config/test_bootstrap_postgres.py tests/config/test_bootstrap_falkordb.py tests/storage/test_project_manager.py tests/storage/test_personal_remote_init.py tests/hooks/test_session_user_profile.py tests/servers/websocket/test_chat_attachments.py tests/servers/routes/test_chat_attachments.py tests/test_runner_chat_attachments_cleanup.py tests/wiki/test_wiki_files_home.py tests/wiki/test_scope_resolution.py tests/wiki/test_scheduled_jobs.py tests/servers/routes/test_hub_files_proxy.py tests/servers/routes/test_wiki_routes.py tests/cli/test_files_migrate.py tests/cli/test_hub_files_restore.py tests/cli/test_pack.py tests/cli/hub_backup/test_cli.py tests/cli/test_install_coverage.py tests/cli/test_install_front_door.py tests/cli/test_compose_bind_address.py tests/cli/installers/test_compose_env.py tests/cli/installers/test_postgres_compose_template.py tests/install/test_bundled_content_manifest.py tests/install/test_daemon_runtime_pins.py -v
cargo test -p gobby-wiki --lib scope -- --nocapture
cargo test -p gobby-wiki --lib registry -- --nocapture
cargo test -p gobby-core reads_bootstrap_with_files_home -- --nocapture
uv run ruff check src/gobby/paths.py src/gobby/config/bootstrap.py src/gobby/storage/projects.py src/gobby/hooks/event_handlers/_session_start/profile.py src/gobby/servers/routes/chat_attachments.py src/gobby/servers/routes/wiki.py src/gobby/cli/pack.py src/gobby/cli/hub_backup/cli.py
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

**Round 1** `kind: verification`

- reviewer_run: 819d849e-f595-49ad-8578-c5b14b13be72
- reviewer_session: 355cda97-8d5f-4edd-bf6e-802bed06ae0b
- verdict: needs_review
- findings:
- F1-coverage-ledger / blocking / companion coverage-ledger YAML absent
- F2-section-6-2-routing / blocking / 6.2 docs category routes executable pack/backup work to tech-writer
- F3-rust-files-home-data-flow / blocking / gwiki has no typed Rust files-home view
- F4-reserved-vault-names / blocking / project-vault refusal omitted; Python reserved-name test missing
- F5-notes-reminders-migration / blocking / notes/reminders source inventory undefined
- F6-bootstrap-consumers / blocking / writers and existing fixtures omit the new required fields
- F7-remote-personal-initializers / blocking / remote startup calls owner-only filesystem identity
- F8-attachment-path-consumers / blocking / cleanup still unlinks record.local_path
- F9-attachment-machine-identity / blocking / remote bind keys on node machine_id
- F10-wiki-remote-dispatch / blocking / status/attach/ingest, MCP, and jobs bypass proxy
- F11-wiki-registry-writer / blocking / register_scope reintroduces absolute topics/ paths
- F12-files-router-registration / blocking / hub_files_proxy not exported or included
- F13-files-cli-registration / blocking / files group not on root CLI
- F14-hub-backup-decomposition / blocking / cli.py 930 lines would exceed 1,000
- F15-backup-test-inventory / blocking / existing pack/backup contract suites omitted
- F16-async-proxy-boundary / blocking / sync DaemonClient on async routes
- F17-proxy-loop / blocking / no hop/self-origin/cycle bound
- F18-files-root-recreation / blocking / mkdir can recreate a lost bind
- F19-migration-recovery / blocking / partial dest refused with no resume path
- F20-restore-recovery / blocking / mid-restore can split DB and files generations
- resolution_notes: Accepted F1–F19; declined F20. F19 repaired as exclusive lock plus resume-on-rerun (no journal). F20 remains sequential restore with preflight/collision and operator rerun. 6.2 is now backend code; 6.3 is docs. 1.1 gained a typed Rust FilesHomeView, writer/fixture inventory, and self-origin refuse. 2.1 gates filesystem identity to local mode. 3.1 adds one owner resolver and no-root-create writes. 4.1 adds Python reserved-name tests and relative register_scope. 5.1 extracts owner_dispatch, async DaemonClient, hop bound, router registration, and id-based attachment bind. 6.1 inventories leftover personal notes/reminders, registers the CLI, and resumes recognized partials. Companion ledger path is named in Constraints; YAML write is still required before expansion.

```json plan-review-round
{"evidence_id":"de7c9274-c7c0-423b-aa71-5e89f39ead13","plan_hash":"98627d1fc1843f30bf6c7645b4392fa60009b7c8f74e0adb37db2567ba2d73e7","round_number":1,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"01a62860d6f3ec60726d6c4be8f75bc74a337b7be8c651ae7dc8a0a13894bcb2","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":5,"emitted_findings":20,"total":25},"evidence_id":"de7c9274-c7c0-423b-aa71-5e89f39ead13","lanes":[{"candidate_count":7,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":11,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":7,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":7,"manifest_digest":"8f22f5df455dea4d90e4f35ecb2a9f67bf4904453eeca07220bb0ce28f7f25fa","status":"valid"},"source_digest":"5b654cb485191784e5fc4531b69276e125b64fede53bb8e5892e4862f4f28e6b","version":1},"findings":[{"category":"gobby-format","check_key":"bootstrap-coverage-ledger","description":"The required `.coverage-ledger.yaml` companion is missing, so close-time coverage cannot compare the eventual leaves with this reviewed plan.","finding_id":"F1-coverage-ledger","fix":"Add `.gobby/plans/hub-owned-files-home.coverage-ledger.yaml` with all 36 acceptance items mapped to the seven expected leaves and pin it to the revised plan hash.","location":"Plan preamble / whole-plan coverage","prevention":"Before resubmission, compare every deliverable and acceptance item against the required companion-ledger artifact.","principle":"Every new epic plan must carry the adversary-reviewed companion coverage ledger required before expansion.","root_cause":"The plan defines seven leaves and 36 acceptance items, while `.gobby/plans/hub-owned-files-home.coverage-ledger.yaml` is absent.","section_id":"__preamble__","severity":"blocking"},{"category":"gobby-format","check_key":"implementation-category-alignment","description":"The current category sends substantial backend implementation to a documentation route and suppresses the code leaf's TDD contract.","finding_id":"F2-section-6-2-routing","fix":"Change 6.2 to `[category: code]` with `implementation_domain: backend` and TDD, or split documentation-only edits into a docs leaf while retaining all executable lifecycle work in a backend code leaf.","location":"P6 / § 6.2","prevention":"For each deliverable, compare its production targets and behavioral acceptance with its category, implementation domain, and TDD route.","principle":"Deliverable category must match the executable work and deterministic agent route it creates.","root_cause":"Section 6.2 is marked `docs` although it changes pack, unpack, backup, restore, compose behavior, and executable tests; shadow derivation routes it to `tech-writer` with `tdd: false`.","section_id":"6.2","severity":"blocking"},{"category":"missing-requirement","check_key":"rust-files-home-data-flow","description":"No Rust API carries `datastore_mode`, `files_home`, and `hub_daemon_url` into gwiki, so the owner default and remote refusal cannot be implemented from the planned tolerance-only change.","finding_id":"F3-rust-files-home-data-flow","fix":"Specify a validated typed Rust files-home bootstrap view, make gwiki consume it, and change 1.1.5 to `reads_bootstrap_with_files_home` covering owner, remote, missing, relative, tilde, and endpoint-default cases.","location":"P1 / § 1.1 and P4 / § 4.1","prevention":"Trace every new configuration field from parser through each language projection to its consuming symbol and acceptance test.","principle":"A consumer that derives behavior from configuration needs a typed data path to the required fields; ignoring unknown keys is insufficient.","root_cause":"P1 asks gcore only to tolerate `files_home`, while P4 requires gwiki to distinguish local versus remote mode and derive `<files_home>/wiki`; acceptance 1.1.5 also cites the old datastore-mode test instead of the new regression named by prose and V2.","section_id":"1.1","severity":"blocking"},{"category":"missing-requirement","check_key":"reserved-vault-name-coverage","description":"A deferred project vault may collide with the personal or reserved home entries, and the Python daemon refusal has no named acceptance test.","finding_id":"F4-reserved-vault-names","fix":"Carry project-name refusal into D1's inherited obligation for #18779, and add a concrete Python test in 4.1 for all three reserved names plus explicit personal-scope success.","location":"D1 and P4 / § 4.1","prevention":"For namespace reservations, enumerate topic, project, personal, Rust, and Python entry points and require a negative test plus the explicit-personal positive control.","principle":"Every reserved-name rule must be assigned to each producer that can create the colliding namespace and to a concrete test at each entry point.","root_cause":"The architecture reserves `personal`, `_personal`, and `wiki` for topic and project vault names; 4.1 covers topics, while D1/#18779 omits project-vault refusal, and 4.1.5 cites only the Rust test for a requirement that also names the Python daemon.","section_id":"4.1","severity":"blocking"},{"category":"missing-requirement","check_key":"legacy-content-source-inventory","description":"Existing note or reminder bytes can be omitted silently, and acceptance 6.1.1 has no deterministic source inventory.","finding_id":"F5-notes-reminders-migration","fix":"Discover and enumerate their current persisted sources, add them to preflight/migration and byte-preservation tests, or state that no legacy files exist and correct the source-class count.","location":"P2 / § 2.1 and P6 / § 6.1","prevention":"Reconcile every destination class with a named legacy source, absence proof, and byte-preservation acceptance before designing moves.","principle":"A migration must enumerate every legacy content source or explicitly prove that no persisted legacy source exists.","root_cause":"Notes and reminders are named as existing hub-semantic content, but neither architecture nor plan identifies source paths or preservation behavior; 6.1 then claims an undefined six source classes.","section_id":"6.1","severity":"blocking"},{"category":"traceability","check_key":"mandatory-bootstrap-consumer-inventory","description":"Existing configuration generation and tests will fail the new hard-error contract outside the planned target and validation inventory.","finding_id":"F6-bootstrap-consumers","fix":"Add every bootstrap writer and current local/remote fixture to 1.1, including install fallback generation and existing bootstrap/app-config tests, then replace stale missing/default expectations explicitly.","location":"P1 / § 1.1","prevention":"Run constructor/writer/fixture blast radius for every newly mandatory field and add each changed file to Targets and focused validation.","principle":"Making a configuration field mandatory requires updating every writer, constructor, fallback, fixture, and existing expectation in the same leaf.","root_cause":"The target list covers the parser, a new test, and installed template, while current install fallbacks and existing bootstrap suites still create local or remote bootstrap data without the newly required mode-specific field.","section_id":"1.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"remote-personal-initializer-gating","description":"Remote daemon startup has no specified branch that preserves the sentinel row while skipping `_personal` filesystem identity.","finding_id":"F7-remote-personal-initializers","fix":"Target `install.py`, `storage/hub/runtime.py`, `cli/utils_config.py`, and their tests; gate filesystem marker creation to local mode while preserving the shared checkout-free sentinel database row.","location":"P2 / § 2.1 and P5 / § 5.1","prevention":"For every owner-only path helper, inspect all startup and initialization callers and define local-owner and remote branches.","principle":"Remote mode must establish shared database identity without invoking an owner-only filesystem path.","root_cause":"Install, hub-database opening, and local-storage initialization call personal identity/project setup unconditionally; changing `personal_project_path` to raise remotely makes these paths fail or recreate node-local state.","section_id":"2.1","severity":"blocking"},{"category":"traceability","check_key":"attachment-path-consumer-inventory","description":"Non-route cleanup paths can unlink the wrong location or fail after absolute `local_path` stops being canonical.","finding_id":"F8-attachment-path-consumers","fix":"Introduce one owner-only resolver from project ID, attachment ID, and filename; retarget chat deletion, session lifecycle, storage hygiene, storage helpers, and existing HTTP route tests, with remote cleanup prohibited from local unlink.","location":"P3 / § 3.1 and P5 / § 5.1","prevention":"For path-shape changes, enumerate create/read/delete/bulk-delete/maintenance consumers and both HTTP and WebSocket test seams.","principle":"Changing a persisted path from canonical absolute storage to reconstruction requires every reader, deleter, cleanup job, and test to use the new resolver.","root_cause":"Conversation deletion, WebSocket lifecycle cleanup, and stale-upload maintenance still pass `record.local_path` to unlink helpers, and the existing HTTP route tests are absent from Targets.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"proxied-attachment-machine-ownership","description":"A remote upload can succeed and then become invisible to message binding because its row is keyed to a different machine.","finding_id":"F9-attachment-machine-identity","fix":"Define one authoritative attachment ownership model: propagate authenticated caller machine identity to all hub operations or proxy the complete bind/fetch/delete lifecycle to the hub; add a remote upload-then-WebSocket-bind integration test.","location":"P3 / § 3.1 and P5 / § 5.1","prevention":"Trace identity keys across upload, persistence, bind, fetch, delete, and proxy authentication, then test the complete remote lifecycle.","principle":"A proxied lifecycle must preserve the ownership identity used by every later fetch, bind, and delete operation.","root_cause":"Hub upload storage uses the hub process machine ID, while remote WebSocket binding queries with the node machine ID; current daemon auth headers do not transmit an authoritative caller machine identity.","section_id":"5.1","severity":"blocking"},{"category":"traceability","check_key":"wiki-remote-entrypoint-inventory","description":"Implementing only the named route helpers leaves reachable remote operations that touch a node-local vault or fail outside the intended proxy path.","finding_id":"F10-wiki-remote-dispatch","fix":"Move owner resolution and remote dispatch into a request-aware shared scope/gateway boundary used by all HTTP handlers, MCP tools, and scheduled jobs; keep checkout project scopes local until #18779 and add refusal/proxy tests for every adjacent path.","location":"P4 / § 4.1 and P5 / § 5.1","prevention":"Inventory all gateway constructors and callers across HTTP, MCP, jobs, and reconciliation, then prove remote topic/personal calls cannot reach local gateway creation.","principle":"Remote topic and personal wiki access must cross one dispatch boundary before any local gateway is constructed, across every HTTP and non-HTTP entry point.","root_cause":"HTTP `status`, `attach`, and `ingest` bypass `_read`/`_write_call`, while MCP registry and scheduled jobs use shared scope helpers that still construct local gateways; these symbols and tests are absent from the plan.","section_id":"5.1","severity":"blocking"},{"category":"missing-requirement","check_key":"relative-registry-write-invariant","description":"The first topic registration after migration can immediately reintroduce absolute registry paths and the removed `topics/` prefix.","finding_id":"F11-wiki-registry-writer","fix":"Target `crates/gwiki/src/registry.rs` and affected integration fixtures; serialize child paths relative to the wiki-home registry parent, reject escapes, update fixtures, and run the affected gwiki integration tests.","location":"P4 / § 4.1 and P6 / § 6.1","prevention":"For every migration normalization, locate all ongoing writers and fixtures that can recreate the old shape.","principle":"A migrated serialization invariant must also be enforced by every future writer.","root_cause":"Migration rewrites `wikis.json` to relative paths once, while current `register_scope` persists absolute `scope.root()` values and existing fixtures retain `hub/topics/<name>`.","section_id":"6.1","severity":"blocking"},{"category":"traceability","check_key":"new-router-registration","description":"`GET` and `PUT /api/files/user-md` can remain unreachable even if their handler module is implemented.","finding_id":"F12-files-router-registration","fix":"Add `src/gobby/servers/routes/__init__.py` and `src/gobby/servers/_app_routes.py` exact targets, export/include the router, and add an assembled FastAPI application test for both endpoints.","location":"P5 / § 5.1","prevention":"For every new router module, inspect package exports, application include registries, and an assembled-app test.","principle":"A new route module is incomplete until exported, registered in the assembled application, and exercised through that assembly.","root_cause":"The plan adds `hub_files_proxy.py` but omits explicit router export and `_app_routes.py` inclusion inventories.","section_id":"5.1","severity":"blocking"},{"category":"traceability","check_key":"new-cli-registration","description":"`gobby files migrate` will not exist from the root CLI unless registration is added.","finding_id":"F13-files-cli-registration","fix":"Add `src/gobby/cli/__init__.py` as a target, register the files group, and include a root-command smoke/invocation test.","location":"P6 / § 6.1","prevention":"For every new CLI module, inspect root imports, `add_command` inventory, help exposure, and top-level invocation.","principle":"A new Click command module is incomplete until imported and registered by the root command.","root_cause":"The plan creates `src/gobby/cli/files.py` but omits the explicit root CLI registry.","section_id":"6.1","severity":"blocking"},{"category":"gobby-format","check_key":"projected-source-size-ceiling","description":"The lifecycle implementation has only 69 available lines before violating the enforced monolith ceiling.","finding_id":"F14-hub-backup-decomposition","fix":"Plan a focused hub-backup files-home archive/restore module before editing `cli.py`; keep `cli.py` orchestration-only and record projected post-change counts for all touched production sources.","location":"P6 / § 6.2","prevention":"Record current and projected line counts for each touched production file and plan decomposition whenever the projection reaches the ceiling.","principle":"Every touched hand-maintained production source must remain below 1,000 lines after the planned edit.","root_cause":"`src/gobby/cli/hub_backup/cli.py` is already 930 lines, while 6.2 adds archive creation, manifest integration, restore extraction, collision policy, and confirmation behavior to three functions without a decomposition.","section_id":"6.2","severity":"blocking"},{"category":"weak-testability","check_key":"existing-lifecycle-test-contracts","description":"The new test can pass while established pack and backup contracts fail or retain stale expectations.","finding_id":"F15-backup-test-inventory","fix":"Add `tests/cli/test_pack.py` and `tests/cli/hub_backup/test_cli.py` to 6.2; update their bootstrap fixtures, exact inventories, summaries, overwrite behavior, and restore order while retaining the new end-to-end round trip.","location":"P6 / § 6.2","prevention":"Blast-radius every exact inventory/order assertion when adding a lifecycle artifact and include those suites in Targets and validation.","principle":"Behavior that changes exact artifact inventories, restore order, summaries, and bootstrap prerequisites must update the existing contract suites that encode them.","root_cause":"Section 6.2 targets only a new round-trip test, while current pack and hub-backup tests assert exact stores/artifacts, JSON summaries, restore sequencing, and bootstrap fixtures.","section_id":"6.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"async-http-proxy-boundary","description":"A slow hub request can block unrelated daemon requests for the full timeout.","finding_id":"F16-async-proxy-boundary","fix":"Specify an async raw method within the existing DaemonClient abstraction using the same URL/auth/error logic, or offload the complete synchronous request/stream lifecycle to a worker thread; add concurrency and cancellation tests.","location":"P5 / § 5.1","prevention":"At every sync/async boundary, test delayed dependency response, unrelated event-loop progress, cancellation, and cleanup.","principle":"Network I/O invoked from async routes must yield the event loop through timeout and cancellation.","root_cause":"The planned reuse of `DaemonClient.call_http_api` is synchronous, while wiki and attachment handlers are async; no async or worker-thread boundary is specified.","section_id":"5.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"hub-proxy-loop-prevention","description":"Misconfigured or aliased hub origins have no typed terminal outcome before timeout/resource exhaustion.","finding_id":"F17-proxy-loop","fix":"Add a files-proxy hop marker rejected on repeated remote forwarding, verify the authenticated target reports local files ownership before use, reject obvious self-origin cases, and test same-origin, alias, remote-to-remote, and two-node cycles.","location":"P1 / § 1.1 and P5 / § 5.1","prevention":"For every proxy-to-same-route design, validate target ownership and carry a bounded hop marker with cycle tests.","principle":"A same-path proxy must bound and reject self or remote-to-remote forwarding cycles.","root_cause":"Bootstrap validates only URL syntax, so a node can point `hub_daemon_url` to itself, another remote daemon, or a two-node cycle and recursively forward identical file routes until exhaustion.","section_id":"5.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"files-home-bind-loss","description":"A bind-loss race can silently create a new local directory rather than raising `FilesHomeError`.","finding_id":"F18-files-root-recreation","fix":"Add no-root-create files-home write primitives or equivalent root-held/revalidated mutation, use them for USER.md and attachment descendants, and test lost/replaced root behavior.","location":"P1 / § 1.1, P3 / § 3.1, and P5 / § 5.1","prevention":"For bind-owned roots, review every parent-creating write and inject root deletion/replacement between resolution and mutation.","principle":"Missing or lost `files_home` must remain a typed failure; descendant writes may create content only beneath a still-present owner root.","root_cause":"`durable_replace` and attachment upload use `mkdir(parents=True)`, so removal or unmount after `require_files_home` can recreate the configured mount path and write canonical data outside the bind.","section_id":"5.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"migration-crash-recovery","description":"The one-shot command can strand user data across old and new roots with no permitted recovery path.","finding_id":"F19-migration-recovery","fix":"Require a daemon-stopped or maintenance-fenced epoch and migration lock; stage, fsync, verify, and publish each source with a durable journal, delete sources only after publication, make reruns resume or roll back recognized states, and test failure after every source class.","location":"P6 / § 6.1","prevention":"Enumerate failure after every migration step and cross-filesystem copy, then specify exclusive ownership, durable state, resume/rollback, and injected-failure tests.","principle":"A multi-source destructive migration must have exclusive ownership and a durable resume or rollback path for every partial state.","root_cause":"Preflight prevents initial conflicts, while a crash, EXDEV copy failure, or concurrent daemon write after the first move leaves an incomplete destination that the specified second run refuses; no journal or maintenance exclusion exists.","section_id":"6.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"cross-store-restore-recovery","description":"A mid-restore failure can produce a durable cross-store split with no journal and allow restart into inconsistent state.","finding_id":"F20-restore-recovery","fix":"Preflight all artifacts and collisions, extract files into same-filesystem staging, run DB plus file publication under a durable maintenance journal, atomically publish only after prerequisites succeed, block daemon restart until completion, and add injected-failure tests around every boundary.","location":"P6 / § 6.2","prevention":"For each multi-store restore, test failure before and after every publication boundary and require durable completion/resume/rollback state.","principle":"Restoring related database and file state must preflight and recover as one maintenance campaign so generations cannot diverge silently.","root_cause":"Section 6.2 specifies collisions and a successful round trip, while current DB restore and unpack mutate final destinations sequentially; failure after either commits can leave attachment rows, wiki metadata, and files from different backup generations.","section_id":"6.2","severity":"blocking"}],"reviewer_session":"355cda97-8d5f-4edd-bf6e-802bed06ae0b","round":1,"verdict":"needs_review"},"session_id":"e18533a2-3d7e-4fa4-bdc4-df7028759faf"}
```

**Round 2** `kind: verification`

- reviewer_run:  cbb81bc5-0658-4642-a854-bcb3aa9df307
- reviewer_session:  df649f5a-2010-4ed0-8dd9-5a3075f6f372
- verdict: needs_review
- findings:
- F21-bootstrap-authoring / blocking / fresh install has no files_home acquisition or persist-before-identity order
- F22-bootstrap-consumer-fixtures / blocking / daemon-url and postgres/falkordb present-file fixtures omit files_home
- F23-websocket-chat-attachments-target / blocking / stored-chat bind lives in websocket/chat_attachments.py
- F24-personal-wiki-shared-resolver / blocking / scope_resolution.py requires repo_path for personal
- F25-compose-bind-runtime / blocking / resolve_compose_runtime does not project files_home
- F26-bundled-manifest-order / blocking / 6.3 intro edit leaves bundled digest stale
- F27-remote-wiki-command-protocol / blocking / MCP/scheduled commands lack hub HTTP routes; file paths must not cross machines
- F28-attachment-cleanup-ownership / blocking / remote row delete orphans hub bytes; unlink-after-delete is not retryable
- F29-proxy-streaming-bounds / blocking / buffered proxy cannot carry 100MB attachments or FileResponse ranges
- F30-files-home-root-capability / blocking / re-stat of the path string cannot detect a replaced bind
- F31-migration-writer-exclusion / blocking / exclusive lock does not exclude file-only writers
- F32-migration-registry-inventory / blocking / hub wiki registry omitted from migrate sources
- F33-archive-extraction-bounds / blocking / unpack reads whole members and can leave torn files
- F34-migration-baseline-tree / blocking / empty-source migrate never initializes the destination skeleton
- F35-gcore-compose-asset / blocking / gcore compose copy has no consumer and is already deleted
- resolution_notes: Accepted F21–F30 and F32–F35. Declined F31's live-daemon reader-writer lock; migrate requires the hub daemon stopped (Constraint 18). F27 repaired as additional `/api/wiki/...` routes plus streamed attach/ingest bytes, not a second RPC protocol. F29 preserves existing FileResponse headers rather than inventing a cache protocol. F30 is one verified-root device/inode helper. F35 removes the gcore compose target. Companion ledger must be re-pinned to the repaired plan hash before expansion.

```json plan-review-round
{"evidence_id":"a8305788-e82d-4db9-9ffe-57d4f12097fc","plan_hash":"d75f573ac262d871ea94fc470a352f7831689e3c764f7c66972810ece9fb4810","round_number":2,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"3dafedd79b608f68cbe9ab28b991bf22fd2f7fa55e04f601719b4900e452e8e2","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":4,"emitted_findings":15,"total":19},"evidence_id":"a8305788-e82d-4db9-9ffe-57d4f12097fc","lanes":[{"candidate_count":2,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":6,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":11,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":8,"manifest_digest":"9f4b58146e7c36fd1d365625f89f950c3544e5e01a870af42adf516204a9ed34","status":"valid"},"source_digest":"ae695b6156abdda73412ad75dc9d446dba921ae0bb8b78838ec03adef93e4580","version":1},"findings":[{"category":"missing-requirement","check_key":"fresh-install-files-home-provisioning","description":"A fresh local install has no specified way to obtain or persist the required absolute files_home before identity writes or managed PostgreSQL startup, so the repaired required-field contract makes the supported creation paths fail after partial side effects.","finding_id":"F21-bootstrap-authoring","fix":"Add a required local-install files_home input, validate that it is an existing absolute directory, target ensure_daemon_config and the PostgreSQL installer caller, render the value into template and fallback bootstraps, and reorder install so bootstrap persistence succeeds before personal identity or service startup. Add fresh-install and injected-bootstrap-write-failure acceptance tests.","location":"P1 / § 1.1 and P2 / § 2.1","prevention":"For every required bootstrap field, trace command input through every template, fallback writer, installer caller, and first runtime consumer, including the missing-file path.","principle":"A new required bootstrap field needs an operator acquisition path and must be persisted before its first consumer or any managed-service side effect.","root_cause":"Fresh install currently reaches personal identity before daemon config creation, while ensure_daemon_config and the PostgreSQL installer writer have no files_home input and are absent from Targets.","section_id":"1.1","severity":"blocking"},{"category":"traceability","check_key":"required-bootstrap-fixture-inventory","description":"Known indirect load_bootstrap consumers will violate the new required local field even if the named § 1.1 tests pass, leaving predictable failures outside the plan's target and validation surface.","finding_id":"F22-bootstrap-consumer-fixtures","fix":"Add tests/utils/test_daemon_url.py, tests/config/test_bootstrap_postgres.py, tests/config/test_bootstrap_falkordb.py, and any other present-file fixtures found by the class-wide sweep to § 1.1 Targets and V2; give present local fixtures absolute files_home values while preserving true missing-file default cases.","location":"P1 / § 1.1","prevention":"Run caller and fixture inventory for every shared parser change, then list every file that must change and every focused suite in validation.","principle":"Tightening a shared configuration contract requires every present-file fixture and focused suite that consumes it to be in the implementation inventory.","root_cause":"Daemon-URL, PostgreSQL-bootstrap, and FalkorDB-bootstrap tests create present local bootstraps without files_home, while § 1.1 Targets and V2 name only a subset of bootstrap suites.","section_id":"1.1","severity":"blocking"},{"category":"traceability","check_key":"stored-chat-websocket-target","description":"The remote upload-to-WebSocket bind repair is aimed at the wrong attachment module, so the actual stored-chat path can retain machine_id filtering and fail after a successful remote upload.","finding_id":"F23-websocket-chat-attachments-target","fix":"Replace or supplement the current WebSocket Target with src/gobby/servers/websocket/chat_attachments.py and its prepare/bind callers, keep the terminal-proxy attachment module explicitly machine-local, and make the remote upload-then-prepare_message_attachments bind path an acceptance test.","location":"P3 / § 3.1 and P5 / § 5.1","prevention":"Resolve similarly named subsystems through their callers before adding Targets, then test the end-to-end entry point that reaches the affected storage method.","principle":"A machine-identity repair must target the actual persisted-chat binder that performs the affected lookup and bind operations.","root_cause":"The plan targets src/gobby/servers/websocket/attachments.py, which handles daemon-local terminal attachments, while src/gobby/servers/websocket/chat_attachments.py calls get_attachments_by_ids and bind_attachments for stored chat uploads.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"checkout-free-personal-scope-resolution","description":"The checkout-free personal sentinel still cannot traverse the shared Python resolver used by routes, MCP, and scheduled jobs, and route-only reserved-name checks leave adjacent entry points inconsistent.","finding_id":"F24-personal-wiki-shared-resolver","fix":"Target src/gobby/wiki/scope_resolution.py, recognize PERSONAL_PROJECT_ID and reserved topic names there, return a first-class personal owner scope without requesting repo_path, and dispatch remote personal/topic scopes before local path existence checks. Cover HTTP, MCP, fresh scheduled registration, and parked-row recovery.","location":"P2 / § 2.1, P4 / § 4.1, and P5 / § 5.1","prevention":"Trace each semantic scope from HTTP, MCP, and scheduled registration through resolution, path lookup, dispatch, and gateway construction; test sentinels with repo_path absent.","principle":"A checkout-free semantic scope must be recognized before any shared resolver requires a checkout path.","root_cause":"Current scope_resolution.py resolves every project scope through LocalProjectManager.repo_path, so the personal sentinel can be rejected or parked before the planned downstream owner_dispatch and _gateway_for_resolved seams run.","section_id":"4.1","severity":"blocking"},{"category":"traceability","check_key":"compose-bind-config-projection","description":"The plan cannot materialize the configured files_home in Compose as written; adding gobby_files to the existing volume map either creates the wrong named-volume shape or leaves the host path unavailable to lifecycle tooling.","finding_id":"F25-compose-bind-runtime","fix":"Define gobby_files as lifecycle metadata in the canonical Python Compose asset, project a validated GOBBY_FILES_HOME value from bootstrap through src/gobby/cli/installers/compose_env.py, target that resolver and both Compose environment/template suites, and keep datastore volume enumeration unchanged.","location":"P6 / § 6.2","prevention":"For every new Compose interpolation, trace bootstrap value to rendered environment and template parsing, then include both environment and template contract suites.","principle":"A Compose lifecycle entry backed by runtime configuration needs an explicit configuration projection and contract tests.","root_cause":"The canonical Compose environment builder exports no files_home value, and § 6.2 targets neither resolve_compose_runtime nor its tests while requiring a configured host bind that is distinct from datastore named volumes.","section_id":"6.2","severity":"blocking"},{"category":"bad-sequencing","check_key":"bundled-content-manifest-finalization","description":"Executing the current leaf order leaves the intro skill's recorded bundled-content digest stale after § 6.3, even though § 1.1 previously regenerated the same manifest.","finding_id":"F26-bundled-manifest-order","fix":"Add src/gobby/install/bundled_content_manifest.json and tests/install/test_bundled_content_manifest.py to § 6.3, regenerate after the intro edit, and include that integrity test in V2.","location":"P1 / § 1.1 and P6 / § 6.3","prevention":"Inventory every bundled-content edit across all leaves and place manifest regeneration and its validation after the last dependent edit.","principle":"A generated digest manifest must be regenerated after the final bundled-content edit in the dependency order.","root_cause":"Section 1.1 regenerates bundled_content_manifest.json for bootstrap.yaml, then dependent § 6.3 edits the bundled intro skill without targeting the manifest or its integrity test.","section_id":"6.3","severity":"blocking"},{"category":"missing-requirement","check_key":"remote-wiki-command-matrix","description":"MCP and scheduled topic/personal work can reach commands that the proposed HTTP proxy cannot execute, and forwarding file paths would make the hub read paths from the wrong machine.","finding_id":"F27-remote-wiki-command-protocol","fix":"Specify one authenticated typed hub command endpoint covering the full GwikiGateway command matrix, wire HTTP, MCP, and scheduled callers through it, transmit file inputs as streamed bytes plus metadata, and add table-driven remote tests for every command and file-input variant.","location":"P5 / § 5.1","prevention":"Build a table of every gateway command and caller, then prove each argument and result is serializable across the owner boundary without interpreting a node path on the hub.","principle":"Every non-local operation needs a complete serializable owner protocol, including non-HTTP callers and file-bearing arguments.","root_cause":"Generic inbound method/path forwarding covers existing route calls, while MCP and scheduled GwikiGateway commands such as trust, refresh, export, sync-sessions, upkeep, librarian, and recap have no matching hub endpoints; attach and ingest can carry node-local paths.","section_id":"5.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"attachment-cleanup-owner-gating","description":"A remote maintenance loop can delete the only hub attachment locator and skip unlink, permanently orphaning canonical bytes; the same row-first order loses retryability when owner unlink fails.","finding_id":"F28-attachment-cleanup-ownership","fix":"Target cleanup_chat_attachments_loop and every bulk-delete caller, return before shared-row mutation on remote daemons, and on the owner unlink through the reconstructed resolver before deleting the row, treating FileNotFound as completed cleanup and retaining the row on other failures. Add remote-no-op and owner retry tests.","location":"P3 / § 3.1 and P5 / § 5.1","prevention":"For every DB-row-plus-file deletion, trace operation order, daemon ownership, retry state, FileNotFound behavior, and failure after each step.","principle":"Destructive metadata cleanup must retain the canonical locator until the storage owner has successfully removed the corresponding bytes.","root_cause":"Current cleanup deletes stale rows before unlinking, and the plan explicitly permits remote cleanup to delete or mark the shared row while skipping node-local unlink.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"bounded-proxy-streaming","description":"Remote attachments can be 100 MB each, so buffered proxying multiplies memory by concurrency and diverges from the local FileResponse contract even though the event loop remains technically asynchronous.","finding_id":"F29-proxy-streaming-bounds","fix":"Require DaemonClient streaming requests and StreamingResponse backpressure for upload/download, forward an explicit allowlist of Range and conditional request headers plus Content-Length, Content-Range, Accept-Ranges, and ETag responses, enforce upload limits while streaming, and add slow-consumer, range, cancellation, and maximum-size tests.","location":"P5 / § 5.1","prevention":"Test maximum-size concurrent transfers, ranges, slow consumers, disconnect cancellation, header parity, and client-resource closure for every file proxy.","principle":"A proxy for large file responses must preserve streaming, backpressure, cancellation, and representation semantics across the added hop.","root_cause":"The planned async raw DaemonClient method preserves final bytes and two headers, while ordinary httpx request handling buffers responses and the plan omits Range, conditional, length, and entity headers.","section_id":"5.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"files-home-root-identity","description":"The stated lost-or-replaced-bind failure rule is enforceable for neither replacement nor all writers; several planned paths can recreate the mountpoint or publish into a substituted directory.","finding_id":"F30-files-home-root-capability","fix":"Specify one verified-root contract that records the owner directory device/inode, revalidates it at publication, performs descendant operations without creating the configured root, and is used by Python profile/attachment/migrate/restore paths and Rust wiki initialization/registry writes. Add removal and replacement tests for each writer class.","location":"P3 / § 3.1 through P6 / § 6.2","prevention":"Inventory every files_home writer in each language and inject root removal plus device/inode replacement immediately before publication.","principle":"A missing-or-replaced bind invariant requires stable root identity and descendant-only writes across every writer class.","root_cause":"require_files_home re-stats only the configured path, while durable_replace, Rust registry writes, migration, unpack, and restore can recursively create parents or write after a replacement race without comparing directory identity.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"migration-writer-fence","description":"The accepted exclusive-lock repair does not currently exclude file-only owner writes, so migration preflight can become stale and a concurrent write can be moved, overwritten, or omitted.","finding_id":"F31-migration-writer-exclusion","fix":"Define a cross-process files-home lock outside the bind: profile, attachment, and wiki owner writes acquire it shared for their whole filesystem operation, while migrate acquires it exclusive from source discovery through registry publication. Target every acquisition site and add concurrent-write tests. Keep resume-on-rerun and the no-journal decision unchanged.","location":"P6 / § 6.1","prevention":"For any exclusive maintenance operation, enumerate every writer and test that each blocks from discovery through final publication.","principle":"An exclusive migration lock is effective only when every concurrent writer participates in the same lock protocol.","root_cause":"The existing maintenance epoch fences database connections and has no files migration campaign; profile, attachment, and wiki writes can proceed without it while § 6.1 assumes an exclusive maintenance lock prevents tree mutation.","section_id":"6.1","severity":"blocking"},{"category":"missing-requirement","check_key":"migration-registry-source-inventory","description":"Existing topic registrations can be lost or transformed nondeterministically because their persisted registry is absent from preflight and the resume state matrix.","finding_id":"F32-migration-registry-inventory","fix":"Add ~/wiki/wikis.json as an explicit source, define deterministic transformation of every registration to wiki-home-relative children, refuse malformed or unknown entries before source mutation, publish the registry after verified vault moves, and test multiple registrations plus a crash before registry publication.","location":"P6 / § 6.1","prevention":"For each migrated subsystem, inventory data files plus indexes/registries/metadata and test present, missing, malformed, partial-publication, and rerun states.","principle":"Migration preflight must inventory every persisted source whose contents affect post-migration behavior.","root_cause":"The source-class list includes ~/wiki/topics/<name> while omitting the sibling ~/wiki/wikis.json registry, and 'rewrite the registry' does not define preservation, filtering, malformed-entry handling, publication order, or rerun state.","section_id":"6.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"bounded-archive-extraction","description":"A large or compressed files_home member can exhaust memory, and interruption can leave a torn final file that the existing collision policy treats as authoritative on rerun.","finding_id":"F33-archive-extraction-bounds","fix":"Require fixed-size chunk extraction into same-filesystem temporary files, fsync and atomically replace each verified member, preflight explicit member-count and aggregate uncompressed-size limits before mutation, and add maximum-size, compressed-member, truncation, and rerun tests. Preserve the declined cross-store-journal decision.","location":"P6 / § 6.2","prevention":"Test maximum allowed members, aggregate declared size, compressed large members, truncation during copy, publication failure, and operator rerun.","principle":"Restoring an unbounded file tree requires bounded-memory extraction and atomic per-file publication.","root_cause":"Current unpack reads each tar member fully into memory and writes final paths directly, while § 6.2 adds large attachment/wiki members without chunk bounds, declared-size/member-count policy, or interrupted-member handling.","section_id":"6.2","severity":"blocking"},{"category":"missing-requirement","check_key":"migration-empty-source-baseline","description":"A clean hub with no legacy personal or wiki data can finish migration without creating the required USER.md, _personal subdirectories, wiki directory, and registry baseline.","finding_id":"F34-migration-baseline-tree","fix":"After validating the pre-provisioned files_home root, add a source-independent initialization step that creates empty USER.md, _personal/{notes,reminders,attachments}, and wiki plus its baseline registry without creating the root itself; add an empty-source acceptance test and keep later source moves collision-safe.","location":"P6 / § 6.1","prevention":"Test migrations with zero, partial, and complete legacy inputs, and separate destination initialization from source movement.","principle":"Baseline destination initialization is a source-independent migration obligation and needs an empty-input acceptance case.","root_cause":"The architecture creates the files-home skeleton before optional legacy moves, while § 6.1 skips missing sources and only asserts movement of present source classes.","section_id":"6.1","severity":"blocking"},{"category":"over-engineering","check_key":"single-canonical-compose-asset","description":"The new gcore Compose copy adds an unsynchronized surface, has no plan consumer, and guarantees failure of the existing standalone-removal regression.","finding_id":"F35-gcore-compose-asset","fix":"Remove crates/gcore/assets/docker-compose.services.yml from Targets and update only src/gobby/data/docker-compose.services.yml plus its Python lifecycle consumers and tests.","location":"P6 / § 6.2","prevention":"Before adding a duplicate asset, search consumers and deleted-surface regression contracts, then retain the canonical source unless a second runtime consumer is proven.","principle":"A duplicate generated or deployed asset needs a concrete consumer; one canonical asset is the proportional form when no second consumer exists.","root_cause":"Section 6.2 creates crates/gcore/assets/docker-compose.services.yml without naming a Rust consumer, while standalone_modules_and_assets_are_deleted explicitly requires that path to remain absent.","section_id":"6.2","severity":"blocking"}],"reviewer_session":"df649f5a-2010-4ed0-8dd9-5a3075f6f372","round":2,"verdict":"needs_review"},"session_id":"e18533a2-3d7e-4fa4-bdc4-df7028759faf"}
```

## T1 Task Mapping
`kind: framing`

| Plan Item | Task Ref | Status |
|-----------|----------|--------|
| Epic | #20330 | open |
