# F1 facts: the live checkout (restart, cutover, schema inputs, startup reads)

Fact-finder F1 for #25629. Read-only. Every claim cites repo-relative path:line at the
0.5.0 HEAD of 2026-11-03 (17343de2b1). "Installed" means `~/.gobby/bin`. "Checkout" means
`/Users/josh/Projects/gobby`, which will sit on 0.9.0.

## 0. How the live daemon is wired today (verified on this machine)

- launchd runs `~/.gobby/bin/gdaemon serve` with WorkingDirectory set to the checkout.
  The backend command is `/Users/josh/Projects/gobby/.venv/bin/python3 -m gobby.runner`
  (`~/Library/LaunchAgents/com.gobby.daemon.plist`, rendered from
  `src/gobby/install/shared/services/com.gobby.daemon.plist.j2:8-24`).
- The venv holds an editable install. `__editable__.gobby-0.5.0.pth` contains
  `/Users/josh/Projects/gobby/src`. So every Python process imports the checkout's source.
- The direct start path builds the same pair: `daemon_start.py:238` (backend argv) and
  `daemon_start.py:247` (`gdaemon serve`).
- The supervisor respawns a crashed backend (`crates/gdaemon/src/lifecycle/backend.rs:176-280`).
  A backend that exits 78 (a StartRefusal, `src/gobby/runner.py:642-644`) makes the supervisor
  stop with `SupervisorExit::Refused` (`backend.rs:228-237`).
- Claude sessions launch `~/.gobby/bin/gmcp` as the gobby MCP server (`~/.claude.json`).
  Every hook command is `~/.gobby/bin/ghook` (`~/.claude/settings.json`).
- A Vite dev server is live: node PID 90081 listens on 60889 with cwd
  `/Users/josh/Projects/gobby/web` (lsof).

## (a) Migrations and schema inputs

### Where they live

- SQL: `crates/gcore/assets/schema/baseline.sql` (baseline v420) and
  `crates/gcore/assets/schema/migrations/421_*.sql` through `487_*.sql` (67 files).
- The chain is compiled into the binaries. `crates/gcore/src/schema/assets.rs:3-9` embeds the
  baseline, `seed.manifest.json` and `catalog.manifest.json` with `include_str!`.
  `assets.rs:24-465` is a hand-maintained `MIGRATIONS` array with each file's checksum.
- The identity has six fields: runner_protocol, baseline_version, baseline_checksum,
  latest_version, latest_checksum, assets_root_hash
  (`src/gobby/storage/schema_identity_pin.py:18-20`; `crates/gcore/src/schema/identity.rs:23-68`).
  The root hash covers the baseline, every migration filename and checksum, and both
  manifests (`assets.rs:502-519`).
- The Python carrier of the same identity is the checkout pin
  `src/gobby/storage/schema_expected_identity.json` (now v487). It is read with
  importlib.resources on every call, no cache (`src/gobby/storage/schema_contract.py:33-40`).
  It is regenerated from a built gdaemon by `scripts/generate_schema_expected_identity.py`.
- `src/gobby/storage/migrations/` is an empty directory that git does not track. No Python
  migration SQL remains.
- All three set members embed the identity: `gdaemon schema version --json`, and
  `gcode`/`ghook schema-identity --json` (`src/gobby/install/bin_set_coherence.py:53-85`;
  `src/gobby/utils/native_bin.py:11`).
- Cutover treats three paths as schema inputs: `crates/gcore/assets/schema`,
  `crates/gcore/src/schema`, and the pin (`src/gobby/cli/cutover.py:47-53`).

### Self-checks inside the chain

- `verify_embedded_assets` checks the baseline checksum, a contiguous version chain, and each
  migration checksum (`crates/gcore/src/schema/runner.rs:199-222`). Two worktrees cannot both
  add migration 488.
- `resolve_pending_migrations` refuses a hub receipt with no embedded asset, or with a
  different filename or checksum (`crates/gcore/src/schema/runner_plan.rs:258-271`). Editing a
  migration after it was applied anywhere breaks plan, preflight and apply.
