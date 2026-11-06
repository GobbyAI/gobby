# F3: binary promotion and rollback (fact-finder, 2026-11-03)

Scope: how promotion works today, whether a rollback to an older binary generation passes each guard, what exists for retention and rollback, where a rollback verb hooks in, and what the hub backup covers. Code cites are repo-relative path:line. Lines marked "machine observation" come from listing `~/.gobby` on 2026-11-03, not from code.

## Summary

- Promotion replaces each binary through a new inode and keeps nothing. No code retains a previous generation of `~/.gobby/bin`, and no rollback verb exists.
- The guards do not refuse a downgrade as such. The same-version guard accepts any version change, including an older one (src/gobby/install/bin_release_identity.py:207-208).
- What refuses a rollback is the schema. If the hub head moved between the two generations, four separate checks refuse the older gdaemon. Rollback by binaries alone works only when the target generation has the same schema identity as the live hub and the checkout pin.
- Three things silently undo a rollback: `gobby install` (rebuilds any set member below the checkout pin), a friction session's `gobby cutover` of 0.9.0 HEAD, and the gcode gateway floor (which refuses an older gcode at runtime).
- Spawned agents cannot stop, start, restart or cut over the daemon. Checkpoint cutovers and rollbacks must run in an unspawned session on the main checkout.
- A minimal design reuses `promote_workspace_binary_set` for the restore. It needs a generation store, a hold with a burned-version check in `check_binary_release`, and a `gobby rollback` verb. That is about 400 to 500 production Python lines. The stamped-set contract does not change.

## (a) promote_workspace_binary_set, step by step

Source: src/gobby/install/bin_set_coherence.py:88-153.

1. Validate the candidates: the set is not empty, every member is known, members are ordered gcode, gdaemon, ghook, and every file exists (bin_set_coherence.py:97, 204-216; SET_MEMBERS at src/gobby/utils/native_bin.py:11).
2. Probe each candidate's embedded schema identity. gdaemon runs `schema version --json`; gcode and ghook run `schema-identity --json` (bin_set_coherence.py:98-100, 53-85).
3. A complete set must agree with itself (bin_set_coherence.py:106-107, 219-228). A partial set must equal the installed identity, which is read from the stamp, or from a gdaemon probe when no stamp exists. An empty bin dir passes (bin_set_coherence.py:108-109, 231-254, 257-270). Metadata requires the complete set (bin_set_coherence.py:102-105).
4. Run the release preflight once without locks (bin_set_coherence.py:111-117). That is `check_binary_release` per member (src/gobby/install/bin_freshness_promotion.py:125-152).
5. Take one native-bin lock per member and fail if one is held (bin_set_coherence.py:121-127). Then run the release preflight again under the locks, with the source identities frozen (bin_set_coherence.py:128-133).
6. For each member, stage and promote (bin_set_coherence.py:134-142, through bin_freshness_promotion.py:57-87 and 155-176):
   - Staging: a `.<name>-staging-*` directory inside bin_dir, so the rename stays on one filesystem (bin_freshness_promotion.py:163). The candidate is copied with fsync and mode 0755 (bin_freshness_promotion.py:210-215).
   - Ad-hoc signing: `codesign -f -s -` runs on the staged copy, on macOS only (bin_set_coherence.py:138, 156-178).
   - New-inode replacement: `os.replace(staged, bin_dir/name)` (bin_freshness_promotion.py:172). The old inode loses its name, and the staging directory is removed (bin_freshness_promotion.py:176). Nothing is kept.
   - Release identity: the receipt `.<name>-release-identity.json` is written after the replace, atomically. It holds the version, the source identity and the artifact sha256 (bin_freshness_promotion.py:173-174; bin_release_identity.py:148-149, 253-266). The source identity is a hash of the crate closure's git-visible files plus locked dependencies (bin_release_identity.py:96-145). It is not a commit.
7. Complete set only: write the version stamps `.<name>-version` and the sidecars `.<name>-install.json` (bin_set_coherence.py:144-145, 181-201). Then write the schema identity stamp `~/.gobby/bin/.gdaemon-schema-identity.json` last (bin_set_coherence.py:146, 273-274; name at native_bin.py:12).
8. On any failure, raise with the promoted and unpromoted member lists (bin_set_coherence.py:147-153). Already promoted members are not undone. Because the stamp is written last, a mid-set failure leaves a mixed set, and the next start refuses it.

