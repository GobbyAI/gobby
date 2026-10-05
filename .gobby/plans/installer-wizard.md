Plan artifact: `.gobby/plans/installer-wizard.md`

# Installer wizard: idempotent steps and the machine-role fork

**Plan ID:** installer-wizard

## Overview
`kind: framing`

This plan specifies #20151 (Author installer-wizard redesign plan: idempotent
reconciler, machine-role fork), under epic #22949 (Lane 7 - Planning/research).
Its expansion root is #23371 (Build the installer wizard from the #20151 plan).
#21295 (Add bundled MCP template checklist to the install wizard) is out of
scope; it sits in #22978 (Triage: needs Josh's decision) until the wizard is
built and tested.

**The problem.** Bare `gobby install` (`src/gobby/cli/install.py::install`)
is a fixed sequence of prompts and writes. Re-running it is not a no-op, and
it cannot set up a machine that joins a hub:
- Every run backs up and rewrites `~/.claude/settings.json`, Qwen's
  `settings.json` and Grok's `gobby.json`, and recopies every shared hook,
  plugin, doc and command file.
- A re-run that changes the embedding fails: the installer's
  `ConfigStore.set_embedding_bootstrap_values` refuses once any embedding key
  or collection exists, and nothing routes the change to the managed switch.
- The UI-exposure question ignores the stored intent and can only enable.
- Joining a hub means hand-writing a remote `bootstrap.yaml`, copying two
  files, running `gobby install`, then running `gobby auth login`. Making a
  hub means running `gobby datastores expose` by hand.

**When the leaves close:**
- The wizard asks for a machine role first: local (hub or solo), join a
  self-hosted hub, or join a managed hub. The last is designed here and
  reports a typed "not available yet".
- The five steps run in the order role, datastores, embedding, UI exposure,
  CLI hooks. Each reads its current state from the store that owns it, offers
  that state as the default, and writes only when the answer differs.
- Re-running `gobby install` with every default accepted makes the five
  steps write nothing: no bootstrap change, datastore exposure change,
  embedding config or secret write, switch request, UI exposure change, or
  CLI hook, settings or content write. The unlisted steps keep today's
  behavior (decision 14).
- A structural embedding change shows how many collections will be
  re-embedded, asks for confirmation, and starts the managed switch.
- Every wizard answer has a non-interactive flag that reaches the same step
  function.

## Decision Record
`kind: framing`

The Orchestrator (gobby#14972) ruled on Q1 to Q7 on 2026-10-05 (16:33 CT) and
accepted every recommendation. Q1 and Q7 need Josh's approval at plan
approval. At 17:31 CT it accepted the enhancer's E06 and E08 as decisions 13
and 14, which Josh sees at plan approval.

1. **Joining a self-hosted hub automates today's remote bridge (Q1, option
   (a); Josh approves).** The wizard prompts for the hub origin
   (`hub_daemon_url`) and the hub PostgreSQL DSN, and writes the remote
   bootstrap. The copied `.secret_kek` and `local_cli_token` are checked for
   presence only, by the existing remote preflight. After preflight passes,
   the wizard runs the `gobby auth login` enrollment. The DSN, key and token
   inputs retire when #21579 (Rust node duties) makes the node hold no
   datastore credential (ROADMAP decision 17). Rejected alternative (b):
   collect only the thin-node inputs and leave the bridge fields manual. It
   would leave today's only working join path hand-written until #21579
   lands.
2. **Hub sharing (Q2).** A local machine is asked "Will other machines join
   this hub?", defaulting to its current answer (No on a fresh install). Yes
   reuses the existing datastore exposure operation, with bind and host
   prompts. No on a current hub runs a new reverse operation (1.4).
3. **Structural embedding changes (Q3).** The wizard shows the number of
   managed collections the switch re-embeds and asks to proceed. On Yes it
   starts the managed switch after the daemon starts. If the daemon is not
   running it prints the exact `gobby embeddings switch` command.
   Non-interactive runs need `--confirm-reembed`. On an existing install, a
   structural change the current catalog switch cannot perform is refused
   before any write (1.6). An unchanged configuration is never refused,
   whatever its provider. The refusal does not promise that another plan
   unlocks the target. The local-inference plan's separate contracts are in
   `.gobby/plans/local-inference-runtime-foundation.md` sections 2.3, 3.2
   and 5.2.
4. **Unlisted steps keep today's behavior (Q4).** Identity, files home, rtk,
   voice, IDE settings, Git hooks and project init keep their orchestration,
   prompts and consumers. Git hooks already skip unchanged hooks
   (`install_git_hooks` reports "already installed").
5. **Managed hub (Q5).** The role step offers it and refuses it with a typed
   "not available yet" error. Its implementation is the deferred section D1,
   whose task is created at expansion and resolved through
   `deferral_task_map`.
6. **Language and ownership (Q6).** The wizard stays Python in
   `src/gobby/cli/`. Each step is a module-level function that the
   `src/gobby/cli/` inventory of #21571 (Operator verbs on the client) can
   classify later.
7. **Role changes (Q7; Josh approves).** The role step defaults to the
   machine's current role. Changing between local and self-hosted on an
   existing install is refused before any write. The remedy names the
   datastore move runbook (`.gobby/plans/completed/hub-pc-datastore-move.md`;
   the ruling cited its pre-archive path) or moving the Gobby home aside and
   installing again. Pointing `GOBBY_HOME` at a fresh directory is not offered
   until #23585 (Install paths ignore GOBBY_HOME) lands. That remedy is the
   deferred section D2. Hub and solo are the same role. Switching between
   them only toggles sharing (decision 2).
8. **Secret answers come from files (writer decision).** The hub DSN, hub
   password and embedding API key are never accepted as plaintext argv. Their
   flags name a file: `--hub-database-url-file`, `--hub-password-file` and
   `--embedding-api-key-file`. Interactive runs use hidden prompts. This
   follows the installer's existing rule that it has no plaintext
   `--embedding-api-key`.
9. **Defaults are section-level for the embedding (writer decision).** The
   existing keep-or-change gate (`should_configure_section`) shows the stored
   summary and defaults to keep. Choosing change re-asks the section's
   questions. The API-key prompt offers "blank keeps current". The classifier
   then writes nothing when the answers match the stored values (1.6).
10. **One leaf per step, wired by that leaf (writer decision).** Each step
    leaf implements its step, adds its flags to `install`, and tests both. The
    step leaves are ordered because they share `src/gobby/cli/install.py`.
    The cross-step re-run guarantee is a separate regression suite (1.8).
11. **The `embedding` component path is unchanged (writer decision).**
    `gobby install embedding` keeps today's fresh-bootstrap behavior. On an
    existing install it still reports the switch-lifecycle refusal from
    `set_embedding_bootstrap_values`. The wizard's reconciliation runs only in
    the full install.
12. **A fresh node bootstrap holds three keys (writer decision).**
    `datastore_mode: remote`, `database_url` and `hub_daemon_url`.
    `BootstrapConfig` defaults supply the ports, bind host and pool settings
    that `docs/guides/shared-stack.md` (Client setup) lists by hand today.
13. **A combined key replacement and structural change is refused (enhancer
    E06; Orchestrator ruling, 17:31 CT).** On a configured install, a run
    that both replaces the embedding API key and makes a structural change
    is refused before any write. The remedy is to replace the key in a
    separate key-only run, then retry the structural change. The switch
    request carries no key: `src/gobby/cli/embeddings.py::switch` posts only
    `catalog_key`, `provider` and `api_base`. Writing the shared key before
    a declined or failed switch would change the provider that is still
    active. A declined switch never writes the key. This plan adds no
    pending-secret state and leaves the switch API unchanged.
14. **The no-op guarantee covers the five wizard steps (enhancer E08;
    Orchestrator ruling, 17:31 CT).** With every default accepted, the role,
    datastores, embedding, UI exposure and CLI hooks steps write nothing.
    The unlisted steps keep today's orchestration (decision 4). For example,
    `run_daemon_setup` (`src/gobby/cli/install_setup.py`) still runs the
    global npm installs outside Homebrew mode. 1.8 proves the composed steps
    at their service boundaries.

## Scope Boundary
`kind: framing`

The task text names "local-inference-runtime-foundation P4". That plan now
asks this one to cite its sections 2.3 and 5.2 instead
(`.gobby/plans/local-inference-runtime-foundation.md`, lines 129-133):
- Its 2.3 supplies pre-daemon local-runtime detection
  (`src/gobby/ai/local_runtime/detection.py`, not yet created). Its 5.2
  supplies the library path and the post-start control contract.
- Its 3.2 rewrites the installer's local-family embedding branches
  (`_setup_lmstudio`, `_setup_ollama`, `_PROVIDER_CONFIG` in
  `src/gobby/cli/installers/embedding.py`) to write a `local_runtime`
  bootstrap block.