- `validate_lineage` refuses a hub head newer than the binary
  ("database schema vN is newer than this runner", `runner_plan.rs:153-169`).
- A gcore unit test asserts the packaged pin equals the Rust-embedded identity
  (`crates/gcore/src/grant/tests.rs:1742-1749`). A stale pin fails `cargo nextest -p gobby-core`.
- Landing refuses a candidate that changes migrations, `baseline.sql` or `assets.rs` without
  also changing the pin (`src/gobby/tasks/landing_policy.py:76-91`;
  `src/gobby/tasks/land_commit.py:649-651`, blocker `schema_carriers_missing`).

### Who applies them, and when

- Only the installed gdaemon applies schema. Python shells out to
  `~/.gobby/bin/gdaemon schema apply` (`schema_contract.py:97-142`, `:163-173`;
  `src/gobby/storage/hub/postgres.py:406-407`). gdaemon applies only the migrations embedded
  in itself (`crates/gdaemon/src/main.rs:172-197`).
- `gobby start` applies first, from the CLI, under the daemon singleton:
  `_services_start(..., require_schema_owner=True)` (`src/gobby/cli/daemon_start.py:393`) calls
  `apply_hub_schema_contract` (`src/gobby/cli/_daemon_services.py:134`;
  `src/gobby/cli/datastores.py:46-62`). The direct path applies again through
  `init_local_storage` (`daemon_start.py:218`; `src/gobby/cli/utils_config.py:82-109`).
- Any CLI call with `apply_migrations=True` applies only when no daemon holds the singleton
  (`src/gobby/storage/hub/runtime.py:20-45`, `:67-115`).
- The backend first runs `gdaemon schema verify` (`runner.py:448-451`). Verify bails unless
  the hub head equals the binary's embedded head (`main.rs:276-297`). Only then does
  `GobbyRunner.create` reach `init_hub_database`, whose apply is a no-op in steady state
  (`runner.py:465`; `src/gobby/runner_init/storage.py:191`; `src/gobby/runner_init/helpers.py:107`, `:141-162`).
- So a new migration reaches the live hub only when `gobby start` runs after a promotion of a
  gdaemon that embeds it. In practice that is `gobby cutover`. A backend-only restart never
  applies one.

### Checkout schema inputs newer than the installed gdaemon

Setup: a chunk lands migration 488, `assets.rs` and the regenerated pin on 0.9.0. The
installed set still embeds v487.

1. `gobby restart` (default backend-only, `--full`, and the HTTP route): REFUSES.
   - `restart_start_refusal` runs before any stop, ahead of the backend-only branch
     (`src/gobby/cli/daemon_lifecycle.py:499-505`, branch at `:511`).
   - Order (`src/gobby/cli/daemon_preflight.py:37-110`): node refusal; worktree refusal
     (`:63-64`); installed set coherence (`:68-69`); `schema_apply_refusal` (`:70-77`);
     dirty bundled content (`:81-82`); SRT check (`:84-96`); hub URL; then
     `gdaemon schema plan` and `gdaemon schema preflight`.
   - `schema_apply_refusal` compares the installed gdaemon's embedded identity with the
     checkout pin and refuses on any difference: "installed gdaemon carries schema identity
     v487 (...), which is not this checkout's v488 (...)"
     (`src/gobby/storage/schema_divergence.py:251-265`). It also refuses a hub ahead of the
     checkout (`:267-274`). Unreadable inputs are not a refusal (`:242-250`).
   - The refusal fires before plan runs. In this path plan and preflight use the installed
     binary (`gdaemon=None`), so they never see the checkout's new migration.
   - The CLI prints "The running daemon was left alone" and exits 1 (`daemon_lifecycle.py:502-505`).
     The HTTP route returns 409 `start_refused` (`src/gobby/servers/routes/admin/_lifecycle.py:82-96`, `:262-277`).
     `gobby hub-backup` and `gobby pack` run the same proof (`src/gobby/cli/hub_backup/cli.py:232`;
     `src/gobby/cli/pack.py:368`).
   - The refusal lasts until a cutover promotes a set built from that commit. It hits every
     session's restart, not only the chunk's.