The same-version changed-source guard is `check_binary_release` (bin_release_identity.py:175-250):
- The candidate must report a version (184-186).
- No installed binary means accept (199-200).
- An installed binary that cannot print `--version` is refused: "remove the unverified installed binary, then rebuild and install" (201-206). The probe is `<bin> --version` with a 5 s timeout (src/gobby/install/version_probe.py:25-73).
- A different installed version means accept (207-208).
- The same version is accepted only if the installed receipt has the same source identity (209-221), or, for legacy installs, the same artifact bytes (222-238). Otherwise it is refused: "bump its patch version before promotion" (239-249).
- The guard dates from #24347 on 2026-10-13 (git log of bin_release_identity.py).

The start-time "mixed installed binary set" refusal:
- `collect_installed_binary_set` reads the stamp, probes every installed member and compares each identity to the stamp (src/gobby/storage/schema_divergence.py:113-149, compare at 143). An unreadable stamp or member counts as mixed (94-96).
- `describe` returns "mixed installed binary set: ..." (98-110). `binary_set_apply_refusal` adds "rebuild and install all three together" (152-157).
- Callers: `gobby start` (src/gobby/cli/daemon_start.py:366-368) and `restart_start_refusal` in installed-set mode (src/gobby/cli/daemon_preflight.py:66-69). The latter is used by restart and hub-backup.

## (b) Would a rollback to an older generation be refused?

Setup: the previous generation P (versions vP, schema identity SP) is restored over the current generation N (vN, SN). The main checkout stays at 0.9.0 HEAD, whose pins equal the HEAD crate versions (tests/install/test_version_pins.py:27-29) and whose pin file is `src/gobby/storage/schema_expected_identity.json`.

1. Same-version changed-source guard: not refused.
   - A bumped member has vP different from vN, so the guard accepts at bin_release_identity.py:207-208. The guard has no monotonic check, and no test covers a downgrade (tests/install/test_binary_release_versions.py has none).
   - An unbumped member has the same version and source, so it is accepted when the restore passes P's recorded source identity (bin_release_identity.py:188-197, 209-221).
   - To pass: restore through `promote_workspace_binary_set` with `source_identities` taken from P's retained receipts. Do not let it compute the identity from the HEAD tree (bin_release_identity.py:190-195), or the new receipt would credit P's binary with HEAD's source.
   - Hole: after a rollback the installed version is vP. A fix-forward build that reuses vN with different source is then accepted at 207-208. A rollback must record vN as burned so the guard refuses that reuse.
   - Hazard: if N's binary cannot print `--version`, every promotion over it is refused (201-206), including the restore. The restore must first move that binary into its generation directory, which is the guard's own remedy text.
2. Version pins and UNPUBLISHED_MANAGED_BINS: not consulted by promotion. They undo or degrade a rollback afterwards.
   - The pins live at src/gobby/install/version_pins.py:5-13, and the unpublished set at version_pins.py:15. The pins must equal the crate versions (tests/install/test_version_pins.py:27-29). So the HEAD floor is always vN or higher, and a restored vP is below it.
   - `gobby install` rebuilds any set member below the pin from the HEAD workspace, through a partial promotion: gdaemon at src/gobby/cli/install_setup_gdaemon.py:150-163 (and 109), gcode at src/gobby/cli/install_setup_gcode.py:157-166 (and 131), ghook at src/gobby/cli/install_setup_ghook.py:228-238 (and 186). gdaemon also reinstalls when its identity differs from the checkout pin (install_setup_gdaemon.py:150-155).
   - Unpublished gclient, gterm and gmcp are built from the checkout on every `gobby install` (install_setup_gclient.py:290-291; AGENTS.md:268-270).
   - Silent case: after a schema-neutral rollback, the partial promotion matches the stamp (bin_set_coherence.py:231-254) and the guard accepts the newer version, so `gobby install` quietly re-promotes the broken HEAD build. `promote_workspace_binary_set` has no activation check, so a spawned agent's routine install can do this. The next full restart then runs it.
   - Loud case: after a cross-identity rollback, the same install is refused by `_require_installed_agreement`.
   - The freshness updater skips set members (src/gobby/install/bin_freshness_updater.py:56, 96-99). For unpublished components it only records `floor_violated` (bin_freshness_updater.py:126-150, 255-256).
   - To pass: a rollback hold that the shared guard enforces (see Implications). Changing floors to capability floors would break the AGENTS.md rule "keep managed version pins and the gcode maintenance version floor in step". That is a contract change for Josh, and this report does not recommend it.