This plan owns the overall install flow, the role fork and the reconciliation
semantics. It leaves the provider setup branches, detection, the
`local_runtime` bootstrap block and local-family activation to that plan. It
edits only `_managed_embedding_collections_exist` in
`src/gobby/cli/installers/embedding.py`. That plan is not expanded, so the
two plans cannot be ordered. Whichever lands second rebases onto the other's
changes there. It also re-checks this wizard's stored-state reading
(`_embedding_state`), effective target and switch dispatch (1.6) against the
other plan's delivered contract.

## Constraints
`kind: framing`

- **Isolation.** Every pytest run uses
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1`
  and targets files, never the full suite. No leaf runs a real `gobby
  install`, touches the live `~/.gobby`, restarts the daemon, calls
  Tailscale or starts Docker. Tests use a temporary `HOME` and fakes at the
  Docker, Tailscale, daemon and hub-database boundaries.
- **No new wizard state file.** Each step reads and writes only its owning
  store:
  - `bootstrap.yaml` for the role, node inputs, API key, sharing flag,
    services bind address and UI intent;
  - ConfigStore for embedding values and the published datastore endpoints;
  - SecretStore for the embedding API key;
  - the installed hook, settings and content files for the CLI hooks.
- **Secrets.** No leaf logs, echoes or stores a DSN, password or API key
  outside its owning store. The wizard never opens `.secret_kek` or
  `local_cli_token`. Remote preflight's existing `_credential_errors`
  (`src/gobby/cli/installers/remote_preflight.py`) checks them with
  `is_file()`. The secret-file reader (`read_secret_file`, 1.5) reports the
  flag, the path and the exception type, never the file's contents. A hidden
  prompt never shows a secret as its default. New error handling never logs
  a raw exception from a secret-bearing call. Tests feed sentinel secrets
  and assert that the sentinel never appears in stdout, stderr or logs,
  including on failure paths.
- **Flag scope.** Every new flag applies to the full install only. When
  COMPONENTS are named, `install` rejects it with `click.UsageError("<flag>
  applies to the full install only.")`, the same way it rejects
  `--files-home`. Each step leaf adds this check for its own flags.
  Explicit flags override both the stored default and the prompt in every
  mode, and a step prompts only for answers without a flag. A flag of the
  other role is a `click.UsageError`: the sharing flags on a self-hosted run
  (1.4) and the hub flags on a local run (1.5).
- **Prompt order.** The five listed steps run in the task's relative order:
  role, datastores, embedding, UI exposure, CLI hooks. The unlisted steps
  (Decision 4) keep their positions between them. After 1.7 the full-install
  prompt sequence is:
  role, hub inputs (self-hosted), files home, project init, IDE settings,
  identity, rtk, hub sharing (local), embedding, UI exposure, then the CLI
  installers, Git hooks and voice.
- **No `src/gobby/config/` edits.** `BootstrapConfig.run_mode`,
  `bootstrap_path`, `read_bootstrap_yaml`, `write_bootstrap_yaml` and
  `update_bootstrap_yaml` are used as they are. A config-module edit would
  also require the runtime config contract carrier.
- **Large files.** No targeted production file reaches 850 lines. The largest
  are `src/gobby/cli/installers/codex.py` (794),
  `src/gobby/cli/installers/embedding.py` (760),
  `src/gobby/cli/datastores.py` (668) and `src/gobby/cli/install.py` (638).
  Step logic lives in new modules so that `install.py` gains only options
  and calls.
- **Granularity.** Each deliverable has at most six acceptance items and at
  most five production files. 1.5 keeps bootstrap publication and enrollment
  together, because enrollment's trigger is the publication diff (a new or
  changed hub origin). 1.6 has one classifier, which owns the key-only and
  switch outcomes.

## P1: Installer wizard
`kind: framing`

**Goal**: a re-runnable install wizard. It forks on machine role, offers
each step's stored state as the default, writes only on change, and accepts
every answer as a flag.

### 1.1 Shared installed content copies only changed files [category: code]
`kind: deliverable`

Targets:
- `src/gobby/cli/installers/shared.py::_install_file`
- `src/gobby/cli/installers/shared.py::install_global_hooks`
- `src/gobby/cli/installers/shared.py::_copy_plugins`
- `src/gobby/cli/installers/shared.py::_copy_docs`
- `src/gobby/cli/installers/shared.py::install_cli_content`
- `tests/cli/installers/test_shared.py::*` — scope-reason: adds the compare-first copy tests

**Research context:**
- `src/gobby/cli/installers/shared.py` (490 lines) copies installed content
  unconditionally:
  - `install_global_hooks` uses `copy2` plus `chmod(0o755)` for
    `validate_settings.py` into `~/.gobby/hooks/` (or `$GOBBY_HOOKS_DIR`).
  - `_copy_plugins` uses `copy2` for each `*.py` plugin, and `_copy_docs`
    for each doc, both called from `install_shared_content`.
  - `install_cli_content` deletes and recopies each command directory with
    `shutil.rmtree` and `copytree`, and recopies each command file.
- Each detected CLI installer calls `install_global_hooks` and
  `install_cli_content` on every run, so every re-run rewrites these files.
- `_install_file` (unlink, then `copy2`) has no callers anywhere in `src/`
  or `tests/` (`gcode grep -w _install_file src tests`). It is dead and is
  deleted here.
- Already compare-first, and not targeted:
  - `skill_install._write_router` compares bytes first;
  - `clean_project_hooks` writes only when it removed a hook;
  - the MCP config writers (`configure_mcp_server_json`,
    `configure_mcp_server_toml`) return `already_configured` without
    writing.
- Approach: one new helper, `copy_if_changed(source, target, *, mode=None)
  -> bool`, in `shared.py`. It returns False and leaves the target alone when
  the target is a regular file (not a symlink) with identical bytes and, if
  `mode` is given, the same permission bits. Otherwise it unlinks a symlinked
  target, copies with `copy2`, applies `mode`, and returns True. The symlink
  case keeps today's dev-mode migration.
- For command directories, a new `_tree_matches(source_dir, target_dir) ->
  bool` compares the two trees' relative file sets and each file's bytes
  (`filecmp.cmp(..., shallow=False)`). Only a mismatch triggers
  `rmtree` plus `copytree`, so files absent from the source are still
  removed.
- Rejected: per-file sync of command directories, which would need its own
  stale-file deletion logic.
- Tests prove a skip by spying on `copy2`, `copytree` and `rmtree` and by
  comparing the target's inode. `copy2` copies the source's `st_mtime_ns`, so
  an equal mtime alone does not prove that no copy ran.

**Implementation:**
- Delete `_install_file`. Add `copy_if_changed` and `_tree_matches`.
- `install_global_hooks`, `_copy_plugins`, `_copy_docs` and
  `install_cli_content` call `copy_if_changed`. Their returned name lists and
  dict shapes stay as they are; they still list every managed item, changed
  or not.
- `install_cli_content` recopies a command directory only when
  `_tree_matches` is False.

Consumers unchanged:
- `src/gobby/cli/installers/__init__.py` — no-edit-reason: re-exports install_global_hooks and install_cli_content by name; signatures are unchanged
- `src/gobby/cli/installers/agy.py` — no-edit-reason: calls install_global_hooks and install_cli_content and ignores whether files changed
- `src/gobby/cli/installers/droid.py` — no-edit-reason: calls install_global_hooks and install_cli_content and ignores whether files changed
- `tests/cli/installers/test_cli_installers_agy.py` — no-edit-reason: patches or observes the helpers by name with the unchanged return shape
- `tests/cli/installers/test_cli_installers_droid.py` — no-edit-reason: patches or observes the helpers by name with the unchanged return shape

**Focused verification (planned):** run
`tests/cli/installers/test_shared.py`,
`tests/cli/installers/test_cli_installers_agy.py` and
`tests/cli/installers/test_cli_installers_droid.py` with the isolation
prefix.

**Acceptance:**

- 1.1.1 - `install_global_hooks`, `_copy_plugins` and `_copy_docs` make no `copy2` call for a byte-identical regular target, which keeps its inode and `st_mtime_ns`, and replace a differing target. test: `tests/cli/installers/test_shared.py::test_shared_copies_skip_identical_targets`.
- 1.1.2 - `install_cli_content` makes no `copy2`, `copytree` or `rmtree` call for identical command files and command directories. It recopies a directory whose tree differs and removes target files absent from the source. test: `tests/cli/installers/test_shared.py::test_cli_content_recopies_only_changed_trees`.
- 1.1.3 - `copy_if_changed` replaces a symlinked target with a regular copy even when the bytes match, and applies `mode` when the permission bits differ. test: `tests/cli/installers/test_shared.py::test_copy_if_changed_replaces_symlink_and_fixes_mode`.
- 1.1.4 - `shared.py` defines `copy_if_changed` and no longer defines `_install_file`. file: `src/gobby/cli/installers/shared.py`.