2. Plain `gobby start`: DOES NOT REFUSE and does not warn.
   - It checks only spawned caller, worktree source and installed set coherence
     (`daemon_start.py:358-368`). Coherence compares installed members with the installed stamp,
     never with the checkout pin (`schema_divergence.py:113-157`).
   - It applies the installed v487 chain (a no-op), launches gdaemon serve, and the backend
     verifies v487 against v487. Checkout Python then runs against the old schema. Any query on
     the new table or column fails at runtime.
   - The same holds for launchd KeepAlive recovery and supervisor crash respawns, which run no
     CLI preflight at all.
   - `gobby stop` has no start proof (`daemon_lifecycle.py:404-442`). So stop plus start
     bypasses the restart refusal. Cutover's own failure text tells the operator to run
     `gobby start` (`cutover.py:328`).
   - The only warning anywhere is `gobby status`, which prints the three heads and "DIVERGED"
     (`schema_divergence.py:41-72`; `daemon_lifecycle.py:800-803`).
3. `gobby cutover`: builds, proves, then applies.
   - Refuses a spawned caller and a node (`cutover.py:388-395`).
   - Refuses uncommitted or untracked schema inputs unless `--allow-dirty`
     (`cutover.py:147-161`, `:418-424`). Other dirt in the checkout is ignored.
   - Builds `cargo build --release --locked -p gobby-code -p gobby-daemon -p gobby-hooks`
     (plus gobby-client) from `--path`, default "." (`cutover.py:117-144`, `:123`).
   - Runs the same-version guard on all three (`cutover.py:285-293`; see section (c)).
   - Runs `restart_start_refusal` in candidate mode (`cutover.py:294-295`, `:428-436`):
     candidate identity must equal the checkout pin, fail closed
     (`daemon_preflight.py:113-131`); plan and preflight run with the candidate binary;
     an isolated test cluster must exist (`schema_contract.py:210-233`).
   - Proves catalog freshness with
     `cargo test --locked -p gobby-core --features postgres --test catalog_manifest_freshness`
     (`cutover.py:238-270`, `:296`).
   - Refuses on protected cron runs or unresolved handoffs unless `--wait` (`cutover.py:210-235`).
   - Stops the daemon, rechecks sources and the spawned-caller guard, promotes all three, writes
     the stamp, checks the restart target, and runs `gobby start`, which applies v488
     (`cutover.py:300-321`).
   - After the stop there is no rollback. Any failure leaves the daemon stopped with the text
     "After resolving this failure, run `gobby start`" (`cutover.py:322-332`). Promotion
     replaces binaries in place (`bin_set_coherence.py:134-146`).

### What `gdaemon schema plan` and `preflight` check

- Plan: embedded asset self-check, lineage, receipt resolution, and the pending list. It never
  verifies database identity, so a migration-owing hub passes (`runner_plan.rs:177-207`;
  `main.rs:199-245`).
- Preflight: plan, then refuses a pending baseline, any pending migration marked
  `-- gobby:non-transactional`, and any transaction-control statement
  (`runner_plan.rs:56-84`). It then runs every pending migration against the live hub inside a
  rolled-back transaction with `lock_timeout = 5s` and `statement_timeout = 60s`
  (`runner_plan.rs:85-92` and on). It holds the apply advisory lock (`runner_plan.rs:40-54`).

### Identity checks, and what each compares