3. gcode maintenance version floor: not a promotion or start refusal. It is a runtime refusal.
   - The floors equal the pin (src/gobby/code_index/gcode_versions.py:5-6).
   - Every gateway call refuses gcode below the floor: "gcode >= X required; found Y" (src/gobby/code_index/gcode_gateway.py:775-800, check at 795-796). Maintenance prune also refuses (gcode_gateway.py:699-705).
   - So a full-set rollback that lowers gcode breaks the Python daemon's code-index features until fix-forward.
   - By contrast, ghook below its minimum only marks health "degraded" (src/gobby/hooks/runtime_compat.py:17, 170-184; src/gobby/servers/routes/admin/_health.py:378). Health still answers 200, which is all start waits for (src/gobby/cli/daemon_lifecycle.py:113-121).
   - To pass: roll back only the member that broke. Under D36 that is usually gdaemon alone. A partial promotion allows this when the schema identity is unchanged (bin_set_coherence.py:231-254). Restore gcode only when gcode itself broke.
4. Release-identity checks: the receipts are read only during promotion (bin_release_identity.py:209; no other reader in src or crates). Nothing checks them at start.
   - To pass: restore through the normal promotion. It writes a receipt for the restored binary (bin_freshness_promotion.py:173-174). Keep N's receipt in its generation as the burned record.
5. Start-time identity checks: these refuse any rollback across a schema change.
   - Mixed set (schema_divergence.py:113-157): passes if all three members and P's stamp are restored together. A partial restore passes only if SP equals SN.
   - Checkout pin: plain `gobby restart` runs `schema_apply_refusal` with no expected identity (daemon_preflight.py:70-76). It refuses an installed gdaemon whose identity is not the checkout's pin (schema_divergence.py:250-266), and a hub newer than the checkout (schema_divergence.py:273-274). `gobby start` checks only the mixed set (daemon_start.py:366). So start may launch a generation that restart refuses. This is the first consequence of a cross-identity rollback.
   - Runner start: the runner runs `gdaemon schema verify` (src/gobby/runner.py:449). That requires the database version to equal the embedded version exactly (crates/gdaemon/src/main.rs:276-297, check at 287).
   - Hub init: `init_local_storage` applies migrations with the installed gdaemon (daemon_start.py:218; src/gobby/storage/hub/postgres.py:407). The schema runner refuses "database schema vX is newer than this runner" (crates/gcore/src/schema/runner_plan.rs:153-160; apply at crates/gcore/src/schema/runner.rs:109).
   - Migrations are forward-only. The #23522 rehearsal recorded "Migrations 460–466 stayed forward-only" (docs/evidence/gdaemon-key-cutover-rollback-rehearsal.md:59-63). `gdaemon schema preflight` is a transactional dry run of pending migrations, not a down migration (runner_plan.rs:40-54, 56-90).
   - To pass: the target generation must have the hub's current schema identity. A rollback across a migration needs a hub restore plus a pin that matches. On the shared 0.9.0 checkout that means a forward revert. It is a disaster path, not a rollback.
6. Cutover preflight: `gobby cutover` always builds from a workspace (src/gobby/cli/cutover.py:117-144, 287). It cannot take retained binaries.
   - Zero-code fallback: run `gobby cutover --path <worktree at the previous checkpoint commit>` from the main checkout. It reads that root's pin (cutover.py:425). The downgrade passes the guard (cutover.py:289-291). The candidate identity is checked against that pin (daemon_preflight.py:113-131), then `schema plan` and `preflight` run with the candidate (daemon_preflight.py:104-105), then the catalog test (cutover.py:238-270).
   - Limits of that fallback: it works only if the hub head has not moved. It is slow (a release build plus a cargo test). It makes different bytes from the ones first promoted. It still needs the hold. It is the recipe when no retained generation exists. It is not Josh's "backup binary".
   - For a dedicated rollback verb, legitimate bypasses are: no build, no catalog test, and no unchanged-sources check. The retained bytes were proven when they were first promoted, and re-running the catalog test from HEAD would test HEAD's assets, not P's. The verb keeps every other step.

Who can run any of this:
- `agent_activation_refusal` refuses stop, start, restart and cutover whenever `GOBBY_AGENT_RUN_ID` is set (src/gobby/releases/activation.py:21-32). Callers: cutover.py:391 and 309, daemon_lifecycle.py:241 and 499, daemon_start.py:358.
- `get_terminal_env_vars` sets `GOBBY_AGENT_RUN_ID` for every spawned agent terminal (src/gobby/agents/constants.py:257).
- Restart and cutover also refuse when the CLI's Python is a linked worktree (src/gobby/utils/dev.py:134-153, through daemon_preflight.py:62-65).
- So checkpoint cutovers and rollbacks must run from an unspawned, operator-opened session on the main checkout. They cannot run from a spawn_agent child or from the chunk's 0.9.0-rust worktree. This changes how rulings 11 and 15 can be applied: "a default agent (like you)" works only for an unspawned session. This workflow's subagent has the variable unset.