### 1.2 CLI settings installers write only on change [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/cli/installers/claude.py::install_claude`
- `src/gobby/cli/installers/qwen.py::install_qwen`
- `src/gobby/cli/installers/qwen.py::_install_agent_scripts`
- `src/gobby/cli/installers/grok.py::install_grok`
- `src/gobby/cli/installers/codex.py::_install_hooks_file`
- `tests/cli/installers/test_cli_installers_claude.py::*` — scope-reason: adds re-run and changed-template tests
- `tests/cli/installers/test_qwen_installer.py::*` — scope-reason: adds the re-run test
- `tests/cli/installers/test_grok_installer.py::*` — scope-reason: adds the re-run test
- `tests/cli/installers/test_codex_installer.py::*` — scope-reason: adds the hooks.json re-run test

**Research context:**
- `install_claude` (`src/gobby/cli/installers/claude.py`, 524 lines):
  - When `settings.json` exists, it always writes a timestamped
    `settings.json.<ts>.backup`.
  - It merges the Gobby hooks, `autoMemoryEnabled` and the status line into
    the loaded settings.
  - It always rewrites the file through `tempfile.mkstemp` plus
    `os.replace`.
- `install_qwen` (`qwen.py`) always backs up `settings.json`, then merges and
  rewrites it. `_install_agent_scripts` copies `shared/scripts/*.sh` to
  `~/.gobby/scripts/` with `copy2` plus `chmod(0o755)` on every run.
- `install_grok` (`grok.py`) always backs up `~/.grok/hooks/gobby.json` and
  rewrites it from the template. `_disable_claude_hook_compat` already
  returns early when compat is disabled, so it is not targeted.
- `_install_hooks_file` (`codex.py`) merges the Gobby hook groups into
  `hooks.json` and always calls `_atomic_write_json`. The `config.toml`
  update in `install_codex` already writes only when
  `updated_config != parsed_config`.
- The pattern to copy is already in `agy.py` and `droid.py`:
  `hooks_changed = updated_settings != existing_settings`. They write and
  back up only when the merged settings differ or the file is missing, and
  set `result["already_configured"] = True` otherwise.
- `copy_if_changed(source, target, *, mode=None)` comes from 1.1 in
  `src/gobby/cli/installers/shared.py`.

**Implementation:**
- `install_claude`:
  - load the existing settings first;
  - build the merged settings on a deep copy, including the
    `autoMemoryEnabled` default and `_configure_statusline`;
  - when the merged settings equal the loaded ones and the file exists, skip
    the backup and the write, and set `result["already_configured"] = True`;
  - otherwise back up, then write, as today. The auto-memory provenance file
    is written only when `autoMemoryEnabled` was introduced, as today.
- `install_qwen`: the same compare-first order for its `settings.json`.
- `_install_agent_scripts`: call `copy_if_changed(script_file, target_file,
  mode=0o755)`.
- `install_grok`: build the hook config, compare it with the parsed existing
  `gobby.json`, and back up and write only when they differ or the file is
  missing.
- `_install_hooks_file`: call `_atomic_write_json` only when the merged
  dictionary differs from the loaded one or the file is missing.

Consumers unchanged:
- `src/gobby/cli/install_components.py` — no-edit-reason: calls the installers and reads success and error keys only
- `src/gobby/cli/installers/__init__.py` — no-edit-reason: re-exports the installers by name
- `src/gobby/mcp_proxy/tools/worktrees/_helpers.py` — no-edit-reason: calls install_claude and install_qwen and reads success only
- `tests/cli/test_install_components.py` — no-edit-reason: patches the installers by name
- `tests/e2e/composer_proof_setup.py` — no-edit-reason: calls install_claude and reads success only
- `tests/mcp_proxy/tools/test_worktrees_helpers.py` — no-edit-reason: patches the installers by name

**Focused verification (planned):** run the four installer test files above
plus `tests/cli/test_install_components.py` with the isolation prefix.

**Acceptance:**

- 1.2.1 - A second `install_claude` run with unchanged inputs makes no `os.replace` call, leaves `settings.json`'s inode, bytes and `st_mtime_ns` unchanged, creates no `settings.json.*.backup`, and reports `already_configured`. test: `tests/cli/installers/test_cli_installers_claude.py::test_install_claude_rerun_writes_nothing`.
- 1.2.2 - `install_claude` with a changed hook template writes one backup and the merged settings. test: `tests/cli/installers/test_cli_installers_claude.py::test_install_claude_backs_up_only_on_change`.
- 1.2.3 - A second `install_qwen` run makes no `settings.json` write and no `copy2` call for the agent scripts, and creates no backup. test: `tests/cli/installers/test_qwen_installer.py::test_install_qwen_rerun_writes_nothing`.
- 1.2.4 - A second `install_grok` run makes no `gobby.json` write, leaves its inode, bytes and `st_mtime_ns` unchanged, and creates no `gobby.json.*.backup`. test: `tests/cli/installers/test_grok_installer.py::test_install_grok_rerun_writes_nothing`.
- 1.2.5 - A second `_install_hooks_file` run makes no `_atomic_write_json` call for Codex `hooks.json`. test: `tests/cli/installers/test_codex_installer.py::test_codex_hooks_file_rerun_writes_nothing`.

### 1.3 Role step forks the install on machine role [category: code]
`kind: deliverable`

Targets:
- `src/gobby/cli/install_role.py`
- `src/gobby/cli/install.py::*` — scope-reason: adds the --role option, rejects it with COMPONENTS, and runs the role step before UI consent and preflight
- `tests/cli/test_install_role.py`
- `tests/cli/test_cli_install.py::*` — scope-reason: adds the role wiring test
- `tests/cli/test_install_coverage.py::*` — scope-reason: answers the new role prompt in its interactive install test

**Research context:**
- `install` (`src/gobby/cli/install.py`, lines 259-638, approximate) reads
  `raw_bootstrap = peek_install_bootstrap()` (a raw read of the GOBBY-home
  `bootstrap.yaml`, `{}` when it is absent). It takes
  `datastore_mode = raw_bootstrap.get("datastore_mode") or "local"` before
  UI consent and preflight. Nothing asks the user for a role.
- `BootstrapConfig.run_mode()` (`src/gobby/config/bootstrap.py`) returns
  `node` for remote mode, otherwise `hub` when `hub` is true, otherwise
  `standalone`. The role step reads the raw mapping with the same rule, so
  an invalid bootstrap fails later in the existing validation, as today.
- The wizard's three choices map onto the modes:
  - `local` means local datastores, standalone or hub (sharing is 1.4);
  - `self-hosted` means remote mode (1.5);
  - `managed` means a future registration-token join (D1).
- Q7 ruling: an existing install keeps its role. Hub and solo are both
  `local`.
- The remedy names the archived runbook
  `.gobby/plans/completed/hub-pc-datastore-move.md`, and moving the home
  aside. It does not suggest pointing `GOBBY_HOME` at a fresh directory,
  because `ensure_daemon_config` (`src/gobby/cli/install_setup.py`) still
  hardcodes `~/.gobby/bootstrap.yaml`. That defect is #23585 (Install paths
  ignore GOBBY_HOME), and the fresh-home remedy is deferred to D2.
- Today's interactive install tests that feed prompt input are few
  (`tests/cli/test_cli_install.py` has two `input=` runs and
  `tests/cli/test_install_coverage.py` has one). Every other install test
  uses `--no-interactive`, which keeps the current role.

**Implementation:**
- New `src/gobby/cli/install_role.py`:
  - `InstallRole = Literal["local", "self-hosted", "managed"]`.
  - `current_install_role(raw: Mapping[str, Any]) -> InstallRole | None`:
    None for an empty mapping, `self-hosted` for
    `datastore_mode: remote`, otherwise `local`.
  - `resolve_install_role(explicit, *, current, no_interactive, choose) ->
    InstallRole`. `choose(default)` is the interactive prompt. The default
    is `current` or `local`. An explicit `--role` wins in every mode and
    skips the prompt. Otherwise an interactive run returns `choose(default)`
    and a non-interactive run returns the default. `managed` raises
    `ManagedHubUnavailable`. A role different
    from a non-None `current` raises `RoleChangeRefused`.
  - `RoleChangeRefused(click.ClickException)` has the message: "This machine
    is installed as <current>. Gobby does not change a machine's role in
    place. To install a different role, move <gobby home> aside and run
    `gobby install` again. To move a hub's datastores to another machine,
    follow .gobby/plans/completed/hub-pc-datastore-move.md."
  - `ManagedHubUnavailable(click.ClickException)` has the message: "Joining
    a managed hub is not available yet. Choose local or self-hosted."
- `install`:
  - add `--role [local|self-hosted|managed]` (default None), and reject it
    when COMPONENTS are named;
  - right after `peek_install_bootstrap()`, call `resolve_install_role`
    with `choose=lambda default: click.prompt("Machine role",
    type=click.Choice(["local", "self-hosted", "managed"]),
    default=default)`;
  - set `datastore_mode = "remote" if role == "self-hosted" else "local"`.
  - Both refusals exit before UI consent, preflight or any write.

**Focused verification (planned):** run `tests/cli/test_install_role.py`,
`tests/cli/test_cli_install.py` and `tests/cli/test_install_coverage.py` with
the isolation prefix.

**Acceptance:**