| Check | Compares | Where | Used by |
|---|---|---|---|
| Installed stamp `~/.gobby/bin/.gdaemon-schema-identity.json` | each installed set member's embedded identity against the stamp | `schema_divergence.py:113-157` | start (`daemon_start.py:366`), restart (`daemon_preflight.py:68`) |
| Stamp writer | complete-set promotion writes it; `ensure_gdaemon` writes it from the checkout pin when they match | `bin_set_coherence.py:143-146`; `install_setup_gdaemon.py:141-178` | cutover, `gobby install` |
| Partial promotion | candidate identity against the stamp | `bin_set_coherence.py:231-254` | `gobby install` of gcode, gdaemon or ghook alone |
| Restart target | resolved gdaemon against the stamp, after promotion | `cutover.py:187-203` | cutover |
| Checkout pin vs installed gdaemon | `schema_expected_identity.json` against `gdaemon schema version --json` | `schema_divergence.py:251-265` | restart only |
| Checkout pin vs candidate | pin against the freshly built gdaemon | `daemon_preflight.py:113-131` | cutover |
| Live head vs checkout pin | `MAX(version)` of `schema_migrations` against pin head | `schema_divergence.py:203-214`, `:267-274` | restart |
| DB head vs binary head | hub head must equal embedded head, then catalog verify | `main.rs:276-297` | every backend start (`runner.py:448-451`) |
| Receipts vs embedded assets | filename and checksum per receipt | `runner_plan.rs:231-291` | plan, preflight, apply |
| Release identity `~/.gobby/bin/.<binary>-release-identity.json` | version plus source fingerprint | `src/gobby/install/bin_release_identity.py:148-250` | cutover, `gobby install` |
| Catalog freshness | migrated catalog against `catalog.manifest.json`, pinned to the candidate identity | `cutover.py:238-270` | cutover |

### Rollback consequence

- Once a cutover applies v488, a v487 gdaemon cannot serve. Backend verify bails on the head
  mismatch (`main.rs:286-291`), and plan refuses a newer hub (`runner_plan.rs:156-160`).
- There is no down-migration path. A previous binary generation is a valid rollback target only
  if no migration landed between the two generations.

## (b) What the live daemon reads from the checkout

- Python source. The editable install points at `src/`. A new backend process (any restart,
  crash respawn or start) imports the checkout as it is on disk.
  - Already-running processes are not fully isolated. Function-level imports are common:
    `gcode grep -l -E '^\s{8,}from gobby\.' src/gobby` lists 201 files, and
    `sync_registry.py:84` imports sync modules by name at call time. A module first imported
    after a landing loads the new file into the old process.
  - Every new CLI process uses new code at once. That includes the restart, start and cutover
    preflight itself (`daemon_preflight.py`, `cutover.py`, `daemon_start.py`).
- Python dependencies. Nothing on start or restart syncs the venv. The dependency report checks
  only git, node, docker and compose (`src/gobby/utils/dependency_requirements.py:245-315`).
  A landed new package fails at import on the next backend start, unless some `uv run` in the
  checkout synced the venv first. That is uv's default behavior, not Gobby code, and it changes
  packages under the running daemon.
- Bundled templates. The daemon cwd is the checkout, so it runs in dev mode
  (`src/gobby/utils/dev.py:69-83`). Every backend startup first refuses dirty bundled content
  (`runner.py:290-294`; `storage.py:47-59`), then syncs skills, prompts, agents, pipelines,
  rules, variables, detection manifests and MCP templates into the DB
  (`storage.py:62-77`; `src/gobby/sync_registry.py:22-39`, `:51-128`).
  - Sync errors are logged, not fatal. A failed rule delivery-disposition migration is fatal
    (`storage.py:83-88`).
  - Skill reference files are stored in DB rows with content and hash (`src/gobby/skills/sync.py:36-53`).
    `get_skill_file` serves the synced copy.
