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

## Orchestrator rulings (gobby#14972, 16:33 CT, 2026-10-05; all recommendations accepted)

- Q1 (Josh approves at plan approval): option (a). The wizard automates the remote-datastore
  bridge:
  - it prompts for the hub origin and DSN;
  - it checks only that the copied `.secret_kek` and `local_cli_token` are present and never
    reads them;
  - it runs remote preflight, then folds in auth-login enrollment.
  The DSN, kek and token inputs retire with S4.1b #21579. Rejected alternative (b): collect
  only the thin-node inputs and leave the bridge fields manual.
- Q2: "Will other machines join this hub?", default No. Yes runs `expose_datastores` with
  bind and host prompts, and has flag parity.
- Q3: show the re-embed count and confirm. Start the switch after the daemon starts, or print
  the exact `gobby embeddings switch` command when the daemon is down. Non-interactive runs
  need an explicit confirm flag. A non-catalog target on an existing install is refused with
  a typed remedy that points at local-inference-runtime-foundation 3.2.
- Q4: identity, files_home, rtk, voice, IDE settings, git hooks and project init keep
  today's orchestration and consumers.
- Q5: managed hub is typed-unavailable, with an expansion placeholder deferral resolved
  through `deferral_task_map`.
- Q6: Python in `src/gobby/cli/`. The reconciler operations are the seam S3.1 #21571
  inventories later. The expansion root is #23371. #21295 is out of scope; it sits in
  Josh's deferred bucket #22978.
- Q7 (Josh approves at plan approval): the role step defaults to the current role. A role
  change on an existing install is refused with a typed remedy naming
  `.gobby/plans/hub-pc-datastore-move.md` or a fresh GOBBY_HOME. Only solo to hub and hub to
  solo happen in place, and they only toggle exposure.
- The scope boundary cites inference 2.3 and 5.2.

## Draft structure (writer's working plan; plan file not yet written)

Plan file: `.gobby/plans/installer-wizard.md`, Plan ID `installer-wizard`. Model the format
on `.gobby/plans/content-ownership-rules.md`:
- header: `Plan artifact:`, Plan ID, Overview, Decision Record, Constraints;
- `## P1: ...` with `kind: framing`;
- deliverables `### 1.N Title [category: code] (depends: ...)` with `kind: deliverable`,
  Targets, Research context, Implementation, Focused verification, and Acceptance
  (`- 1.N.M - ... test: \`path::test\``);
- `## V1 Plan Changelog`;
- `## V2: Verification`.
More than six acceptance items needs a Granularity statement.

Planned deliverables (steps are built and unit-tested in their own modules; `install.py`
is touched only in 1.7):
- 1.1 CLI hook installers write only changed files. Every run today backs up and rewrites
  `settings.json` (`installers/claude.py:251-266`, then the atomic write at line 341), and
  `installers/shared.py::_install_file` always unlinks and copies. Add one shared
  write-if-changed helper and use it across claude, codex, grok, qwen, droid, agy and the
  shared content. Back up only when the content changes.
- 1.2 Role step:
  - read the current role from bootstrap (`BootstrapConfig.run_mode`);
  - the ternary choice is hub/solo, join self-hosted, or join managed (typed-unavailable);
  - a role change on an existing install is refused with a remedy (Q7);
  - writes go through `config/bootstrap_io.py::update_bootstrap_yaml`.
- 1.3 Hub sharing toggle: "Will other machines join this hub?" Solo to hub uses
  `cli/datastores.py::expose_datastores`. Hub to solo needs a new reverse operation: reset
  `services_bind_address` to 127.0.0.1, set `hub: false`, and unpublish the endpoints.
  Check `validate_bind_address` and `_commit_shared_endpoints`.
- 1.4 Join self-hosted step:
  - prompt for the `hub_daemon_url` origin and DSN, and write the remote bootstrap;
  - check that `.secret_kek` and `local_cli_token` are present (existence only);
  - run `run_remote_preflight`, then `auth_login.enroll`.
  Secret answers take file or stdin flags, never plaintext argv (memory afc4bcca); this is
  a writer decision, flagged for the Adversary.
- 1.5 Embedding step:
  - snapshot defaults (`_install_state._embedding_state`); unchanged means no-op;
  - a fresh install uses the existing `set_embedding_bootstrap_values`;
  - a changed api_key only goes through a SecretStore write (non-structural);
  - a structural change needs a catalog key, a collection count N from Qdrant, a confirmation,
    and the switch start after daemon start (or the printed command);
  - a non-catalog target gets a typed refusal citing inference 3.2;
  - new flags: `--embedding-catalog` and `--confirm-reembed`.
- 1.6 UI exposure step: the default comes from `ui_exposure._read_intent` and
  `get_ui_exposure_status`; a change uses enable or disable; no change is a no-op. Flag:
  `--expose-ui/--no-expose-ui`.
- 1.7 Wizard composition and flag parity in `cli/install.py::install`:
  - order: role, datastores, embedding, UI exposure, CLI hooks;
  - the unlisted steps keep their place (Q4);
  - non-interactive runs use the same steps;
  - end-to-end no-op test: two runs with defaults mutate no bootstrap, config, secret or
    hook file. Test homes: `tests/cli/test_cli_install.py` and
    `tests/cli/test_install_state.py`.
- 1.8 Docs [docs]: `docs/guides/shared-stack.md` (Hub setup and Client setup) and
  `src/gobby/install/shared/skills/gobby/references/intro/onboarding.md`.
- Deferred section for join managed hub: `kind: deferred` with deferral YAML and a
  placeholder task_ref. Read `docs/contracts/plan-coverage.md` §Deferrals (line 289)
  first.

Tests that exist: `tests/cli/test_install_state.py`, `test_install_embedding_wizard.py`,
`test_cli_install.py`, `test_auth_login.py`, `test_datastores_expose.py`,
`tests/cli/installers/test_cli_installers_claude.py`, `test_codex_installer.py`,
`test_grok_installer.py`, `test_qwen_installer.py`, `test_cli_installers_droid.py`,
`test_cli_installers_agy.py`, `test_shared.py`, `test_embedding_installer.py`, and
`test_ui_exposure.py` (find its path).

Line counts: no target reaches 850. The largest are `installers/codex.py` 794,
`installers/embedding.py` 760, `cli/datastores.py` 668 and `cli/install.py` 638.