- 1.3.1 - `current_install_role` returns None for no bootstrap, `self-hosted` for remote mode, and `local` for local mode with `hub` true or false. test: `tests/cli/test_install_role.py::test_current_install_role_from_bootstrap`.
- 1.3.2 - `resolve_install_role` defaults to the current role, interactively and without prompts. `--role` sets the role on a fresh install and skips the prompt. test: `tests/cli/test_install_role.py::test_resolve_install_role_defaults_to_current`.
- 1.3.3 - A role different from the current role raises `RoleChangeRefused`, whose message names the current role, the Gobby home and the runbook path. test: `tests/cli/test_install_role.py::test_role_change_on_existing_install_is_refused`.
- 1.3.4 - `managed` raises `ManagedHubUnavailable` on fresh and existing installs. test: `tests/cli/test_install_role.py::test_managed_role_is_typed_unavailable`.
- 1.3.5 - On a local install, `gobby install --role self-hosted --no-interactive` exits non-zero with the remedy before preflight and leaves `bootstrap.yaml` unchanged. `--role` with a COMPONENT is a usage error. test: `tests/cli/test_cli_install.py::test_install_refuses_role_change_before_mutation`.

### 1.4 Hub sharing step [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `src/gobby/cli/datastores.py::_commit_shared_endpoints`
- `src/gobby/cli/install_hub_sharing.py`
- `src/gobby/cli/install.py::*` — scope-reason: adds the sharing flags, rejects them with COMPONENTS, and runs the sharing step for the local role
- `tests/cli/test_datastores_expose.py::*` — scope-reason: adds the reverse-operation tests
- `tests/cli/test_install_hub_sharing.py`
- `tests/cli/test_cli_install.py::*` — scope-reason: adds the sharing wiring test

**Research context:**
- `expose_datastores(gobby_home, *, bind_address, published_host)`
  (`src/gobby/cli/datastores.py`, 668 lines) is the only writer of
  `hub: true`. It:
  - validates the bind (`validate_bind_address`: loopback or a local
    Tailscale IPv4) and the host (`validate_published_host`);
  - under `managed_services_lock`, writes `services_bind_address` and
    `hub: True` with `write_bootstrap_yaml`;
  - restarts the managed services (`_start_managed_services`);
  - publishes the endpoints with `_commit_shared_endpoints(gobby_home,
    published_host)`, which CAS-patches `databases.published_host`,
    `databases.qdrant.url` (`http://<host>:<qdrant port>`) and
    `databases.falkordb.host`;
  - on failure, rolls back with `_restore_compose_state`.
- No reverse operation exists. The installers fall back to `localhost` for
  Qdrant (`src/gobby/cli/installers/qdrant.py`) and to
  `DEFAULT_FALKORDB_HOST` for FalkorDB
  (`src/gobby/cli/installers/falkor.py`) when `databases.published_host` is
  unset. `DEFAULT_SERVICES_BIND_ADDRESS` is `127.0.0.1`.
- Current state: `hub` and `services_bind_address` from the raw bootstrap,
  and `databases.published_host` from the ConfigStore snapshot. The step
  runs after `prepare_install_state`, where `config_store` exists. It runs
  only for the `local` role; remote mode forbids `hub: true`.
- Today's hub setup in `docs/guides/shared-stack.md` (Hub setup) is
  `gobby install`, then the `gobby datastores` exposure command with `--bind`
  and `--host`.

**Implementation:**
- `datastores.py`:
  - `_commit_shared_endpoints(gobby_home, published_host: str | None)`.
    None unsets `databases.published_host`, sets `databases.qdrant.url` to
    `http://localhost:<qdrant port>`, and sets `databases.falkordb.host` to
    `DEFAULT_FALKORDB_HOST`.
  - New `unexpose_datastores(gobby_home) -> None` mirrors
    `expose_datastores`. Under the same lock it requires local mode, writes
    `services_bind_address: DEFAULT_SERVICES_BIND_ADDRESS` and `hub: False`,
    restarts the services, and calls `_commit_shared_endpoints(gobby_home,
    None)`. It uses the same rollback on failure and raises
    `DatastoreExposureError`.
- New `src/gobby/cli/install_hub_sharing.py`:
  - `HubSharing(share: bool, bind_address: str | None, published_host: str
    | None)`.
  - `resolve_hub_sharing(raw, current_host, *, share, bind, host,
    no_interactive, confirm, prompt) -> HubSharing`:
    - The default answer is `raw.get("hub") is True`.
    - A given flag supplies its answer in every mode and skips its
      question.
    - Interactive, for answers without a flag: `confirm("Will other
      machines join this hub?", default)`. When sharing is on, `prompt`
      asks for the Tailscale IPv4 bind and the published host, defaulting
      to the current values when the machine is already a hub.
    - Non-interactive: flag values, else the current values. Turning
      sharing on without `--datastores-bind` and `--datastores-host` is a
      `click.UsageError` naming both flags.
  - `apply_hub_sharing(decision, raw, current_host, gobby_home) -> str |
    None`:
    - no call when the share flag, bind and host all match the current
      state;
    - `expose_datastores` when sharing turns on or a hub's bind or host
      changes;
    - `unexpose_datastores` when it turns off, returning the notice
      "Machines joined to this hub lose datastore access."
    - It maps `DatastoreExposureError` to `click.ClickException`.
- `install`:
  - add `--share-datastores/--no-share-datastores` (default None),
    `--datastores-bind IPV4` and `--datastores-host NAME`, and reject them
    when COMPONENTS are named;
  - for the `local` role, run the step after `prepare_install_state`. Read
    `current_host` from `config_store.read_snapshot().values`. A required
    change with no `config_store` is a `click.ClickException`;
  - the three flags on a `self-hosted` run are a `click.UsageError`
    ("<flag> applies to the local role only.").
- `expose_datastores` keeps calling `_commit_shared_endpoints` with a host
  string, which keeps today's behavior.

**Focused verification (planned):** run
`tests/cli/test_datastores_expose.py`,
`tests/cli/test_install_hub_sharing.py` and `tests/cli/test_cli_install.py`
with the isolation prefix.

**Acceptance:**

- 1.4.1 - `unexpose_datastores` writes `services_bind_address: 127.0.0.1` and `hub: false`, restarts the services, unsets `databases.published_host`, and points Qdrant and FalkorDB at the local defaults. test: `tests/cli/test_datastores_expose.py::test_unexpose_datastores_restores_loopback_endpoints`.
- 1.4.2 - When readiness or endpoint publication fails, `unexpose_datastores` restores the previous bootstrap and compose state and raises `DatastoreExposureError`. test: `tests/cli/test_datastores_expose.py::test_unexpose_datastores_rolls_back_on_failure`.
- 1.4.3 - `resolve_hub_sharing` defaults to the current sharing state and the current bind and host. Turning sharing on without prompts requires both flags. In an interactive run, a given flag skips its question. test: `tests/cli/test_install_hub_sharing.py::test_resolve_hub_sharing_defaults_to_current`.
- 1.4.4 - `apply_hub_sharing` makes no call when nothing changed. It calls `expose_datastores` for a turn-on or a changed bind or host, and `unexpose_datastores` with the notice for a turn-off. test: `tests/cli/test_install_hub_sharing.py::test_apply_hub_sharing_mutates_only_on_change`.
- 1.4.5 - `install` runs the sharing step only for the `local` role and passes the three flags to it. The flags with a COMPONENT or on a self-hosted run are a usage error. test: `tests/cli/test_cli_install.py::test_install_hub_sharing_flags_reach_step`.

### 1.5 Join a self-hosted hub [category: code] (depends: 1.4)
`kind: deliverable`

Targets:
- `src/gobby/cli/install_join.py`
- `src/gobby/cli/install.py::*` — scope-reason: adds the hub flags, rejects them with COMPONENTS, publishes the node bootstrap before preflight and enrolls after it
- `tests/cli/test_install_join.py`
- `tests/cli/test_cli_install.py::*` — scope-reason: adds the join wiring test

**Research context:**
- Today's join (`docs/guides/shared-stack.md`, Client setup):
  1. hand-write a remote `bootstrap.yaml`;
  2. `scp` the hub's `.secret_kek` and `local_cli_token`;
  3. run `gobby install`;
  4. run `gobby auth login`.
- `install` passes the raw bootstrap's `database_url` and `hub_daemon_url`
  to `_run_install_preflight` (`src/gobby/cli/_install_daemon.py`). In remote
  mode that calls `run_remote_preflight`
  (`src/gobby/cli/installers/remote_preflight.py`). It reports a missing
  DSN, and `_credential_errors` reports a missing `.secret_kek` or
  `local_cli_token` (an `is_file()` check). The probe then reads the token to
  authenticate `/api/files/user-md`, then runs the PostgreSQL, shared config,
  Qdrant and FalkorDB probes. The wizard adds no presence check of its own.