## (c) Is any previous generation kept today? Is there a rollback verb?

- No code keeps one. Promotion drops the old inode (bin_freshness_promotion.py:172, 176), and `promote_workspace_binary_set` has no backup step (bin_set_coherence.py:88-153).
- The only retention in code is gterm's host image pins:
  - Content-addressed hard links (copy fallback) under `~/.gobby/gterm-images/`, so a live host and its one-shot fallback never exec replaced bytes (crates/gterminal/src/host/image.rs:1-3, 13, 54-75).
  - Pruned to the running image (image.rs:139-149). Documented in src/gobby/install/shared/skills/gobby/references/admin/daemon.md:40-50.
  - It is a live-upgrade mechanism for gterm only. It is not an operator rollback, and it never re-signs.
- Manual leftovers (machine observation, 2026-11-03). No code writes these (gcode grep for `.bak-v`, `backups/gclient`, `backups/gterm`, `backups/native` finds only a test fixture):
  - `~/.gobby/bin/.gcode.bak-v435`, which is gcode 1.7.0 (docs/evidence/community-labels-2026-09/live-smoke.md:15-17), and `~/.gobby/bin/.gdaemon.bak-v435`.
  - `~/.gobby/backups/gclient/gclient-0.1.14` through `0.1.16`, `~/.gobby/backups/gterm/gterm-0.1.2-*`, and `~/.gobby/backups/native/gclient-before-22609-20260919`.
  - The installed stamp is at schema v487 and gdaemon 0.4.128 (cutover install at 2026-11-03T22:19:56Z). So the v435 copies cannot pass the exact-version verify (main.rs:287).
  - tests/fixtures/test_e2e_native_bin_env.py:131 only copies such dotfiles into test homes.
- gclient caches: `~/.gobby/client` holds per-project client state, prefs and session files, with no binaries. `~/.gobby/cache/cargo-target*` holds the last build output, overwritten by each build (machine observation).
- `~/.gobby/cutover-restore-manifest-23523.json` (2026-10-11) lists terminal sessions to resume after a cutover. It holds no binaries (machine observation).
- No rollback verb exists. The only "rollback" in src/gobby/cli is datastore compose-state restore (src/gobby/cli/datastores.py:254-262).
- The one recipe on record is the #23522 rehearsal (docs/evidence/gdaemon-key-cutover-rollback-rehearsal.md:1-23, 59-63):
  - It was a reviewed inverse commit that kept the schema bytes, pin, Cargo.lock and version_pins.py.
  - It was built and promoted into an isolated scratch bin dir. It was rehearsal-only ("must not land on `0.5.0`").
  - It predates the 2026-10-13 guard. Today, an inverse that keeps bumped versions while changing source is refused at bin_release_identity.py:239-249.

## (d) cutover and restart step by step, and where retention and rollback hook in

`gobby cutover` (cutover.py:366-449, run_cutover 273-347):
1. Activation refusal, then node refusal (391-396). The workspace root needs Cargo.toml, crates/ and the pin file (60-66).
2. Refuse dirty schema inputs unless `--allow-dirty` (419-423, 147-161). Read the workspace pin (425).
3. Compute source identities (286). Build `cargo build --release --locked` for gcode, gdaemon, ghook and gclient. If the shared run fails, rebuild without gclient (287, 117-144).
4. Release preflight (289-291).
5. Prove the start half with the candidate. Node and worktree refusals, candidate identity equals the pin, dirty bundled content, SRT, readable hub URL, `gdaemon schema plan` and `preflight`, test cluster (294-295; daemon_preflight.py:37-110, 113-131). Then the stop admissions: protected cron runs and unresolved handoffs (cutover.py:210-235).
6. Catalog freshness cargo test (296, 238-270). Sources unchanged (297).
7. Stop through `_do_stop`, restart intent (300, 401-408).
8. Sources unchanged again, and activation checked again (306-309).
9. `promote_workspace_binary_set(..., metadata=workspace-cutover)` (311-319).
10. `_verify_restart_target`: the resolved gdaemon matches the stamp (321, 187-203).
11. Start (322, 410-416).
12. Promote gclient after the daemon is back, outside the stamp (338, 350-357).

