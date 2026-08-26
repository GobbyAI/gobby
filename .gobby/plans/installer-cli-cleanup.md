# Installer CLI Cleanup

**Plan ID:** installer-cli-cleanup

## Overview
`kind: framing`

`gobby install` grew a flag per feature (`--claude … --agy`, `--hooks/--git-hooks`,
`--all`, `--config-only`, `--project`, `--rtk/--no-rtk`, `--voice`,
`--expose-ui/--no-expose-ui`, `--ide-settings/--no-ide-settings`,
`--falkordb-password-stdin`, `--secret-kek-posture`) and any flag with no scope silently
ran the full install. Josh's directive: the full install fires only on bare
`gobby install`; everything else names what it touches. This plan replaces the flags with
positional components, makes managed-service credentials random-by-default and never
rotated, removes the KEK-posture question from the installer, stops `gobby stop --docker`
from deleting containers, and folds in the defects found during the audit. It is the
tightening pass before the interactive wizard (separate, later work).

Confirmed Decision Record (2026-08-25, session #11102):

1. CLI shape: `gobby install [COMPONENT]...`; bare = the only full install. Components:
   `claude codex grok qwen droid agy git-hooks rtk impeccable voice embedding ide-settings`.
   `gobby uninstall [COMPONENT]...` mirrors it.
2. Component runs require an existing install and run only their component.
3. Credentials: random PostgreSQL DSN only when a fresh local `bootstrap.yaml` is created;
   existing DSNs reused verbatim, never rotated. FalkorDB password generated-only, in the
   SecretStore. KEK posture: installer step and flag removed; key file self-initializes;
   `gobby secrets rekey` untouched. Account creation stays interactive; fresh
   `--no-interactive` refuses. `gobby datastores rotate-password postgres|falkordb` is the
   supported rotation path for existing installs; data volumes are never touched.
4. Bare `gobby uninstall` removes everything installed (CLI hooks, global dispatchers, UI
   exposure, RTK rule + managed binary, Impeccable runtime) and never touches Docker,
   volumes, bootstrap, secrets, or files home. `git-hooks` is explicit-only.
5. `gobby stop --docker` uses `compose stop`; dead `uninstall_falkordb` is deleted.
6. Impeccable stays in the full install; `impeccable` is its component.
7. Fold-ins: FalkorDB `:?` in the Compose template; canonical values beat process env in
   the compose runtime. No `ui` component (`gobby ui expose/unexpose`); Tailscale is not
   required. Key-file KEK is the default and the only installer path.

## Constraints
`kind: framing`

- Restraint rung per choice: components replace ten booleans with one `click.Choice`
  argument and a dispatch table (rung 6, less code than today); every component reuses an
  existing installer function (rung 2); credential fixes reuse `secrets.token_urlsafe` and
  the existing `_resolve_falkordb_password` generation branch (rung 2/3).
- Full install order is unchanged: files-home claim → personal identity → banner → daemon
  config → managed stack → `run_daemon_setup` → account identity → RTK prompt → UI
  exposure prompt → local token → detected CLI installers → git hooks (if repo) →
  embedding → voice prompt → summary → daemon start. `--no-interactive` on a fresh
  datastore still refuses at `ensure_install_identity`.
- Component runs: `bootstrap.yaml` (via `gobby.config.bootstrap_io.bootstrap_path()`) and
  `~/.gobby/bin/gdaemon` must exist, else
  `click.UsageError("Gobby is not installed; run `gobby install` first.")`. They take no
  install-maintenance claim (`local_install_requires_maintenance` stays keyed on the full
  install), never call `_ensure_daemon_config`, `_install_required_stack`,
  `run_daemon_setup`, `ensure_install_identity`, `_provision_local_api_token`, or
  `_maybe_start_daemon_after_install`, and exit 1 when any component result lacks
  `success: True`.
- Surviving modifiers: `--no-interactive`, `-C/--path`, `--files-home`,
  `--container-restarts/--no-container-restarts`, `--embedding-url`,
  `--embedding-provider`, `--embedding-model`, `--embedding-dim`. Embedding overrides apply
  to the full install or the `embedding` component; with other components only, usage
  error `--embedding-* requires the embedding component`.
- Installer `mode="project"` parameters stay: `src/gobby/mcp_proxy/tools/worktrees/_helpers.py:135-181`
  calls `install_claude/qwen/droid(worktree_path, mode="project")` and
  `install_codex_project_hooks`; `tests/mcp_proxy/tools/test_worktrees_helpers.py` pins it.
  The CLI always passes `"global"`.
- Postgres: an existing `gobby-postgres` volume whose password differs from
  `bootstrap.yaml` cannot be recovered; the install fails with a message naming
  `bootstrap.yaml`'s `database_url` and the volume, and no path in Gobby ever runs
  `compose down`, `docker rm`, or `docker volume rm` against managed services (the only
  `docker rm` left is hub-backup's disposable scratch container).
- `src/gobby/data/docker-compose.services.yml` is protected by the installed
  `block-docker-policy-edits` rule; Josh toggles it for deliverable 2.2 and re-enables it.
- Production sizes: `src/gobby/cli/install.py` is 923 lines. 1.1 starts the split (moves
  `_reconcile_rtk_step` into new `src/gobby/cli/install_components.py`, drops the `mode`
  argument) and 1.2 finishes it, leaving `install.py` well under 1,000. `install_setup.py` 788, `_install_prompts.py` 769, `daemon.py` 884
  (untouched), `install_setup_impeccable.py` 881 (untouched).
- Ordering: `install_falkordb(password=None)` already defaults, so 1.2 drops the
  `--falkordb-password-stdin` flag and stops passing a password while 2.2 later deletes the
  parameter; no intermediate state is broken.
- Literal sweeps to rerun at implementation time (hit lists were gathered 2026-08-25):
  `gcode grep -F -- '--config-only' src tests docs README.md`,
  `gcode grep -F -- '--falkordb-password-stdin' src tests docs`,
  `gcode grep -F -- '--secret-kek-posture' src tests docs`,
  `gcode grep -F -- 'uninstall --tools' src tests docs`,
  `gcode grep -F -- 'install --hooks' src tests docs`,
  `gcode grep -F 'gobbyfalkor' src tests docs`,
  `gcode grep -F 'GOBBY_POSTGRES_PASSWORD' src tests docs`,
  `gcode grep -F 'uninstall_falkordb' src tests`. `web/` has no consumers (setup wizard
  deleted); `.github/` and `scripts/` have none.
- Out of scope: the wizard; passphrase KEK posture outside the installer (kept, via
  `gobby secrets rekey`); a `ui` component; `docs/reviews/*` and `wiki/` mirrors.
- Validation prefix for every pytest run:
  `DATABASE_URL="${DATABASE_URL:-postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test}" GOBBY_TEST_PROTECT=1 uv run pytest <paths> -q`
  run bare; then `uv run ruff format src/ tests/cli && uv run ruff check src/ tests/cli`,
  `uv run mypy src/`, and the test-types / suppressions / test-quality ratchets on touched
  test paths.

## P1: Component CLI
`kind: framing`

**Goal**: `gobby install` and `gobby uninstall` take positional components; bare install is
the only full install; bare uninstall removes everything installed and nothing else; the
installer stops asking about the KEK and stops reading a FalkorDB password.

### 1.1 Component registry, install guard, and shared setup helpers [category: code]
`kind: deliverable`

Targets:
- `src/gobby/cli/install_components.py::*` — scope-reason: new component registry and runner module
- `src/gobby/cli/install.py::*` — scope-reason: reconcile step moved out and the install() call sites updated
- `src/gobby/cli/install_setup.py::run_daemon_setup`
- `src/gobby/cli/_install_prompts.py::_run_standard_cli_install`
- `src/gobby/cli/_install_prompts.py::_run_standard_cli_uninstall`
- `src/gobby/cli/installers/git_hooks.py::*` — scope-reason: module-level hook script text and uninstall_git_hooks wiring
- `tests/cli/test_install_components.py::*` — scope-reason: new test module for the registry and runners
- `tests/cli/test_install_setup.py::*` — scope-reason: helper extraction tests
- `tests/cli/test_install_prompts.py::*` — scope-reason: meta table and runner signature tests
- `tests/cli/test_install_coverage.py::*` — scope-reason: reconcile_rtk patch target moves with the step

This deliverable starts the `install.py` split: `_reconcile_rtk_step` moves out of
`install.py` into the new `install_components.py` as `reconcile_rtk_step` (install.py imports
it; tests that patched `gobby.cli.install.reconcile_rtk` patch
`gobby.cli.install_components.reconcile_rtk`), and the `install()` call to
`_run_standard_cli_install` drops the `mode` argument. 1.2 finishes the split.

New module `src/gobby/cli/install_components.py`:

```python
COMPONENTS: tuple[str, ...] = (
    "claude", "codex", "grok", "qwen", "droid", "agy",
    "git-hooks", "rtk", "impeccable", "voice", "embedding", "ide-settings",
)
CLI_COMPONENTS = frozenset({"claude", "codex", "grok", "qwen", "droid", "agy"})
# voice, embedding, and ide-settings only write configuration; uninstall rejects them.
UNINSTALLABLE_COMPONENTS = ("claude", "codex", "grok", "qwen", "droid", "agy", "git-hooks", "rtk", "impeccable")

@dataclass(frozen=True)
class EmbeddingOverrides:
    url: str | None = None; provider: str | None = None; model: str | None = None; dim: int | None = None

def require_installed() -> None:
    """Component runs need an existing install."""
    from gobby.config.bootstrap_io import bootstrap_path
    from gobby.paths import get_gobby_home
    if not bootstrap_path().exists() or not (get_gobby_home() / "bin" / "gdaemon").exists():
        raise click.UsageError("Gobby is not installed; run `gobby install` first.")

def run_install_components(components, *, project_path, no_interactive, embedding: EmbeddingOverrides | None, runtime) -> dict[str, dict[str, Any]]
def run_uninstall_components(components, *, project_path, runtime) -> dict[str, dict[str, Any]]
```

`run_install_components` dispatches, in the order given, to existing functions:
CLI names → `_run_standard_cli_install(name, installer, project_path, "global", results, hook_timeout_seconds=runtime.require_config().hooks.provider_timeout)`;
`git-hooks` → `_run_git_hooks_install(install_git_hooks, project_path, results)` (usage
error when `project_path / ".git"` is missing); `rtk` →
`_reconcile_rtk_step(db, True, no_interactive=…)` recorded as `results["rtk"]`;
`impeccable` → `provision_impeccable(project_path)`; `voice` →
`_run_voice_install(results, voice_flag=True, no_interactive=…, db=db)`; `embedding` →
`_run_embedding_install(install_embedding, results, no_interactive=…, **overrides)`;
`ide-settings` → `configure_ide_terminals()`. `run_uninstall_components`: CLI names →
`_run_standard_cli_uninstall(name, fn, Path.home(), results, **({"mode": "global"} for qwen/droid))`;
`rtk` → `disable_rule_if_present(db)` + `remove_managed_rtk()`; `impeccable` →
`remove_impeccable_runtime()`; `git-hooks` → `uninstall_git_hooks(project_path)`. Each
records `results[name] = {"success": …}` in the existing shape.

Helpers extracted from `run_daemon_setup` into `install_setup.py` and called from it
unchanged: `provision_impeccable(project_path) -> ImpeccableInstallResult` (the
`install_impeccable_cli()` + echo + `reconcile_impeccable_installation` block) and
`configure_ide_terminals() -> None` (the `configure_ide_settings` block). Full-install
behavior and echo text are unchanged.

`_install_prompts.py`: add `"grok"` to `_CLI_UNINSTALL_META` (today
`gobby uninstall` on a machine with `~/.grok/hooks/gobby.json` raises `KeyError`);
delete dead `_run_codex_uninstall` (`_echo_migration_notice`, the per-project legacy
notice, goes with its caller in 1.2); drop the `mode` positional from
`_run_standard_cli_install` (always global) and the `project_subpath` column of
`_CLI_INSTALL_META`. `installers/git_hooks.py` line 208 hook text becomes
`gobby install git-hooks`.

**Acceptance:**

- 1.1.1 - `require_installed` raises the usage error when bootstrap or gdaemon is missing and passes when both exist. file: `src/gobby/cli/install_components.py`.
- 1.1.2 - `run_install_components` and `run_uninstall_components` dispatch every component name to the listed function with a recorded result. file: `src/gobby/cli/install_components.py`.
- 1.1.3 - `run_daemon_setup` output is unchanged after extracting `provision_impeccable` and `configure_ide_terminals`. test: `tests/cli/test_install_setup.py`.
- 1.1.4 - `_CLI_UNINSTALL_META` has a `grok` row and `_run_codex_uninstall` is gone. symbol: `gobby.cli._install_prompts._run_standard_cli_uninstall`.
- 1.1.5 - Installed pre-push hooks name `gobby install git-hooks`. file: `src/gobby/cli/installers/git_hooks.py`.

### 1.2 Rewrite `gobby install` around components [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/cli/install.py::*` — scope-reason: command rewrite; flag removal; KEK step removal; maintenance branches replaced by component dispatch
- `src/gobby/cli/install_components.py::*` — scope-reason: new component registry and runner module
- `src/gobby/cli/_install_prompts.py::_run_falkordb_install`
- `tests/cli/test_install_coverage.py::*` — scope-reason: TestInstallCommand and files-home lifecycle rewritten for components; KEK patch lines
- `tests/cli/test_cli_install.py::*` — scope-reason: help-text, per-CLI, config-only, remote-mode, TestSecretKekPostureInstall tests
- `tests/cli/test_cli_falkordb.py`
- `tests/cli/test_cli_falkor.py::*` — scope-reason: install/uninstall param assertions
- `tests/cli/test_install_front_door.py::*` — scope-reason: --all/--config-only invocations; KEK monkeypatch; _install_required_stack password kwarg
- `tests/cli/test_install_prompts.py::*` — scope-reason: _invoke_install callback signature; secret_kek_posture kwargs; TestFalkorDBInstallPrompt signature
- `tests/cli/test_cli.py::*` — scope-reason: bare install/uninstall smoke

Signature:

```python
@click.command("install")
@click.argument("components", nargs=-1, type=click.Choice(COMPONENTS))
@click.option("--no-interactive", "no_interactive_flag", is_flag=True, ...)
@click.option("--container-restarts/--no-container-restarts", default=True, ...)
@click.option("--files-home", ...)
@click.option("--embedding-url", ...)  # + provider/model/dim, unchanged
@click.option("-C", "--path", "working_dir", ...)
def install(components: tuple[str, ...], no_interactive_flag: bool, container_restarts_flag: bool,
            files_home: Path | None, embedding_url: str | None, embedding_provider: str | None,
            embedding_model: str | None, embedding_dim: int | None, working_dir: Path | None) -> None:
```

Body: the `--embedding-provider requires --embedding-url` check stays. If `components`:
usage error when embedding overrides are given without `embedding`; `require_installed()`;
`runtime = get_cli_runtime()`; `results = run_install_components(...)`; echo
`"<Name> component complete."` per component in order; exit 1 when any result failed;
`runtime.close()`. Otherwise the full install exactly as today minus: `all_flag`,
`config_only_flag`, `project_flag`/`mode`, `hooks_flag`, the per-CLI flags, `rtk_flag`
(full install passes `None` to `_reconcile_rtk_step`), `voice_flag` (prompt only),
`expose_ui_flag` (prompt only via `resolve_installer_ui_exposure(None, ...)`),
`ide_settings_flag` (prompt only), `falkordb_password_stdin`, `secret_kek_posture`, the
`explicit_scope`/`section_*`/`hooks_only_maintenance` derivation, the maintenance
branches, the `config_only_flag` early return, and the `_echo_migration_notice` call
(delete the function from `_install_prompts.py` too).

`install.py` is split: the component registry and runner move to `install_components.py`
(the module 1.1 creates) and the rewritten command module lands well below 900 lines.

KEK step: delete `_configure_secret_kek_posture`, the `--secret-kek-posture` option and
parameter, the `POSTURE_*`/`SECRET_KEK_PASSPHRASE_ENV` imports, and the call site. The key
file already self-initializes in `SecretStore._get_dek` →
`_initialize_envelope(posture=POSTURE_KEY_FILE)` (`storage/secrets.py:478-487`) on the
installer's own `SecretStore(db)` or first daemon use; nothing replaces the step. Storage
layer, `gobby secrets rekey`, schema columns, and their tests are untouched (the
`docs/contracts/secrets.md` wording moves in 3.1).

FalkorDB flag: `_install_required_stack` loses `falkordb_password`;
`_run_falkordb_install` becomes `(installer, results)` and calls
`installer(gobby_home=…)` with no password (the parameter, which defaults to `None`, is
deleted in 2.2). Keep the one-time "Generated FalkorDB password" echo.

Tests: delete `tests/cli/test_cli_falkordb.py`; rewrite `test_install_help` to assert the
component list and surviving options; convert per-flag tests to positional invocations
(`["claude"]`, `["git-hooks"]`, `["rtk"]`, `["voice"]`, `["embedding", "--embedding-url", …]`);
add `test_install_components_require_existing_install`, `test_install_rejects_embedding_overrides_without_component`,
`test_install_multiple_components_run_in_order`; `test_install_prompts._invoke_install`
passes the new keyword set; front-door tests use bare `[]` for the full install; delete
`TestSecretKekPostureInstall` and every `_configure_secret_kek_posture` patch line.

**Acceptance:**

- 1.2.1 - `gobby install --help` lists the twelve components and only the surviving options. test: `tests/cli/test_cli_install.py`.
- 1.2.2 - Bare `gobby install` runs the full install; `gobby install claude git-hooks rtk` runs those three and no daemon config, services, identity, or daemon start. test: `tests/cli/test_install_coverage.py`.
- 1.2.3 - Component runs without an install fail with the not-installed usage error. test: `tests/cli/test_install_coverage.py`.
- 1.2.4 - `install.py` is under 900 lines and the registry lives in `install_components.py`. file: `src/gobby/cli/install_components.py`.
- 1.2.5 - `gobby install --help` has no `--secret-kek-posture` or `--falkordb-password-stdin`, and `install.py` imports nothing from the posture constants. file: `src/gobby/cli/install.py`.
- 1.2.6 - A full install with a fresh SecretStore ends with key-file posture without any installer call to `set_kek_posture`. test: `tests/cli/test_cli_install.py`.
- 1.2.7 - `_run_falkordb_install(installer, results)` invokes the installer without a password argument. test: `tests/cli/test_install_prompts.py`.

### 1.3 Rewrite `gobby uninstall` around components [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `src/gobby/cli/uninstall.py::*` — scope-reason: command rewrite; --tools/--all/--project/--rtk removal
- `src/gobby/cli/install_components.py::*` — scope-reason: rtk uninstall branch tolerates an unreachable hub
- `tests/cli/test_install_coverage.py::*` — scope-reason: TestUninstallCommand rewritten
- `tests/cli/test_install_setup_impeccable.py::*` — scope-reason: --tools selector matrix becomes component matrix
- `tests/cli/test_install_setup_rtk.py::*` — scope-reason: --tools invocation
- `tests/cli/test_uninstall.py`
- `tests/cli/test_install_components.py::*` — scope-reason: rtk hub-unavailable tolerance tests
- `tests/cli/test_cli_install.py::*` — scope-reason: TestUninstallCommand converted to components
- `tests/cli/test_cli.py::*` — scope-reason: bare uninstall smoke with the hub offline
- `tests/cli/test_cli_falkor.py::*` — scope-reason: uninstall param assertion

```python
@click.command("uninstall")
@click.argument("components", nargs=-1, type=click.Choice(COMPONENTS))
@click.option("-C", "--path", "working_dir", ...)
@click.confirmation_option(prompt="Are you sure you want to uninstall Gobby hooks?")
def uninstall(components: tuple[str, ...], working_dir: Path | None) -> None:
```

Bare: detect installed CLIs from the existing global config paths (today's `all_flag`
branch, global paths only), then `run_uninstall_components(detected + ("rtk", "impeccable"))`,
remove the global hook dispatchers, `_teardown_ui_exposure()`, summary, exit 1 on failure.
"No Gobby hooks found" stays when nothing is detected, but rtk/impeccable cleanup still
runs. With components: `run_uninstall_components(components)` only; `git-hooks` uses
`-C`. Docker, volumes, bootstrap, secrets, and files home are never touched (there is no
code path; a test asserts no `docker` subprocess is spawned). Delete `--tools`, `--all`,
`--project`, `--rtk`, and the per-CLI flags; `tests/cli/test_uninstall.py` merges into
`test_install_setup_rtk.py`.
The `rtk` branch of `run_uninstall_components` tolerates an unreachable hub (warning,
`rule_disabled=False`) so a bare uninstall after `gobby stop --docker` still removes hooks
and the managed binary.

**Acceptance:**

- 1.3.1 - Bare `gobby uninstall --yes` removes detected CLI hooks, dispatchers, UI exposure, the RTK rule and managed binary, and the Impeccable runtime, and spawns no docker process. test: `tests/cli/test_install_coverage.py`.
- 1.3.2 - `gobby uninstall claude --yes` touches only Claude hooks and leaves the RTK rule enabled. test: `tests/cli/test_install_coverage.py`.
- 1.3.3 - `gobby uninstall rtk impeccable --yes` reproduces today's `--tools` matrix outcomes. test: `tests/cli/test_install_setup_impeccable.py`.
- 1.3.4 - `gobby uninstall git-hooks -C <repo> --yes` calls `uninstall_git_hooks` for that repo. symbol: `gobby.cli.installers.git_hooks.uninstall_git_hooks`.

## P2: Credentials and container safety
`kind: framing`

**Goal**: Managed-service passwords are random and stable, and nothing in Gobby deletes a
managed container.

### 2.1 Generate the PostgreSQL DSN on fresh bootstrap and let canonical values win [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `src/gobby/cli/install_setup.py::ensure_daemon_config`
- `src/gobby/install/shared/config/bootstrap.yaml::database_url`
- `src/gobby/cli/installers/postgres.py::_resolve_postgres_install_database_url`
- `src/gobby/cli/installers/postgres.py::_install_docker`
- `src/gobby/cli/installers/compose_env.py::resolve_compose_runtime`
- `tests/cli/installers/test_compose_env.py::*` — scope-reason: precedence tests flip
- `tests/cli/installers/test_postgres_installer.py::*` — scope-reason: DSN resolution and env tests
- `tests/cli/test_install_setup.py::*` — scope-reason: ensure_daemon_config creation tests

Today `ensure_daemon_config` copies the shared template whose `database_url` is
`postgresql://gobby:gobby_dev@localhost:60891/gobby`, and `publish_install_files_home`
calls it before `install_postgres`, so `_resolve_postgres_install_database_url`'s
`token_urlsafe` branch never fires (`docs/reviews/config.md:42`).

- Remove `database_url` from `src/gobby/install/shared/config/bootstrap.yaml` and from the
  literal fallback dict in `ensure_daemon_config`. When creating a bootstrap,
  `ensure_daemon_config` sets
  `data["database_url"] = f"postgresql://gobby:{secrets.token_urlsafe(32)}@localhost:60891/gobby"`
  (URL-safe alphabet; port from the existing `DEFAULT_POSTGRES_PORT`-style constant in
  `installers/postgres.py`, imported or duplicated as a module constant — pick the one
  that avoids a circular import). An existing bootstrap is never modified.
- `_resolve_postgres_install_database_url`: delete the generate branch and the
  `GOBBY_POSTGRES_PASSWORD` read; when the bootstrap has no `database_url`, return a
  failure via `click.ClickException("bootstrap.yaml has no database_url; run `gobby install`")`.
- `resolve_compose_runtime`: `environment = dict(os.environ) | canonical` so bootstrap,
  config-store, and SecretStore values win over the process environment for every key
  Gobby owns; explicit `overrides` still win last. Update the docstring.
- `_install_docker`: when `_wait_for_pg_isready` fails against a pre-existing
  `gobby-postgres` volume, the returned error names `bootstrap.yaml` `database_url` and the
  existing volume (`docker volume ls` name from the Compose project) and suggests either
  restoring the original bootstrap or removing the volume manually. No new docker calls.
- Tests: rewrite `test_postgres_installer.py::test_persisted_password_wins_over_env`-style
  cases so a persisted DSN beats `GOBBY_POSTGRES_PASSWORD`; `test_compose_env.py:104`
  asserts canonical beats env; `ensure_daemon_config` creation test asserts a random
  URL-safe password of ≥32 chars and no `gobby_dev` anywhere under `src/gobby/install`.

**Acceptance:**

- 2.1.1 - A fresh local bootstrap carries a random DSN and the shared template carries none. file: `src/gobby/install/shared/config/bootstrap.yaml`.
- 2.1.2 - Re-running `gobby install` with an existing bootstrap leaves `database_url` byte-identical even with `GOBBY_POSTGRES_PASSWORD` set. test: `tests/cli/installers/test_postgres_installer.py`.
- 2.1.3 - `resolve_compose_runtime` prefers canonical values over `os.environ` and still honors explicit overrides. test: `tests/cli/installers/test_compose_env.py`.
- 2.1.4 - A readiness failure against an existing volume returns a non-destructive error naming bootstrap.yaml and the volume. symbol: `gobby.cli.installers.postgres._install_docker`.

### 2.2 FalkorDB generated-only password and required template variable [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/cli/installers/falkor.py::_resolve_falkordb_password`
- `src/gobby/cli/installers/falkor.py::install_falkordb`
- `src/gobby/cli/installers/falkor.py::_install_falkordb_locked`
- `src/gobby/data/docker-compose.services.yml::*` — scope-reason: two GOBBY_FALKORDB_PASSWORD env lines
- `tests/cli/installers/test_falkordb_installer.py::*` — scope-reason: provided-source tests and template literal
- `tests/cli/installers/test_falkor_installer.py::*` — scope-reason: password_source assertions
- `tests/cli/installers/test_qdrant_installer.py::*` — scope-reason: compose template literal
- `tests/cli/installers/test_postgres_compose_template.py::*` — scope-reason: template checksum/literal
- `tests/cli/test_install_prompts.py::*` — scope-reason: TestFalkorDBInstallPrompt secret and echo assertions

- Drop the `password` parameter from `install_falkordb`/`_install_falkordb_locked` and the
  "provided" branch of `_resolve_falkordb_password`; sources are `reused` (SecretStore
  `falkordb_password`) and `generated` (existing 32-char `secrets.choice` loop). The
  installer CLI stopped passing a password in 1.2, so no caller changes.
- Template lines 15 and 17: `${GOBBY_FALKORDB_PASSWORD:?GOBBY_FALKORDB_PASSWORD must be set}`
  (same contract as `POSTGRES_PASSWORD` on line 93). `reconcile_unified_compose` already
  re-renders `~/.gobby/services/docker-compose.yml` on the next install/start, so existing
  installs pick it up without manual steps. Josh toggles `block-docker-policy-edits` for
  this edit.
- The installer-level tests drop the provided-source cases and pin the `:?` literal.

**Acceptance:**

- 2.2.1 - `install_falkordb` has no password parameter and resolves only reused or generated passwords. symbol: `gobby.cli.installers.falkor._resolve_falkordb_password`.
- 2.2.2 - The Compose template requires `GOBBY_FALKORDB_PASSWORD` on both lines and no `gobbyfalkor` literal remains in `src/` or `tests/`. file: `src/gobby/data/docker-compose.services.yml`.
- 2.2.3 - The generated password is echoed once and stored as the `falkordb_password` secret. test: `tests/cli/test_install_prompts.py`.

### 2.3 Stop containers without removing them and delete `uninstall_falkordb` [category: code] (depends: 2.2)
`kind: deliverable`

Targets:
- `src/gobby/cli/_daemon_services.py::_stop_managed_services_locked`
- `src/gobby/cli/installers/falkor.py::uninstall_falkordb`
- `src/gobby/cli/installers/__init__.py`
- `tests/cli/test_cli_falkor.py::*` — scope-reason: compose verb assertion
- `tests/cli/installers/test_falkordb_installer.py::*` — scope-reason: TestUninstallFalkorDB deletion
- `tests/cli/installers/test_docker_guard.py::*` — scope-reason: falkordb uninstall guard test deletion

- `_stop_managed_services_locked`: `command.append("stop")` instead of `"down"`; guard label
  `"managed-services compose stop"`; log text "stop" unchanged. Containers keep their
  identity and `unless-stopped` policy; `gobby start` brings them back with `up -d`.
- Delete `uninstall_falkordb` (compose `down` + config clear) and its export from
  `installers/__init__.py`; delete `TestUninstallFalkorDB` and
  `test_falkordb_uninstall_fails_closed_before_compose_down`
  (`test_managed_services_compose_down_fails_closed` still covers the guard on the stop
  path — rename it to match).
- `tests/cli/test_cli_falkor.py::test_services_stop_runs_compose_down` becomes
  `..._compose_stop` asserting `command[-1] == "stop"`.

**Acceptance:**

- 2.3.1 - `gobby stop --docker` issues `docker compose … stop` and never `down`. test: `tests/cli/test_cli_falkor.py`.
- 2.3.2 - `uninstall_falkordb` no longer exists and `gcode grep -F 'compose' src/gobby -m 100` shows no `"down"` argument outside tests. file: `src/gobby/cli/installers/falkor.py`.

### 2.4 Add `gobby datastores rotate-password postgres|falkordb` [category: code] (depends: 2.3)
`kind: deliverable`

Targets:
- `src/gobby/cli/datastores.py::*` — scope-reason: new rotate-password command in the existing datastores group
- `src/gobby/cli/installers/falkor.py::_update_config`
- `src/gobby/cli/installers/postgres.py::_write_bootstrap_defaults`
- `tests/cli/test_datastores_rotate_password.py`
- `docs/guides/cli-commands.md::*` — scope-reason: datastores command table

Existing installs stay on whatever password they were created with (`gobby_dev` for every
stock install so far), so a supported rotation path is what makes random-by-default
meaningful. One command, both services, no restart on its own:

```python
@datastores.command("rotate-password")
@click.argument("service", type=click.Choice(["postgres", "falkordb"]))
def rotate_password(service: str) -> None:
```

- Refuses when `bootstrap.yaml` `datastore_mode != "local"` (remote clients hold no
  datastore credentials).
- `postgres`: `new = secrets.token_urlsafe(32)`; connect with the current bootstrap DSN
  (psycopg, `connect_timeout=5`) and run `ALTER ROLE gobby PASSWORD %s`; then
  `_write_bootstrap_defaults(gobby_home=…, database_url=<DSN with new password>)` (the
  existing 0600 writer in `gobby.config.postgres_bootstrap.write_postgres_defaults`).
  Order matters: the role changes first so a failed bootstrap write leaves a DSN that no
  longer works only after the user is told the new password on stderr for manual repair.
- `falkordb`: `new` from the same generator as `_resolve_falkordb_password`; persist via
  `_update_config(port=DEFAULT_FALKORDB_PORT, password=new, gobby_home=…)` (CAS patch,
  `$secret:falkordb_password`). The running container keeps the old password until
  `gobby restart` recreates it on the `gobby_falkordb_data` volume.
- Both end with `click.echo("Run `gobby restart` to apply the new <service> password.")`;
  the password itself is never printed on success. Docker is never invoked.
- Tests: fake psycopg connection records the `ALTER ROLE` statement and the bootstrap file
  gains the new DSN; FalkorDB path asserts the CAS patch carries a `SecretUpdate` and no
  subprocess runs; remote mode exits 2.

**Acceptance:**

- 2.4.1 - `gobby datastores rotate-password postgres` alters the role and rewrites `database_url` with a 32-byte URL-safe password. test: `tests/cli/test_datastores_rotate_password.py`.
- 2.4.2 - `gobby datastores rotate-password falkordb` writes a new `falkordb_password` secret and spawns no docker process. test: `tests/cli/test_datastores_rotate_password.py`.
- 2.4.3 - Remote-mode installs are refused with exit 2. behavior: "rotate-password" in `docs/guides/cli-commands.md`.

## P3: Documentation
`kind: framing`

**Goal**: Every reference to a removed flag is gone and the component model is documented once.

### 3.1 Document components and remove stale flag references [category: docs] (depends: 2.4)
`kind: deliverable`

Targets:
- `docs/guides/cli-commands.md::*` — scope-reason: install/uninstall sections rewritten
- `docs/guides/system-requirements.md::*` — scope-reason: --config-only and password-stdin examples
- `docs/guides/remote-docker-acceptance.md::*` — scope-reason: stale --config-only --auth-mode commands
- `docs/guides/shared-stack.md::*` — scope-reason: --expose-ui references
- `docs/guides/voice.md::*` — scope-reason: gobby install --voice
- `docs/guides/configuration.md::*` — scope-reason: embedding flag list and install references
- `docs/architecture/development-guide.md::*` — scope-reason: per-CLI flag examples
- `docs/contracts/secrets.md::*` — scope-reason: installer opt-in sentence
- `src/gobby/install/shared/skills/impeccable/references/live.md::*` — scope-reason: uninstall --tools
- `src/gobby/install/shared/skills/impeccable/references/live-setup.md::*` — scope-reason: uninstall --tools

`cli-commands.md`: replace the install and uninstall option tables and the
scope/section/modifier paragraphs with: synopsis `gobby install [COMPONENT]...`, a
component table (what each installs/removes), the modifier table, the "bare = full
install; components require an install" rule, credential behavior (random Postgres DSN
on first install, generated FalkorDB secret, key-file KEK), and `gobby stop --docker`
stopping without removing containers. Other files: swap `--config-only` for bare
`gobby install` (the remote-acceptance guide's `--auth-mode` is already stale), `--voice`
→ `gobby install voice`, `--expose-ui` → `gobby ui expose`, `uninstall --tools` →
`gobby uninstall impeccable`. `docs/contracts/secrets.md:52` names
`gobby secrets rekey --posture passphrase` as the passphrase opt-in and drops the installer
sentence. After edits the Constraints literal sweeps return no hits under `docs/guides`,
`docs/architecture`, `docs/contracts`, or `src/gobby/install/shared`.

**Acceptance:**

- 3.1.1 - `docs/guides/cli-commands.md` documents the component model, modifiers, and credential defaults. behavior: "gobby install [COMPONENT]" in `docs/guides/cli-commands.md`.
- 3.1.2 - No removed flag remains in live docs or bundled skill references. file: `docs/guides/system-requirements.md`.
- 3.1.3 - The secrets contract names `gobby secrets rekey` as the passphrase opt-in. behavior: "passphrase opt-in" in `docs/contracts/secrets.md`.

## V2 Verification
`kind: verification`

1. Unit: `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/cli/test_install_coverage.py tests/cli/test_cli_install.py tests/cli/test_cli_falkor.py tests/cli/test_install_front_door.py tests/cli/test_install_prompts.py tests/cli/test_install_setup.py tests/cli/test_install_setup_impeccable.py tests/cli/test_install_setup_rtk.py tests/cli/test_cli.py tests/cli/test_daemon_coverage.py tests/cli/installers/ tests/mcp_proxy/tools/test_worktrees_helpers.py -q` — bare, exit 0.
2. Static: ruff format/check, `uv run mypy src/`, `uv run gobby test-types audit tests/cli --baseline .gobby/test-types-baseline.json --fail-on-new`, `uv run gobby test-types suppressions . --baseline .gobby/python-suppressions-baseline.json`, `uv run gobby test-quality audit tests/cli --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity low`.
3. Live (Josh's machine, existing install). First, a verified hub backup: `uv run gobby hub-backup` (writes `~/.gobby/backups/hub/<UTC timestamp>/` and proves each artifact restores; `hub-backup restore` is the rollback). Announce the restart to active sessions via `gobby-agents:send_message` and wait for a quiet window. Then `uv run gobby datastores rotate-password falkordb` then `uv run gobby restart` → daemon health green, `docker volume ls` unchanged; `uv run gobby datastores rotate-password postgres` then `uv run gobby restart` → health green, `~/.gobby/bootstrap.yaml` `database_url` carries the new password; `uv run gobby install --help`; `uv run gobby install rtk` → RTK status line, no banner; `database_url` unchanged by every install/uninstall invocation that follows; CLI round-trip on a CLI with no active Gobby session (never `claude` from inside a Claude session — it would remove the hooks this and the other live sessions run on; check `uv run gobby sessions list --source grok --status active` first): `uv run gobby uninstall grok --yes` (also proves the `_CLI_UNINSTALL_META` grok fix) then `uv run gobby install grok` → `~/.grok/hooks/gobby.json` restored, no daemon setup; repeat with `qwen`; `uv run gobby stop --docker` → `docker ps -a` still lists `gobby-postgres/qdrant/falkordb` (exited), `uv run gobby start` brings them back.
4. Fresh-install path: in a scratch `GOBBY_HOME` with `GOBBY_TEST_ALLOW_DOCKER` unset, `ensure_daemon_config(files_home=…)` produces a random DSN (unit test in 2.1); full Docker fresh install is a manual check when Josh next provisions a machine.

## V1 Plan Changelog
`kind: verification`

No review rounds (Lightweight).