- Bootstrap writes go through `src/gobby/config/bootstrap_io.py`:
  - `write_bootstrap_yaml(path, data)` publishes a whole mapping under the
    file lock;
  - `update_bootstrap_yaml(path, updater)` is the locked read-modify-write;
  - `bootstrap_path()` honors `GOBBY_HOME`.
  `_parse_mode_owner_fields` requires `hub_daemon_url` and forbids
  `files_home` and `hub: true` in remote mode. `BootstrapConfig` defaults
  cover the ports and pool, so a fresh node bootstrap needs only
  `datastore_mode`, `database_url` and `hub_daemon_url`.
- Enrollment is `enroll(request: LoginRequest, prompts: LoginPrompts) ->
  Enrollment` (`src/gobby/cli/auth_login.py`):
  - it reads the bootstrap and uses `hub_daemon_url` when `request.hub` is
    None;
  - for TLS it pins the hub certificate through `prompts.confirm(message)`,
    or `request.fingerprint` when given;
  - it takes the email from `request.email` or `prompts.email()`, and the
    password from `prompts.password()`;
  - it mints a machine key and publishes `api_key`, `api_key_id` and
    `hub_cert` into the bootstrap.
  `login` builds the prompts from `click.prompt` and `click.confirm`. The
  wizard reuses `enroll` unchanged. `enroll` takes the password only from
  `prompts.password()`, while the email can come from `request.email`.
- Full-command tests stub `run_remote_preflight` at its boundary. The
  wizard's helpers must not open the copied credentials. Preflight keeps its
  documented token read.

**Implementation:**
- New `src/gobby/cli/install_join.py`:
  - `read_secret_file(path: Path, *, flag: str) -> str` reads UTF-8 text and
    removes one trailing line ending only, so password whitespace survives.
    An empty value, `OSError` or `UnicodeDecodeError` is a
    `click.UsageError` naming the flag, the path and the exception type,
    never the contents. 1.6 reuses it for the key file.
  - `HubJoin(hub_daemon_url: str, database_url: str)`.
  - `resolve_hub_join(raw, *, hub_url, database_url_file, no_interactive,
    prompt) -> HubJoin`:
    - The defaults are the raw bootstrap's `hub_daemon_url` and
      `database_url`.
    - Interactive: "Hub URL" with the current origin as default, then the
      hidden "Hub PostgreSQL URL (blank keeps current)".
    - `--hub-url` and `--hub-database-url-file` (read with
      `read_secret_file`) supply their answers in every mode and skip their
      prompts.
    - A non-interactive run with no current value and no flag is a
      `click.UsageError` naming `--hub-url` and `--hub-database-url-file`.
    - The DSN is never echoed.
  - `publish_hub_join(join, raw, path) -> bool`:
    - with no bootstrap, `write_bootstrap_yaml` with exactly the three keys;
    - with changed values, `update_bootstrap_yaml` setting the two values;
    - with unchanged values, no write.
    It returns whether it wrote.
  - `enroll_if_needed(join, raw_before, *, email, password_file,
    fingerprint, no_interactive) -> Enrollment | None`:
    - It runs `enroll` only when `raw_before` has no `api_key` or its
      `hub_daemon_url` differs from `join.hub_daemon_url`.
    - It builds `LoginRequest(hub=None, email=email,
      fingerprint=fingerprint, label=socket.gethostname(), insecure=False)`.
    - Interactive `LoginPrompts` use `click.prompt` and `click.confirm`, as
      `login` does.
    - A given `--hub-password-file` (read with `read_secret_file`)
      supplies `LoginPrompts.password` in interactive and non-interactive
      runs.
    - Non-interactive: a missing email or password file is a
      `click.UsageError`, and certificate confirmation returns False, so a
      TLS hub needs `--hub-fingerprint`.
    - An enrolled node whose hub origin is unchanged needs no enrollment
      input.
