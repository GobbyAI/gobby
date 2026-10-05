# Working context for #20151 (Author installer-wizard redesign plan: idempotent reconciler, machine-role fork)

Working notes for Plan Writer 3 (gobby#15468). Verified on `0.5.0` on 2026-10-05.
Expansion root: #23371 (Build the installer wizard from the #20151 plan), which blocks
#21295 (Add bundled MCP template checklist to the install wizard; Josh deferred it until the
installer is built and tested).

## Current install flow (`src/gobby/cli/install.py::install`, lines 259-638)

Order today:
1. Detect CLIs.
2. `peek_install_bootstrap()` gives `datastore_mode`.
3. UI exposure consent (`resolve_installer_ui_exposure`, default No, applied only on Yes).
4. Preflight (`_install_daemon._run_install_preflight`).
5. `resolve_install_files_home`; then, local only, the maintenance claim,
   `publish_install_files_home` and `ensure_personal_project_identity`.
6. `_should_initialize_project`.
7. `ensure_daemon_config`.
8. Local only: `_install_required_stack` (postgres, qdrant, falkordb).
9. IDE settings consent, then `run_daemon_setup`.
10. `ensure_install_identity`.
11. `reconcile_rtk_step`.
12. Project init.
13. `apply_installer_ui_exposure`.
14. Local only: provision the local API token.
15. `prepare_install_state` (ConfigStore/SecretStore snapshot).
16. Per-CLI installers.
17. Git hooks.
18. Embedding (`should_configure_section`, "Change X?" default No).
19. Voice (same gate).
20. Summary.
21. `_maybe_start_daemon_after_install`.

Options: `--embedding-url/-provider/-model/-dim`, `--no-interactive`,
`--container-restarts/--no-container-restarts`, `--files-home`, `-C/--path`. `COMPONENTS`
(`install_components.py:63`) is the per-component reinstall list.

Existing reconcile shape: `_install_state.py`.
- `snapshot_install_state` reads ConfigStore overrides and SecretStore presence for the
  embedding, voice, qdrant and falkordb sections.
- `should_configure_section` is a keep/change gate. A configured section is kept unless the
  user says yes to "Change <label>?". It is not a per-field default.

## Machine role today

- `config/bootstrap.py::BootstrapConfig` has `datastore_mode` (`local|remote`), `hub: bool`,
  `files_home`, `hub_daemon_url`, `ui_expose`, `api_key`, `api_key_id`, `hub_cert`.
- `run_mode()` returns `node` when the mode is remote, otherwise `hub` if `hub` is true,
  otherwise `standalone`.
- `_parse_mode_owner_fields` enforces:
  - local requires `files_home` and forbids `hub_daemon_url`;
  - remote forbids `files_home` and `hub: true`, and requires `hub_daemon_url`.
- `hub: true` is written only by `cli/datastores.py::expose_datastores`
  (`gobby datastores expose --bind <tailscale-ipv4> --host <name>`). It also sets
  `services_bind_address` and publishes the qdrant and falkordb endpoints.

Joining a hub today (`docs/guides/shared-stack.md` §Client setup):
1. Hand-write a remote `bootstrap.yaml` containing the hub PostgreSQL DSN and
   `hub_daemon_url`.
2. `scp` the hub's `.secret_kek` and `local_cli_token`.
3. Run `gobby install`. `cli/installers/remote_preflight.py::run_remote_preflight` probes:
   the key and token files, `/api/files/user-md`, a PostgreSQL query, shared config and
   secrets, Qdrant, and FalkorDB.
4. Run `gobby auth login` (`cli/auth_login.py::enroll`). It requires a remote bootstrap,
   pins the hub TLS certificate, takes email and password, and mints an API key into
   bootstrap (`api_key`, `api_key_id`, `hub_cert`).

## Roadmap constraints (ROADMAP.md)

- Decision 13: the modes are standalone, hub and node. A node registers with a user-issued,
  machine-bound API key and holds no datastore credential.
- Decision 17 (2026-09-09) supersedes the description's "today's remote datastore mode". The
  node is thin; the datastores never leave the hub; `local_cli_token` retires once every
  caller holds a key. Hosted Gobby (story C) is one hub per customer and is deferred.
- `docs/guides/system-requirements.md:38-41`: today's `datastore_mode: remote` is "the
  transitional Python bridge, not the planned thin-node runtime".
- Stage 3: S3.1 inventories `src/gobby/cli/` as keep, absorb or drop, and S3.4 retires
  Python. The installer is not named in the roadmap.

## Embedding

- `cli/installers/embedding.py::_persist_embedding_config` calls
  `ConfigStore.set_embedding_bootstrap_values` (`storage/config_store.py:329-376`). That
  refuses with `EmbeddingConfigMutationBlocked` when:
  - any embedding key or the API key secret already exists, or
  - managed collections exist, or
  - a switch is active.
- So a re-run that changes the embedding fails today; it never switches silently.
- `ConfigStore` (line 201): every embedding key except `api_key` is structural, including
  `api_base`. Writes need source `install` or `embedding_switch`.
- The managed switch is `gobby embeddings switch <catalog_key>`, which calls the daemon at
  `/api/embeddings/switch/start`.
  - `ai/embedding_switch_service.py::start` and `ai/embedding_switch.py::start_switch` only
    accept catalog keys (`ai/embedding_catalog.py`: nomic-v1.5-*, qwen3-*).
  - A switch always runs stage, build (full corpus), flip and gc.
  - It needs a running daemon.
  - Non-catalog targets (openai, openai-compatible custom model) cannot be switched to.
- `_managed_embedding_collections_exist` (embedding.py:544) lists Qdrant collections
  through `CollectionNameResolver` kinds. Counting them gives the "re-embeds N
  collections" figure.

## UI exposure

`ui_exposure.py`:
- `resolve_installer_ui_exposure` returns False non-interactively or when Tailscale is
  absent. Otherwise it asks with default No.
- `apply_installer_ui_exposure` only enables.
- `_read_intent` and `_write_intent` hold the `ui_expose` intent in bootstrap.
- `get_ui_exposure_status`, `reconcile_ui_exposure`, `enable_tailscale_ui` and
  `disable_tailscale_ui` exist.
- Today the installer neither reads the current intent as its default nor disables.

## Inference-plan boundary

`.gobby/plans/local-inference-runtime-foundation.md` lines 129-133:
- #20151 owns installer-wizard UX and third-party runtime installation.
- That plan supplies pre-daemon detection (2.3 `src/gobby/ai/local_runtime/detection.py`,
  5.2 library path) and post-start control contracts.
- It asks #20151 to cite 2.3 and 5.2 in place of the old "P4".
- Its 3.2 rewrites the installer's local-family embedding branches to write a
  `local_runtime` bootstrap block plus `source="local"`.
- That plan has no coverage manifest and `src/gobby/ai/local_runtime/` does not exist, so
  it is not yet expanded or implemented.

## Related

- Memory afc4bcca: the installer has no plaintext `--embedding-api-key`; the key comes from
  a `$secret:` reference, SecretStore or a hidden prompt.
- Memory dd938a6e: `--ide-settings` is tri-state and consent-gated.
- #20145 (closed): the optional API-key prompt for every embedding provider.