- Templates can go live with no restart:
  - `reload_cache` re-syncs rules, agents, pipelines, variables and detection manifests from
    the checkout (`src/gobby/mcp_proxy/tools/workflows/_import.py:13-81`). It needs
    `manage_workflow_definitions` or the operator terminal
    (`src/gobby/install/shared/workflows/rules/tool-hygiene/definition-mutation-authority.yaml:10-27`).
    The admin route `/workflows/reload` calls it (`_lifecycle.py:340-415`).
  - `gobby sync` and `gobby install` from the checkout sync everything
    (`src/gobby/cli/sync.py:212-243`; `src/gobby/cli/install_setup.py:149-154`). `gobby sync`
    from another checkout is refused unless `--force` (`sync.py:58-90`).
- Project workflow files. `.gobby/workflows/{rules,agents,pipelines,variables}/*.yaml` from each
  registered project's main checkout are imported at every startup, in dev and installed mode
  (`storage.py:79-81`; `src/gobby/workflows/imports.py:277-364`). The repo tracks
  `.gobby/workflows/rules/project-operating-policy.yaml`.
- Project config. `.gobby/project.json` is read per call from the project path
  (for example `src/gobby/config/tasks.py:163`; `src/gobby/config/validation_detection.py:284-302`).
- Web UI. `ui.mode` defaults to `auto` (`src/gobby/config/ui.py:87`). With `web/` source present
  it resolves to dev (`src/gobby/cli/ui_mode.py:31-51`), and the backend spawns `npm run dev`
  from the checkout's `web/` (`src/gobby/runner_lifecycle_subsystems.py:605-639`;
  `src/gobby/cli/utils_ui.py:304`). That server is live now. Landed `web/` edits reach browsers
  through Vite with no restart and no build. Production mode would serve gitignored `web/dist`
  from disk (`src/gobby/servers/_app_ui.py:289-313`).
  - `landing_policy.py:28-29` calls `web/` "ui_build". That is true only in production mode.
- Machine config, not in the checkout. `~/.gobby/bootstrap.yaml` and the DB config store.
  `gdaemon serve` reads `front_door.routes` once at its own start
  (`crates/gdaemon/src/front_door/routes.rs:6-7`; `crates/gdaemon/src/serve.rs:131`, `:174-179`, `:329`).
  A backend-only restart keeps the supervisor process and its routing table
  (`daemon_lifecycle.py:600`). A family absent from `front_door.routes` takes its
  `default_backend`, which is Proxy unless the family overrides it
  (`crates/gcore/src/route_family.rs:28-31`).

### What happens to an installed row when its template changes

All of this happens at the next sync: dev-mode startup, `reload_cache`, `gobby sync` or `gobby install`.

- Edited: the row is refreshed from the template. The user's enabled toggle is kept.
  Rules: `src/gobby/workflows/sync_rules.py:346-368`. Agents: `src/gobby/agents/sync.py:1-8`.
  Skills: owned skills are overwritten (`skills/sync.py:205-216`).
- Deleted: the row is soft-deleted as an orphan.
  - Rules prune only when the scan is authoritative, found files, and had no errors
    (`sync_rules.py:150-165`).
  - Agents are marked with `SYNC_ORPHAN_TAG` and come back if the template returns
    (`agents/sync.py:254-269`).
  - Skills retire only after the `gobby` router and requirement migration succeed. A missing
    router fails the whole skill sync (`skills/sync.py:330-370`).
  - Pipelines skip orphan cleanup when the scan was incomplete (`src/gobby/workflows/sync_pipelines.py:240-253`).
    Variables prune too (`src/gobby/workflows/sync_variables.py:135-141`).
- Moved: identity is the definition name, not the path. A move inside the synced tree keeps the
  row. A rule moved between the global and a project directory is re-scoped
  (`sync_rules.py:346-368`). A move into `deprecated/` or out of the tree is a delete. A rename
  is a new row plus an orphaned old row.

## (c) Which landed changes go live when

The landing code already classifies paths and reports the class to every session
(`landing_policy.py:19-73`; `land_commit.py:652`, `:699`, `:716`, `:345-346`). Corrected with
the facts above:

### Live at landing, before any restart

- `web/**` while the UI is in dev mode (verified live).
- `.gobby/project.json`.
- Any Python module the running backend has not imported yet, and all Python run by new CLI
  processes, including the restart, start and cutover code.