`gobby restart` (daemon_lifecycle.py:445-538):
1. Activation refusal or `restart_start_refusal` in installed-set mode (499-505).
2. Without `--full`, `--docker` or an expected identity, it posts `/api/admin/restart`. Only the Python backend is replaced: "native supervisor retains its listeners and lease" (512-519, 541-617, 574, 600). A swapped gdaemon binary is not exec'd.
3. With `--full`, or when the backend is unhealthy: `_do_stop`, then `start` (524-538). `start` launches `~/.gobby/bin/gdaemon serve` with `GOBBY_BACKEND_COMMAND` set to the checkout's `python -m gobby.runner` (daemon_start.py:240-259).

Hook points:
- Retention goes inside `promote_workspace_binary_set`, after the locks and the second preflight and before the first stage (bin_set_coherence.py:128-134). Every set promotion then retains the outgoing set: cutover, and install's partial promotions. gclient gets the same snapshot inside its own promotion (src/gobby/cli/install_setup_gclient.py:168-189) and cutover.py:350-357. Do the same for gmcp if CP-MCP ships it.
- Retained files must be copies, not hard links or symlinks:
  - `_install_gdaemon` re-signs the installed path in place after promotion (install_setup_gdaemon.py:134-138, 115-131).
  - The gcode and ghook installers chmod the installed path (install_setup_gcode.py:~174; install_setup_ghook.py:243).
  - `_write_sidecars` rewrites `.gdaemon-version` and the stamp in place with `write_text` (install_setup_gdaemon.py:173-178).
  - A symlinked binary reads as dev mode (src/gobby/install/bin_freshness_inspector.py:63).
  - A copy keeps the embedded ad-hoc signature, and the restore re-signs its staged copy anyway.
- The hold and burned-version check go in `check_binary_release` (bin_release_identity.py:175). That is the single choke point: `_stage_and_promote` calls it directly when no identity is passed (bin_freshness_promotion.py:168-171), so it also covers component installs.
- The rollback verb goes in a new `src/gobby/cli/rollback.py`, reusing cutover's helpers:
  1. Activation and node refusals.
  2. Resolve the generation and the members.
  3. `restart_start_refusal(runtime, <retained gdaemon>, expected_identity=<retained stamp>)`. Its `schema plan` refuses a hub newer than the target (runner_plan.rs:156-160). Then `_stop_admission_refusal`.
  4. Stop. Move an unverifiable installed binary aside.
  5. `promote_workspace_binary_set(<retained paths>, source_identities=<retained receipts>, metadata=rollback)`.
  6. Write the hold. Run `_verify_restart_target`. Start. Restore gclient after the start, if asked.
  - A complete-set restore never reads the installed stamp (bin_set_coherence.py:106-107). So it also recovers today's dead end of a mixed or half-promoted set.

Size estimate (Python production lines):

| File | Lines |
|---|---|
| New generation module (snapshot, list, resolve, prune, hold, burned history) | 180-220 |
| `cli/rollback.py` | 150-200 |
| bin_set_coherence.py, bin_release_identity.py, bin_freshness_promotion.py | 40-60 |
| gclient and gmcp snapshot calls, CLI registration | 15-25 |
| **Total** | **about 400-500** |

Tests add about 350 to 500 lines. Docs need AGENTS.md Architecture Facts, crates/AGENTS.md, admin-operations.md, and the daemon.md skill reference (with a version bump under rule 17).

Contract effects:
- The stamped-set contract does not change. The members, the stamp name, stamp-last writing, the partial-set identity rule and the "mixed installed binary set" semantics all stay. The restore is itself a call to `promote_workspace_binary_set`, so AGENTS.md "that function is the contract" stays true.
- gclient's separate promotion does not change in shape. It stays outside the stamp and is promoted after the daemon is up. It only gains a retained copy and a separate restore step.
- V1A (the Rust verbs chunk, which owns `CLI gobby cutover` and the install engine in the earlier map) must port the generation store, the hold and `gobby rollback`. Add a `CLI gobby rollback` row to its selector (parity-ledger rows at docs/contracts/parity-ledger.md:82, 110, 214).

## (e) Hub database backup as the data rollback