- `install`:
  - add `--hub-url URL`, `--hub-database-url-file PATH`, `--hub-email
    EMAIL`, `--hub-password-file PATH` and `--hub-fingerprint SHA256` (file
    flags use `click.Path(exists=True, dir_okay=False, path_type=Path)`),
    and reject them when COMPONENTS are named;
  - for the `self-hosted` role, before preflight: resolve, publish, then
    re-read `peek_install_bootstrap()` so preflight sees the new values;
  - after preflight passes, call `enroll_if_needed` with the pre-publish
    mapping. A preflight failure exits as today, with no enrollment;
  - the five hub flags on a `local` run are a `click.UsageError` ("<flag>
    applies to the self-hosted role only.").

**Focused verification (planned):** run `tests/cli/test_install_join.py`,
`tests/cli/test_auth_login.py` and `tests/cli/test_cli_install.py` with the
isolation prefix.

**Acceptance:**

- 1.5.1 - `resolve_hub_join` defaults to the stored origin and DSN. It reads the DSN from `--hub-database-url-file` or the hidden prompt, where blank keeps the stored value. A given flag skips its prompt. A fresh non-interactive run without the inputs is a usage error naming both flags. test: `tests/cli/test_install_join.py::test_resolve_hub_join_defaults_and_file_inputs`.
- 1.5.2 - `publish_hub_join` writes a fresh bootstrap with exactly `datastore_mode: remote`, `database_url` and `hub_daemon_url`. It updates only changed values, and writes nothing when they are unchanged. test: `tests/cli/test_install_join.py::test_publish_hub_join_writes_only_on_change`.
- 1.5.3 - The join step never opens `.secret_kek` or `local_cli_token`. A sentinel DSN or password never appears in stdout, stderr or logs, including when a file read, resolution or enrollment fails. test: `tests/cli/test_install_join.py::test_hub_join_never_reads_copied_credentials`.
- 1.5.4 - `enroll_if_needed` calls `enroll` only when the bootstrap has no `api_key` or the hub origin changed. Without prompts it requires `--hub-email` and `--hub-password-file`, and refuses an unpinned TLS certificate unless `--hub-fingerprint` is given. `--hub-password-file` also supplies the interactive password, keeping its whitespace. test: `tests/cli/test_install_join.py::test_enroll_if_needed_runs_only_for_new_enrollment`.
- 1.5.5 - A fresh `gobby install --role self-hosted --no-interactive` with the hub flags publishes the bootstrap before preflight and enrolls after preflight passes. A failed preflight exits without enrolling. The hub flags on a local run are a usage error. test: `tests/cli/test_cli_install.py::test_install_join_runs_preflight_then_enrollment`.

### 1.6 Embedding step reconciles against the stored configuration [category: code] (depends: 1.5)
`kind: deliverable`

Targets:
- `src/gobby/cli/_install_state.py::EmbeddingInstallState`
- `src/gobby/cli/_install_state.py::_embedding_state`
- `src/gobby/cli/installers/embedding.py::_managed_embedding_collections_exist`
- `src/gobby/cli/_install_embedding_prompts.py::_run_embedding_install`
- `src/gobby/cli/_install_embedding_prompts.py::_get_embedding_api_key`
- `src/gobby/cli/_install_embedding_prompts.py::_select_embedding_model`
- `src/gobby/cli/install_embedding_change.py`
- `src/gobby/cli/install.py::*` — scope-reason: adds the embedding flags, rejects them with COMPONENTS, runs the embedding step before the CLI installers, and requests a pending switch after daemon start
- `tests/cli/test_install_state.py::*` — scope-reason: adds the catalog-key state test
- `tests/cli/test_install_embedding_change.py`
- `tests/cli/test_install_embedding_wizard.py::*` — scope-reason: adds the key-replacement test and moves the key mocks to the (key, replaced) return
- `tests/cli/test_install_coverage.py::*` — scope-reason: updates its EmbeddingInstallState fixtures and _run_embedding_install call assertions for the new keywords
- `tests/cli/test_cli_install.py::*` — scope-reason: adds the embedding wiring test

**Research context:**
- In `install`, `should_configure_section(install_state.embedding, ...,
  explicit=embedding.any_set)`
  (`src/gobby/cli/_install_state.py`) keeps a configured section unless the
  user answers Yes to "Change embedding provider/model/endpoint?". A run
  without prompts keeps it unless `--embedding-*` flags are given. The
  embedding block runs after the CLI installers today.
- `_run_embedding_install` (`src/gobby/cli/_install_embedding_prompts.py`,
  518 lines):
  - selects the provider (`_select_embedding_provider`);
  - gets the key (`_get_embedding_api_key`);
  - takes overrides (`_prompt_customization`) and, for vLLM, the server URL;
  - picks a catalog key (`_select_embedding_model`, skipped without prompts
    or when overrides are given);
  - calls `install_embedding`, which persists through
    `_persist_embedding_config` and
    `ConfigStore.set_embedding_bootstrap_values`.
  `set_embedding_bootstrap_values` raises `EmbeddingConfigMutationBlocked`
  when any embedding key or the key secret exists, when managed collections
  exist, or when a switch is active. A changed re-run therefore fails today.
- `_get_embedding_api_key` returns the stored key when one exists and never
  prompts again, so a stored key cannot be replaced.
- `ConfigStore` treats every embedding key except the API key as structural.
  Only sources `install` and `embedding_switch` may write them. The API key
  can be written through `ConfigStore.patch` with
  `ConfigPatch(secrets={AI_EMBEDDING_API_KEY_KEY: SecretUpdate(key)})`, using
  `apply_cas_config_patch` (`src/gobby/cli/config_writes.py`) on a
  `ConfigStore` bound to a `SecretStore`, as the FalkorDB installer does.
- The managed switch is started by `gobby embeddings switch <catalog_key>
  [--provider P] [--api-base URL]` (`src/gobby/cli/embeddings.py::switch`).
  It POSTs `{"catalog_key", "provider", "api_base"}` to
  `/api/embeddings/switch/start` through `get_daemon_client(timeout=30.0)`.
  `EmbeddingSwitchCoordinator.start` accepts a catalog key with provider
  `lmstudio`, `ollama` or `vllm`. vLLM resolves its served model itself.
- `_managed_embedding_collections_exist` (`src/gobby/cli/installers/embedding.py`)
  lists Qdrant collections and returns True on the first name whose parsed
  kind is in `CollectionNameResolver().kinds`.
- `EmbeddingInstallState` has `provider`, `model`, `api_base`, `dim` and
  `has_api_key`, and no catalog key. `_embedding_state` reads the model,
  API base, dim and key from the ConfigStore values.
  `AI_EMBEDDING_CATALOG_KEY` (`src/gobby/config/embedding_keys.py`) holds the
  catalog key.
- Provider default API bases are in `_PROVIDER_CONFIG`
  (`src/gobby/cli/installers/embedding.py`), which is read-only here (owned
  by the local-inference plan's 3.2).
- A local full install holds the install maintenance claim, so the daemon is
  down until `_maybe_start_daemon_after_install`. That starts it only
  interactively with a local UI. `_daemon_already_running()` reports the
  result.
- The switch resolves an API base only for vLLM. For every other provider,
  `EmbeddingSwitchCoordinator.start`
  (`src/gobby/ai/embedding_switch_service.py`) uses
  `_provider_api_base(provider)` (`src/gobby/ai/embedding_switch_runner.py`).
  It takes no model or dimension; both follow from the catalog key.
- `_select_embedding_provider` re-detects local providers, and for provider
  `none` it calls the installer itself before returning.
  `_prompt_customization` defaults blank answers to the provider defaults,
  not to the stored values. Neither suits a configured install.
  `_select_embedding_model` defaults its picker to `DEFAULT_CATALOG_ID`.
- The existing structural flags are `--embedding-url`,
  `--embedding-provider` (`lmstudio`, `ollama`, `openai-compatible` or
  `vllm`; requires `--embedding-url`), `--embedding-model` and
  `--embedding-dim`.
- Consumer sweep: `gcode grep -w
  '_get_embedding_api_key|_run_embedding_install|EmbeddingInstallState|resolve_installer_ui_exposure|apply_installer_ui_exposure|_commit_shared_endpoints'
  src tests -m 150`.
  - Production hits: `_install_embedding_prompts.py`, `_install_prompts.py`,
    `_install_state.py`, `datastores.py`, `install.py`,
    `install_components.py` and `ui_exposure.py`.
  - Test hits: `tests/cli/test_cli_install.py`,
    `tests/cli/test_datastores_expose.py`,
    `tests/cli/test_install_components.py`,
    `tests/cli/test_install_coverage.py`,
    `tests/cli/test_install_embedding_wizard.py`,
    `tests/cli/test_install_prompts.py` and `tests/test_ui_exposure.py`.
  - Each hit is a Target or a listed consumer in 1.4, 1.6 or 1.7.

**Implementation:**
- `_install_state.py`: `EmbeddingInstallState.catalog_key: str | None =
  None`, set by `_embedding_state` from `AI_EMBEDDING_CATALOG_KEY`.
- `installers/embedding.py`: the inspection counts the managed collections.
  A new `_managed_embedding_collection_count(config_store) -> int` holds the
  loop, and `_managed_embedding_collections_exist` returns `count > 0`, with
  unchanged error handling.
- New `src/gobby/cli/install_embedding_change.py`:
  - `EmbeddingTarget(provider, catalog_key, api_base, model, dim)`.
  - `resolve_embedding_target(current, *, provider, api_base, model, dim,
    catalog_key, no_interactive, prompt, pick_catalog) -> EmbeddingTarget`:
    - It starts from the stored values in `current` and replaces only the
      answers that a flag gives or a prompt changes.
    - Interactive runs ask for the provider, the catalog key
      (`pick_catalog`) and the API base, defaulting to the stored values,
      and skip each answer that has a flag.
    - When the provider changes and no API base is given, the API base is
      the new provider's default.
    - A run whose only embedding flag is `--embedding-api-key-file` asks no
      structural question.
    - It never runs provider detection or `_prompt_customization`.
  - `classify_embedding_change(current, target, *, key_replaced: bool) ->
    EmbeddingChange`, with the kinds `unchanged`, `key_only`, `switch` and
    `refused`, decided in this order:
    1. An identical normalized structure (provider, catalog key, API base,
       model and dim) is `key_only` when the key was replaced, else
       `unchanged`. This holds for every provider, including `none`,
       `openai` and `openai-compatible`.
    2. A structural change together with a replaced key is `refused`, with
       decision 13's two-run remedy.
    3. A structural change is `refused` in these cases:
       - the target provider is not `lmstudio`, `ollama` or `vllm`;
       - the target has no catalog key;
       - the model or dim differs from the stored value. The switch takes
         both from the catalog key, so the remedy names
         `--embedding-catalog`.
       - for `lmstudio` and `ollama`, the API base differs from
         `_provider_api_base(provider)`, which the switch uses. vLLM keeps
         its explicit API base.
    4. Otherwise it is `switch`.
    The refusal remedy reads: "This installed configuration cannot be
    changed through the current catalog switch. The wizard switches catalog
    models on LM Studio, Ollama and vLLM only." It does not promise that
    another plan unlocks the target.
  - `write_embedding_api_key(key) -> None` is the non-structural secret
    write described above.
  - `confirm_switch(change, count, *, no_interactive, confirm_reembed,
    confirm) -> bool`. Interactively it asks: "Switching to <key>
    re-embeds <N> collections. Proceed?", default No. Without prompts it
    returns `confirm_reembed`.
  - `request_embedding_switch(pending, *, daemon_running) -> None`. When the
    daemon runs, it POSTs the CLI's JSON body and echoes the returned status.
    An error response is printed as a warning together with the retry
    command. When the daemon is down, it prints `gobby embeddings switch
    <key> --provider <provider> --api-base <url>`, omitting `--api-base`
    when there is none.
- `_get_embedding_api_key`:
  - takes `api_key_file: Path | None = None`, read with `read_secret_file`
    (1.5); a file overrides everything;
  - returns `(key, replaced)`;
  - with a stored key and prompts, asks "Embedding API Key (blank keeps
    current)"; a non-blank answer different from the stored key sets
    `replaced`.
- `_select_embedding_model` takes `default_key: str | None = None`. The
  picker defaults to that key when it is in `picker_keys()`, else to
  `DEFAULT_CATALOG_ID`.
- `_run_embedding_install` takes `current: EmbeddingInstallState | None =
  None`, `catalog_override: str | None = None`, `api_key_file: Path | None =
  None` and `confirm_reembed: bool = False`. `catalog_override` replaces the
  catalog picker. When `current` is configured, it skips
  `_select_embedding_provider` and `_prompt_customization`. It resolves the
  target with `resolve_embedding_target`, using
  `_select_embedding_model(default_key=...)` as `pick_catalog`, gets the
  key, and classifies instead of calling the installer:
  - `unchanged`: records success and writes nothing;
  - `key_only`: calls `write_embedding_api_key`;
  - `refused`: records a failed result with the remedy;
  - `switch`: counts the collections and confirms. A declined switch
    records success and writes nothing. An accepted one records
    `results["embedding"]["pending_switch"]`.
  It still returns the provider string. A fresh install, and the component
  path that passes no `current`, keep today's path through the installer.
- Tests:
  - in `tests/cli/test_install_embedding_wizard.py`, every
    `_get_embedding_api_key` mock and direct assertion moves to the `(key,
    replaced)` return, including the missing-key and abort cases;
  - in `tests/cli/test_install_coverage.py`, the `EmbeddingInstallState`
    fixtures and `_run_embedding_install` call assertions take the new
    keywords.
- `install`:
  - add `--embedding-catalog KEY`, `--embedding-api-key-file PATH` and
    `--confirm-reembed`, and reject them when COMPONENTS are named;
  - count the first two as explicit for `should_configure_section`;
  - pass `install_state.embedding` as `current`;
  - move the embedding block before the per-CLI installer loop;
  - after `_maybe_start_daemon_after_install`, call
    `request_embedding_switch` for a pending switch with
    `daemon_running=_daemon_already_running()`.

Consumers unchanged:
- `src/gobby/cli/_install_prompts.py` — no-edit-reason: re-exports _run_embedding_install by name; the added keywords have defaults
- `src/gobby/cli/install_components.py` — no-edit-reason: the embedding component calls _run_embedding_install without current, which keeps the fresh-bootstrap path (Decision 11)
- `tests/cli/test_install_components.py` — no-edit-reason: patches _run_embedding_install by name
- `tests/cli/test_install_prompts.py` — no-edit-reason: patches _run_embedding_install by name with a return value
- `tests/cli/installers/test_embedding_installer.py` — no-edit-reason: patches _managed_embedding_collections_exist by name; its bool contract is unchanged

**Focused verification (planned):** run `tests/cli/test_install_state.py`,
`tests/cli/test_install_embedding_change.py`,
`tests/cli/test_install_embedding_wizard.py`,
`tests/cli/installers/test_embedding_installer.py`,
`tests/cli/test_install_coverage.py` and `tests/cli/test_cli_install.py` with
the isolation prefix.

**Acceptance:**

- 1.6.1 - `_embedding_state` reports the stored catalog key in `EmbeddingInstallState.catalog_key`. test: `tests/cli/test_install_state.py::test_embedding_state_reports_catalog_key`.
- 1.6.2 - `classify_embedding_change` returns `unchanged` for an identical target and `key_only` for a replaced key alone, for every provider including `none`, `openai` and `openai-compatible`. It returns `switch` for a changed catalog key or provider on `lmstudio`, `ollama` or `vllm`, or a changed vLLM API base. It returns `refused` for a structural change to a non-catalog provider, a missing catalog key, a changed model or dim, or a custom LM Studio or Ollama API base. test: `tests/cli/test_install_embedding_change.py::test_classify_embedding_change`.
- 1.6.3 - With a stored key, the key prompt offers "blank keeps current". A replacement from the prompt or `--embedding-api-key-file` writes only the API key secret through `ConfigStore.patch` and no structural key. A key-file-only run asks no structural question and ignores provider detection. A sentinel key never appears in stdout, stderr or logs, including when the file read fails. test: `tests/cli/test_install_embedding_wizard.py::test_replacement_key_writes_secret_only`.
- 1.6.4 - A switch asks for confirmation naming the managed collection count. Without prompts it proceeds only with `--confirm-reembed`. A declined switch, and a combined key replacement and structural change, write no secret and record no pending switch. test: `tests/cli/test_install_embedding_change.py::test_structural_change_requires_reembed_confirmation`.
- 1.6.5 - A pending switch is POSTed to `/api/embeddings/switch/start` with the CLI's body when the daemon runs. Otherwise the exact `gobby embeddings switch` command is printed. test: `tests/cli/test_install_embedding_change.py::test_pending_switch_posts_or_prints_command`.
- 1.6.6 - On a configured install, `gobby install` with defaults calls neither the embedding installer nor any config write. The embedding step runs before the CLI installers, and the new flags with a COMPONENT are a usage error. test: `tests/cli/test_cli_install.py::test_install_embedding_change_routes_through_switch`.

### 1.7 UI exposure step offers the stored intent [category: code] (depends: 1.6)
`kind: deliverable`

Targets:
- `src/gobby/ui_exposure.py::resolve_installer_ui_exposure`
- `src/gobby/ui_exposure.py::apply_installer_ui_exposure`
- `src/gobby/cli/install.py::*` — scope-reason: adds --expose-ui/--no-expose-ui, rejects it with COMPONENTS, and runs the UI step after the embedding step and before the CLI installers
- `tests/test_ui_exposure.py::*` — scope-reason: adds the default and change tests and passes current at the existing call sites
- `tests/cli/test_install_coverage.py::*` — scope-reason: updates its UI-exposure install test for the new step position and signatures
- `tests/cli/test_cli_install.py::*` — scope-reason: adds the UI step order and flag test

**Research context:**
- `resolve_installer_ui_exposure(explicit, *, full_install, no_interactive,
  confirm)` (`src/gobby/ui_exposure.py`, 392 lines):
  - returns `explicit` when set;
  - returns False without prompts or when `_read_node_info()` cannot reach
    Tailscale;
  - otherwise returns `confirm()`. `install` passes a `click.confirm`
    default of False and always passes `explicit=None`.
- `apply_installer_ui_exposure(expose, daemon_port)` returns None for False
  and calls `enable_tailscale_ui` for True. It never disables.
- The intent is `bootstrap.yaml`'s `ui_expose`, read by `_read_intent(path)`
  and written by `_write_intent`. `disable_tailscale_ui(daemon_port,
  config_path=...)` removes Gobby's root handler and clears the intent.
  `reconcile_ui_exposure` restores a stored intent at daemon readiness, so a
  degraded handler is repaired by the daemon, not by the wizard.
- In `install` today, the UI question is asked before preflight, and the
  answer is applied after project init.

**Implementation:**
- `ui_exposure.py`:
  - new `installer_ui_exposure_intent(config_path=None) -> bool` returns
    True when the bootstrap exists and `_read_intent` is not None;
  - `resolve_installer_ui_exposure(explicit, *, full_install,
    no_interactive, current: bool, confirm: Callable[[bool], bool])`. It
    returns `explicit` when set. It returns `current` without prompts, for a
    partial install, or when Tailscale is unavailable. Otherwise it returns
    `confirm(current)`.
  - `apply_installer_ui_exposure(expose, daemon_port, *, current: bool,
    config_path=None)` returns None when `expose == current`. It calls
    `enable_tailscale_ui` when turning on and `disable_tailscale_ui` when
    turning off.
- `install`:
  - add `--expose-ui/--no-expose-ui` (default None), and reject it when
    COMPONENTS are named;
  - move the resolve and apply pair to just after the embedding step and
    before the per-CLI installer loop;
  - `current = installer_ui_exposure_intent()` and `confirm=lambda
    default: click.confirm("Expose the web UI to your Tailscale network?",
    default=default)`;
  - keep today's warning on `UiExposeError` for both directions.
- Tests: the existing `resolve_installer_ui_exposure` calls in
  `tests/test_ui_exposure.py` pass `current` and assert `confirm(current)`.

**Focused verification (planned):** run `tests/test_ui_exposure.py`,
`tests/cli/test_install_coverage.py` and `tests/cli/test_cli_install.py` with
the isolation prefix.

**Acceptance:**

- 1.7.1 - `resolve_installer_ui_exposure` returns the explicit flag when set. It returns the stored intent without prompts or without Tailscale, and otherwise asks with the stored intent as the default. test: `tests/test_ui_exposure.py::test_resolve_installer_ui_exposure_defaults_to_current`.
- 1.7.2 - `apply_installer_ui_exposure` makes no Tailscale call when the answer equals the stored intent. It enables when turning on and disables when turning off. test: `tests/test_ui_exposure.py::test_apply_installer_ui_exposure_mutates_only_on_change`.
- 1.7.3 - `install` asks the UI question after the embedding step and before the CLI installers, and honors `--expose-ui/--no-expose-ui`. The flag with a COMPONENT is a usage error. test: `tests/cli/test_cli_install.py::test_install_ui_exposure_step_order_and_flags`.

### 1.8 Re-run no-op regression suite [category: test] (depends: 1.2, 1.7)
`kind: deliverable`

Targets:
- `tests/cli/test_install_rerun_noop.py`

**Research context:**
- The task's acceptance bar is that re-running with every default accepted
  is a no-op. 1.1 to 1.7 each prove their own step. This suite proves the
  composed `install` command.
- `install` (`src/gobby/cli/install.py`) reaches these external boundaries,
  which the suite fakes:
  - the managed stack (`_install_required_stack`) and
    `_provision_gdaemon_for_services`;
  - `run_daemon_setup`, `ensure_install_identity`, `reconcile_rtk_step` and
    `install_git_hooks`;
  - Tailscale (`ui_exposure._probe_serve_state` and `_read_node_info`);
  - the daemon (`_maybe_start_daemon_after_install`,
    `_daemon_already_running`);
  - the hub database: a `get_cli_runtime` stub returning recording
    ConfigStore and SecretStore fakes that count every write;
  - `expose_datastores`, `unexpose_datastores`, `enroll` and
    `install_embedding`, which the suite asserts are not called.
- The real CLI installers run against a temporary `HOME` with the bundled
  templates. The bootstrap is a real file under a temporary `GOBBY_HOME`.
- Existing harness patterns are in `tests/cli/test_cli_install.py` (the
  `CliRunner` runs and the patch map near line 91, approximate).

**Implementation:**
- New `tests/cli/test_install_rerun_noop.py`. One fixture builds a first
  install for each of two roles: a local hub with embedding, UI exposure and
  every detected CLI configured, and a self-hosted node with `api_key` set.
  `GOBBY_HOME` is the temporary `HOME/.gobby`, which keeps the fixture
  isolated until #23585 (Install paths ignore GOBBY_HOME) lands.
- The five step implementations run for real. Only their external
  boundaries are faked.
- After the first run, it resets every spy and counter, then snapshots:
  - the bootstrap's bytes, inode and `st_mtime_ns`;
  - each installed hook, settings and content file's bytes, inode and
    `st_mtime_ns`;
  - the set of `*.backup` files.
- Each test runs `install` again. Spies on `copy2`, `copytree`, `rmtree`,
  `os.replace`, `_atomic_write_json`, `write_bootstrap_yaml` and
  `update_bootstrap_yaml` must record no call for a managed artifact. The
  fakes must record no ConfigStore or SecretStore write and no call to
  `expose_datastores`, `unexpose_datastores`, `enroll`,
  `install_embedding`, the switch request, `enable_tailscale_ui` or
  `disable_tailscale_ui`. Every snapshot must be unchanged.
- The suite proves the five steps composed at their service boundaries.
  The unlisted steps stay faked, and it makes no claim about their
  filesystem activity (decision 14).

**Focused verification (planned):** run
`tests/cli/test_install_rerun_noop.py` with the isolation prefix.

**Acceptance:**

- 1.8.1 - A second `gobby install --no-interactive` on a local hub install makes no copy, replace or bootstrap write call for a managed artifact, leaves every snapshot unchanged, creates no `*.backup` file, and makes no ConfigStore, SecretStore, exposure, enrollment, embedding installer, switch request or UI exposure call. test: `tests/cli/test_install_rerun_noop.py::test_local_rerun_with_defaults_mutates_nothing`.
- 1.8.2 - A second `gobby install --no-interactive` on an enrolled self-hosted node meets every 1.8.1 assertion, including no bootstrap write and no `enroll` call. test: `tests/cli/test_install_rerun_noop.py::test_node_rerun_with_defaults_mutates_nothing`.
- 1.8.3 - An interactive re-run that accepts every prompt's default meets every 1.8.1 assertion, on both fixtures. test: `tests/cli/test_install_rerun_noop.py::test_interactive_defaults_match_non_interactive_rerun`.

### 1.9 Document the wizard [category: docs] (depends: 1.7)
`kind: deliverable`

Targets:
- `docs/guides/shared-stack.md`
- `docs/guides/cli-commands.md`
- `src/gobby/install/shared/skills/gobby/references/admin/installation.md`
- `src/gobby/install/shared/skills/gobby/references/intro/onboarding.md`

**Research context:**
- `docs/guides/shared-stack.md` (278 lines):
  - "Hub setup" runs `gobby install`, then the `gobby datastores` exposure
    command with `--bind` and `--host`;
  - "Client setup" hand-writes the remote bootstrap, copies `.secret_kek`
    and `local_cli_token` with `scp`, and runs `gobby install`.
  The enrollment command is documented separately.
- `docs/guides/cli-commands.md` has the `gobby install` modifier table, near
  line 270 (approximate), with the `--embedding-*` and `--no-interactive`
  rows.
- `references/admin/installation.md` says to start with `gobby install
  --help`, and calls the `datastores` exposure a transitional operator
  procedure.
- `references/intro/onboarding.md` (58 lines) says remote installation
  configures the hub origin and provisions the hub's credential.
- The flags come from 1.3 to 1.7:
  - `--role`;
  - `--share-datastores/--no-share-datastores`, `--datastores-bind`,
    `--datastores-host`;
  - `--hub-url`, `--hub-database-url-file`, `--hub-email`,
    `--hub-password-file`, `--hub-fingerprint`;
  - `--embedding-catalog`, `--embedding-api-key-file`, `--confirm-reembed`;
  - `--expose-ui/--no-expose-ui`.

**Implementation:**
- `shared-stack.md`:
  - Hub setup: answer Yes to "Will other machines join this hub?" (or pass
    `--share-datastores --datastores-bind <tailscale-ipv4>
    --datastores-host <hub-dns-name>`). The standalone `datastores` exposure
    command stays documented as the operator path.
  - Client setup: copy `.secret_kek` and `local_cli_token`, then run
    `gobby install` and choose `self-hosted`. Give the non-interactive form
    with the hub flags. Drop the hand-written bootstrap block. State that
    enrollment runs inside install.
- `cli-commands.md`: one modifier row per new flag. State that secret inputs
  take file paths, that new flags apply to the full install only, that a
  role change is refused, and that a combined key and structural embedding
  change is refused (decision 13).
- `installation.md` and `onboarding.md`: describe the role step, the
  five-step re-run guarantee (decision 14), and the role-change remedy.

**Acceptance:**

- 1.9.1 - The Hub setup section describes the hub-sharing question and its flags. behavior: "Will other machines join this hub?" in `docs/guides/shared-stack.md`.
- 1.9.2 - The Client setup section replaces the hand-written bootstrap with the self-hosted role and its file flags, and keeps the two copied files. behavior: "--hub-database-url-file" in `docs/guides/shared-stack.md`.
- 1.9.3 - The `gobby install` modifier table lists every new flag. behavior: "--confirm-reembed" in `docs/guides/cli-commands.md`.
- 1.9.4 - The installation reference describes the role step and the role-change refusal. behavior: "--role" in `src/gobby/install/shared/skills/gobby/references/admin/installation.md`.
- 1.9.5 - The onboarding reference describes joining a self-hosted hub through the role step. behavior: "self-hosted" in `src/gobby/install/shared/skills/gobby/references/intro/onboarding.md`.

## D1 Join a managed hub (depends: 1.3)
`kind: deferred`

The third role joins a hosted server daemon with a registration token
instead of a DSN and copied credentials. It is designed now:
- the role step offers `managed` and refuses it with
  `ManagedHubUnavailable` (1.3);
- the implemented branch prompts for the managed hub origin and a
  registration token, read from a file flag without prompts;
- it registers the machine for a machine-bound API key, as `enroll` does
  today;
- it writes a node bootstrap that holds no datastore credential.

It waits on hosted Gobby (ROADMAP story C, deferred) and the thin-node
runtime of #21579 (Rust node duties), so it cannot be specified against
current code.

```yaml
deferral:
  task_ref: "created-at-expansion"
  reason: "Hosted Gobby and the credential-free node runtime (#21579 Rust node duties) do not exist yet; the registration-token join cannot be specified against current code."
  owner: "orchestrator"
  original_acceptance_items:
    - D1.1
```

- D1.1 - Choosing `managed` prompts for the managed hub origin and a registration token (file flag without prompts), registers a machine-bound API key, writes a credential-free node bootstrap, and is a no-op on re-run.

## D2 Role-change remedy offers a fresh Gobby home (depends: 1.3)
`kind: deferred`

The `RoleChangeRefused` remedy (1.3) names only moving the Gobby home aside,
which works today. Pointing `GOBBY_HOME` at a fresh directory would also be a
remedy, but `ensure_daemon_config` (`src/gobby/cli/install_setup.py`) and
`src/gobby/cli/installers/claude.py` still write under `~/.gobby`. #23585
(Install paths ignore GOBBY_HOME) owns that fix. This plan does not absorb it.

```yaml
deferral:
  task_ref: "created-at-expansion"
  reason: "External prerequisite: #23585 (Install paths ignore GOBBY_HOME) must land before the role-change remedy can offer a fresh GOBBY_HOME."
  owner: "orchestrator"
  original_acceptance_items:
    - D2.1
```

- D2.1 - After #23585 lands, the `RoleChangeRefused` remedy also offers setting `GOBBY_HOME` to a fresh directory and running `gobby install` there.

## V1 Plan Changelog
`kind: verification`

- 2026-10-05: First draft by Plan Writer 3 (gobby#15468) under the
  Orchestrator's rulings Q1 to Q7 (2026-10-05, 16:33 CT). Facts were
  verified read-only on `0.5.0` at `361cdd224b`. Research notes are in
  `.gobby/plans/research/installer-wizard-context-20151.md`. The draft is
  narrative only, with no M1.
- 2026-10-05: Enhancer pass by `plan-enhancer-taskless-old` (run d0f45474)
  returned E01 to E09. The Orchestrator (gobby#14972) accepted all nine at
  17:31 CT. Applied:
  - 1.6's classifier compares the effective target first, so an unchanged
    configuration is never refused (E02). It refuses a model or dim change
    and a custom LM Studio or Ollama API base, which the switch cannot
    perform (E01).
  - The test consumers of changed signatures and the consumer sweep (E03).
  - Proof of a skipped write by spies and inode checks (E04).
  - Flag precedence and role-mismatched flags (E05).
  - Secret failure paths, through `read_secret_file` (E07).
  - Refusal remedy text and the second-lands re-check (E09).
  - E06 and E08 became decisions 13 and 14.
  Per the Orchestrator's ruling on #23585 (Install paths ignore
  GOBBY_HOME), the fresh-`GOBBY_HOME` remedy is deferred section D2, with
  #23585 as its external prerequisite.

## V2: Verification
`kind: verification`

After every leaf has landed:

1. Run the focused suites of 1.1 to 1.8 together against the test hub.
2. `uv run gobby install --help` lists every flag from 1.3 to 1.7 and no
   plaintext secret flag.
3. On a scratch machine or VM with a disposable home:
   - Install a local hub, then re-run `gobby install` and accept every
     default. `bootstrap.yaml`, `~/.claude/settings.json` and the hook files
     keep their `st_mtime_ns`, and no `*.backup` appears.
   - On a second machine, join it with the self-hosted role. A re-run
     accepting the defaults writes nothing and does not enroll again.