- Templates and project workflow files, as soon as an authorized session calls `reload_cache`
  or anyone runs `gobby sync` or `gobby install` from the checkout.
- Dependency changes in `pyproject.toml` or `uv.lock`, at the next `uv run` in the checkout.

### Live at the next restart, with no promotion

- `src/gobby/**` Python (`landing_policy.py:26-27`, `:56-57`).
- Bundled templates under `src/gobby/install/shared/` (dev-mode startup sync).
- `.gobby/workflows/*/*.yaml` (`landing_policy.py:38-51`).
- Raised floors in `src/gobby/install/version_pins.py:5-13` take effect in Python checks. For
  example, the daemon refuses gcode graph work below the floor
  (`src/gobby/code_index/gcode_versions.py:5-6`; `src/gobby/code_index/gcode_gateway.py:795-796`).

### Live only after a promotion of `~/.gobby/bin`

- gcode, gdaemon, ghook: by `gobby cutover`. Or by `gobby install` alone, when a raised floor
  makes `ensure_gdaemon` or `install_gcode` rebuild from the checkout and the schema identity is
  unchanged (`install_setup_gdaemon.py:141-170`; `src/gobby/cli/install_setup_gcode.py:138-184`).
- gdaemon front-door code also needs the supervisor process to restart (cutover, `--full`, or stop and start).
- gmcp, gclient, gterm: built from the checkout by every `gobby install`, promoted when the
  version was bumped (`src/gobby/install/version_pins.py:15`;
  `src/gobby/cli/install_setup_gmcp.py:85-131`, `:180-263`). Cutover also promotes gclient after
  the daemon is back (`cutover.py:338-347`). gmcp is what every new Claude session runs.
- The hourly GitHub freshness loop (on by default, `src/gobby/config/bin_freshness.py:11-14`)
  skips set members and checkout builds (`src/gobby/install/bin_freshness_updater.py:54-57`, `:96-99`).
- Migrations: only when `gobby start` runs after such a promotion.

### What can make the next restart refuse

- Any committed change to the pin, which every migration, baseline or `assets.rs` change
  forces. Every session's restart refuses until a cutover (`schema_divergence.py:251-265`).
- A hub head ahead of the checkout (`schema_divergence.py:267-274`).
- A mixed installed set (`schema_divergence.py:152-157`).
- Uncommitted or untracked files under the bundled skills, prompts, workflows, mcp or detection
  trees of the checkout (`daemon_preflight.py:81-82`; `src/gobby/sync/integrity.py:36-67`, `:88-135`).
  The backend refuses the same at startup in dev mode (`runner.py:293-294`).
- A failing installed-binary plan or preflight, or an unreadable hub URL (`daemon_preflight.py:98-110`).
- A CLI running from a linked worktree (`src/gobby/utils/dev.py:124-154`).
- A spawned agent as caller (`src/gobby/releases/activation.py:21-32`) or a managed HTTP
  principal (`_lifecycle.py:99-112`).
- Broken Python on the restart or startup path, or a rule template that breaks the
  delivery-disposition migration (`storage.py:83-88`).

### What can make the next cutover refuse

- Uncommitted schema inputs (`cutover.py:418-424`).
- Changed source for gcode, gdaemon or ghook under an unchanged version. The fingerprint covers
  each crate, its recursive path dependencies (gcore included), the root `Cargo.toml`,
  `rust-toolchain.toml`, `.cargo/config.toml` and the locked dependencies, but not test files
  (`bin_release_identity.py:96-145`, `:175-250`). Adding a member to the root `Cargo.toml`
  changes all six binaries' fingerprints.
- A `Cargo.lock` out of step with the manifests, since the build uses `--locked` (`cutover.py:123`).
- A hand-edited or stale pin. Restart refuses pin against installed, and cutover refuses
  candidate against pin (`daemon_preflight.py:124-128`). Only a pin generated from a real build
  passes both.