- `gobby hub-backup` (src/gobby/cli/hub_backup/cli.py:184-269, 432-495) proves the start half (232), stops the daemon (240), holds a maintenance claim, archives, restore-verifies each store, publishes atomically and restarts. It covers:
  - PostgreSQL as `pg_dump` plus `pg_dumpall --globals-only --no-role-passwords` (src/gobby/cli/hub_backup/_stores.py:389-418)
  - a Qdrant snapshot, a FalkorDB RDB, the Docker volume tars, files_home, the rule-allow audit logs and the machine identity (cli.py:123-126, 455-485)
- Requirements: managed Docker Postgres, the three containers running, and 5 GiB free (cli.py:106-110, 119, 356-373).
- `gobby hub-backup restore BACKUP --database-url DSN [--clean] [--yes]` needs the daemon stopped (cli.py:297). It restores files_home, the Postgres globals and data, and reconciles principals (cli.py:272-348). It does not restore Qdrant, FalkorDB or the volumes (docs/guides/admin-operations.md:308-313; docs/guides/cli-commands.md:1101-1102).
- `gobby postgres backup` and `restore` cover Postgres only, with the daemon stopped (src/gobby/cli/postgres.py:137-150, 148, 177; src/gobby/cli/postgres_backup.py:57-153).
- Existing backups (machine observation): `~/.gobby/backups/hub/20260915T171640Z` and `20261022T121135Z`, plus a stale `.20261023T001220Z.*.tmp`.
- Fit as a data rollback:
  - A hub restore discards every write since the backup, for every project on the hub. So it is the disaster path for a rollback across a migration, not the routine rollback.
  - The hub-backup verb is Python and "proxied" in the ledger (parity-ledger.md:107-108). Plan #20997 (`.gobby/plans/gobby-backup.md:1-30`) designs a Rust `gbackup` that also restores Qdrant and FalkorDB. No `crates/gbackup` exists yet.
  - After Python is deleted, the late checkpoints need that Rust backup, or they lose their data rollback.

## Implications

1. Generation store. Before each promotion, copy the outgoing files into `~/.gobby/bin/generations/<seq>-<utc>/` with a manifest: binaries, stamp, receipts, version stamps, sidecars and source-sha files. Copy them; do not link them. Keep 3 generations, and never prune the installed one or a held one.
2. Restore. Restore by calling `promote_workspace_binary_set` with the retained paths and the retained receipts' source identities. This passes the guard as it stands, because downgrades are accepted at bin_release_identity.py:207-208. Move an unverifiable broken binary aside first.
3. Hold and burned versions. In `check_binary_release`, refuse a candidate whose version equals a burned version. Allow older versions for rollback and newer versions for fix-forward. Clear the hold explicitly on the fix-forward cutover. This stops `gobby install` and friction cutovers from re-promoting the broken HEAD, and it closes the version-reuse hole.
4. Rollback scope. Default to the broken member, which is gdaemon under D36. Roll back the full set only when needed, because gcode below the HEAD floor breaks the gcode gateway (gcode_gateway.py:795-796).
5. Schema-neutral checkpoints (a map rule, no code). Migrations reach the hub first in a cutover with every switch off. The flip cutover then carries no schema change, so its rollback target has the same identity. A rollback across a migration is refused by four checks (schema_divergence.py:260-266 and 273-274; main.rs:287; runner_plan.rs:156-160). It is a disaster path: a pre-cutover `gobby hub-backup`, then a restore, then a forward revert on 0.9.0. While a hold is active, no migration may land on 0.9.0, or plain restarts will refuse.
6. Who runs it. Checkpoint cutovers and rollbacks run in an unspawned, operator-opened session from the main checkout (activation.py:21-32; constants.py:257; dev.py:134-153). They never run from a spawn_agent child or a 0.9.0-rust worktree. This needs to be stated against rulings 11 and 15.
7. A swapped gdaemon needs a full stop and start. A plain `gobby restart` keeps the native supervisor running (daemon_lifecycle.py:574, 600).
8. Placement. Nothing in the guards needs code on 0.5.0. The first promotion through the new code retains whatever is installed then, which is the last pre-port generation. So it can be the first 0.9.0 chunk, at the root of the DAG and blocking every chunk that lands promotable Rust code, as long as it is rehearsed before CP-RUNNER's cutover. A start-gate item on 0.5.0 is needed only if the cut itself, or 0.5.0 friction cutovers, must be rollback-capable. Either route is a policy call for Josh: it is Python on 0.9.0 (D4, ruling 6), and it is a new task (ruling 10).
9. V1A must port the generation store, the hold and `gobby rollback`, and gains the new ledger row. The stamped-set contract is unchanged. gclient is retained and restored separately after the daemon is up, as at cutover.py:338.