- A stale `catalog.manifest.json`, or no isolated test hub (`cutover.py:238-270`).
- A pending migration that is non-transactional, uses transaction control, waits over 5 s for a
  lock, or runs a statement over 60 s against the live hub (`runner_plan.rs:56-92`).
- Protected cron runs or pending handoffs without `--wait` (`cutover.py:210-235`).
- Everything in the restart proof except the installed-identity comparison.

## Implications for the map (constraints on every port chunk)

1. Schema inputs land only inside a checkpoint cutover window. A landed pin blocks every
   ordinary restart by every session until a cutover promotes that commit, and the cutover
   applies the migration to the live hub with no way back. Ordinary chunks carry no migration,
   no `baseline.sql` change, no `assets.rs` change and no pin change.
2. When a checkpoint does land schema: generate the pin from a real build. Regenerate the catalog
   manifest. Keep each migration transactional and free of transaction control, and quick under
   the 5 s lock and 60 s statement limits. Never edit a migration that any hub has applied.
   Allocate migration numbers serially across parallel worktrees. Test against
   `GOBBY_TEST_GDAEMON=checkout` after `cargo build -p gobby-daemon`
   (`tests/fixtures/gdaemon_binary.py:16-92`); otherwise pytest uses the installed v487 binary.
3. Every landed commit bumps the patch version of each of gcode, gdaemon, ghook, gclient, gmcp
   and gterm whose fingerprint it changed, with `Cargo.lock` updated in the same commit.
   Otherwise the next cutover by any session refuses. This includes root `Cargo.toml` edits such
   as adding a family crate.
4. Do not raise `MANAGED_BIN_VERSION_PINS` floors before the checkpoint that promotes those
   versions. A raised floor goes live at the next restart in Python checks, and it makes any
   `gobby install` rebuild and promote from 0.9.0 outside a checkpoint.
5. Rust that any session can promote must be dormant by default. A friction cutover builds and
   promotes all of 0.9.0 HEAD. New families keep the Proxy default backend. Runtime switches
   default to today's behavior. Flips live in `~/.gobby/bootstrap.yaml` and need a supervisor
   restart (cutover, `--full`, or stop and start), not a backend-only restart.
6. gmcp, gclient and gterm changes go live at any session's next `gobby install` once bumped.
   Treat a gmcp bump as a live change to every new Claude session. Bump these only at a checkpoint.
7. Python edits on 0.9.0 must be behaviorally inert at landing, not merely un-restarted. New CLI
   processes and lazily imported modules run them at once. Edits to the restart, start, cutover,
   preflight, schema-contract, bin-promotion or bundled-integrity modules change every session's
   next restart or cutover. Treat those modules as checkpoint-only.
8. No template edits, moves or deletes in `src/gobby/install/shared/` or `.gobby/workflows/`
   before retirement. They go live at the next restart or `reload_cache`, and a delete or move
   soft-deletes rows. Never leave template files dirty or untracked in the main checkout: that
   blocks every restart and every backend start.
9. No `web/` edits until the UI is meant to change. The live Vite server serves them at landing.
10. No new Python dependencies outside a checkpoint. Nothing on the start path syncs the venv.
11. Checkpoint cutovers run from an unspawned session in the main checkout, with `--path` set to
    the main checkout. Spawned agents are refused (`activation.py:21-32`). A worktree CLI is
    refused (`dev.py:134-154`). `--path` pointing at a worktree would promote that worktree's
    build against the main checkout's pin.
12. Never stop and start to get around a restart refusal. Plain start skips the pin check and runs
    new Python against the old schema. Recovery after a failed cutover is `gobby start` only
    once the cause is fixed.
13. Per-family binary rollback, which ruling 14 requires, is impossible across a migration today.
    The previous generation fails backend verify and plan on a newer hub. A checkpoint that adds
    a migration needs either no binary rollback past it, or a schema-compatibility mechanism that
    does not exist yet.
