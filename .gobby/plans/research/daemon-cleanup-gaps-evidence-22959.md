# Current-daemon cleanup gaps: evidence report

Task: #22959 (Investigate current-daemon cleanup gaps across project removal,
worktrees, caches and telemetry), under #22949 (Lane 7 - Planning/research).
Author: Plan Writer 3 (gobby#15468). Measured 2026-10-05 between 22:35 and
23:30 UTC. Source binding is HEAD `d830d904737191dacbdd7d153d22992d1ee5f154`.
The revision after the enhancer pass re-checked identity, statistics, backups,
worktrees, configs and creator transcripts between 01:00 and 01:20 UTC on
2026-10-06, with code citations bound to HEAD
`fec30c7e2a96be75f59dbc696a35ff9343062d2f`.

Method: every measurement was read-only. Database reads used the diagnostic
script in Appendix A, which opens a `READ ONLY` transaction with a 120 s
statement timeout and never applies migrations. Filesystem reads used `du`,
`stat`, `find`, `readlink`, `lsof`, `ps`, `zgrep` and `grep`. Nothing was
deleted, vacuumed, reindexed or migrated.

Identity: this was an operator-identity run.
`GOBBY_MANAGED_EXECUTION_BOOTSTRAP` was unset, so gobby's CLI runtime
resolved the database the way every `gobby` command does, by loading the
bootstrap config inside the Python process
(`src/gobby/storage/hub/runtime.py:86-95`). No connection detail was printed
or logged, and no agent opened `~/.gobby/bootstrap.yaml` or
`~/.gobby/local_cli_token`. The connected role is `gobby` with
`rolsuper = true` and `rolbypassrls = true`. Among the measured tables, row
security is enabled only on `projects`, and its `gobby_migration_owner_access`
policy covers this role. Every count below is therefore hub-wide.

Starting evidence: the Researcher #14550 report. It was never committed and
now sits at `~/.gobby/research-archive/code-index-storage-gaps-2026-09-26.md`
(sha256 `4e7044a6e2c70e7236533a3c6b145543f9ea5c8eaf2aeef0b455343cd47688f1`).
A byte-identical copy is committed beside this report as
`.gobby/plans/research/code-index-storage-gaps-2026-09-26.md`. Its 2026-09-26
sizes are historical. Every figure below was re-measured.

Code citations come from `gcode evidence` range reads. Each one gives the
path, the line range and the `excerpt_hash`.

## Verdicts

| Miss | Verdict | Fix class |
|---|---|---|
| (a) Project removal leaves state | Confirmed. The purge has no filesystem step, so it never removes worktree or clone dirs, Cargo targets or backups. Three tables with no FK hold orphan rows today. | A small guard for registered worktrees and clones. The FK delete action per table is a Josh decision. Backups and main-checkout Cargo targets stay an explicit gap. |
| (b) Orphan worktrees | Confirmed as relics: 1.0 GB plus empty dirs. None has a registry row. Creator attribution is an evidence gap. | Josh-approved operator removal. The in-transaction guard in (a) prevents purge from cascading away registered worktree rows. A racing creator's directory is removed only when its compensation succeeds; clones need the caller-cleanup trace first. |
| (c) Old 21 GB `cargo-target` | Confirmed stale. Transcripts show agent sessions setting `CARGO_TARGET_DIR` to all five `wt-*` dirs. Gobby's own legacy entries are empty. | Josh-approved operator removal. No code change. |
| (d) `unmodeled_observation_events` size | The prune keeps up. Size comes from volume plus index churn: 84% of rows are one unmodeled Codex tool. | A product decision, then a data-only modeling fix. |
| (e) `token_events` retention | The policy exists (180 days, #19655) and was never implemented. No row is eligible until 2027-01-27. | Schema index plus a delete contract that keeps session lifetime totals intact, landing before 2027-01-27. |

## (a) Project removal

### Evidence

`ProjectPurgeService._purge_project` runs these steps in order:

1. soft-delete;
2. refuse while terminals are active;
3. drain and delete cron jobs;
4. take the exclusive fence;
5. invalidate code;
6. clear vectors and the graph;
7. delete hub rows.

None of these steps removes worktree or clone directories, Cargo targets or
`~/.gobby/backups/<project-uuid>`. Any on-disk effect of the gcode invalidate
child was not traced.

The scheduler is the `gobby:project-purge` cron job. Its handler is
`projects:purge-expired`, registered by `register_project_purge_cron` through
`src/gobby/runner_init/project_purge.py:76`. It runs every 24 h. Each run
selects every soft-deleted, non-system project whose `deleted_at` is at or
before now minus 24 h (inclusive cutoff), with no row limit, ordered by
`deleted_at, id`. It purges all of them, at most 4 at a time.
`PROJECT_PURGE_ID_LIMIT = 10` caps only the ID arrays in the returned result;
the `purged_count` and `failed_count` fields carry the full counts:

- `src/gobby/projects/purge.py:19-24`
  `excerpt_hash=d101e5446fbd05bc312e3a5fcd8d601e372b22fb351e42bd9b4ba2f47c1b4f29`
- `src/gobby/storage/projects.py:514-522` (`list_purge_candidates`)
  `excerpt_hash=3ab30fed35de8e2241bbcb2b25e203c95ab3ea03eaf24bd9e991e53ee7a7ba8f`
- `src/gobby/projects/purge.py:363-380` (handler, semaphore of
  `PROJECT_PURGE_CONCURRENCY = 4`)
  `excerpt_hash=2573eb37f88dac287f2ba0a9c1d241e690093c737651d31d542ad483a2dd906b`

Failure visibility: the handler catches each project's exception and records
only `failed` with the project ID. The exception and the outcome reason are
discarded. The run returns the failed IDs (first 10), the failed count and an
overall `failed` status. The handler never retries a project itself. A failed
project stays soft-deleted and becomes a candidate again at the next daily
run, which is the only retry.

- `src/gobby/projects/purge.py:256-263`
  `excerpt_hash=3b9e9ceca490b52fee13d3efe547e03f79477a8fb72ef4c33b2467fe717ebec4`
- `src/gobby/projects/purge.py:352-357`
  `excerpt_hash=d6e18fb8c9a4e7d75f16eefab740ae8afdf53331a36d388074c94273d2cbe621`
  `_delete_hub_rows` names only `tasks`, `plans`, `sessions` and `projects`.
  Everything else relies on FK actions.

FK actions on every `project_id` column, read from `pg_constraint`:

- `CASCADE` (26 tables), including `worktrees`, `clones`,
  `project_checkouts` and `project_lifecycle_events`. The registry rows for
  worktrees and clones therefore vanish with the project, while their
  directories stay on disk.
- No FK (16 tables): every `code_*` table (owned by gcode and invalidated
  separately), `metrics_events`, `metrics_events_archive`,
  `tool_schema_hashes`, `memory_dream_truth_state` and `token_events`.

Live leftovers from removed projects:

- `metrics_events`: 50 rows with `project_id` `5baa0e37-e8a6-4c0a-b226-7510ca894595`.
- `metrics_events_archive`: 1 row with `project_id` `3d14159f-a60c-489e-b060-d41b48249ba3`.
- `tool_schema_hashes`: 2 rows with `project_id` `3d14159f-a60c-489e-b060-d41b48249ba3`.
- `token_events`, `sessions`, `tasks` and the `CASCADE` tables have zero orphans.
  For `token_events`, both the `project_id` check and the `session_id` check
  against `sessions` return 0.
- No project row is currently soft-deleted (`soft_deleted` query, Appendix B).
- `~/.gobby/backups` holds per-project dirs only for live projects:
  `1d7a5bc4…` (game-goblins, 1.0 MB) and `d45545c5…` (gobby, 265 MB). The
  removed projects `5baa0e37` and `3d14159f` have no backup dir today. The
  other entries (`hub` 8.7 GB, `gclient`, `gterm`, `native`,
  `22943-cutover-prep-14639`) are not keyed by project. Who writes them, and
  under what retention, was not traced.

Soft delete also releases the checkout routing rows. `soft_delete` calls
`unregister_project` in the same transaction, so `project_checkouts` no
longer names the project's main checkout by the time purge runs:

- `src/gobby/storage/projects.py:646`
  `excerpt_hash=3857441011dafa42771fde990bf2cd71a74c07ed8a26e6ddf98838e4fd2e3886`

### Root cause

The purge was built around hub rows and FK cascade. Its contract has no
filesystem step and no list of tables without an FK. Cascading `worktrees` and
`clones` rows also erases the only registry evidence that those directories
exist.

Evidence gap: no lifecycle record survives purge, because
`project_lifecycle_events` cascades. Whether projects `5baa0e37` and `3d14159f`
were removed by today's purge or by an older delete path cannot be proven.

### Minimal current-daemon fix

1. **Purge guard, in two places.** An early count alone does not prevent
   orphans. Checkout creation clones or adds the worktree on disk first and
   registers the row afterwards, outside the purge fence, so a row can commit
   after an early count and before the cascade. The guard therefore has two
   parts:
   - **Authoritative check, atomic with the cascade.** The final purge
     transaction already locks the project row `FOR UPDATE` and re-checks
     terminals before deleting anything:
     `src/gobby/projects/purge.py:334-343`
     `excerpt_hash=637f91d9c891c36caead1ea3b80c9edb5279dc5f53ecdcfffedd90ae39b0d558`.
     Add a count of `worktrees` and `clones` rows right after that lock,
     raising `ProjectPurgeError` when either is nonzero. A `worktrees` or
     `clones` INSERT checks its FK by taking `FOR KEY SHARE` on the same
     project row, which conflicts with `FOR UPDATE`. The purge transaction
     runs at the hub default, READ COMMITTED (`default_transaction_isolation`
     on the live hub), so each statement takes a fresh snapshot. A creator
     whose row committed first is counted. A creator whose insert is in
     flight holds `FOR KEY SHARE`, so purge's lock waits for its commit and
     the count that follows sees the row. A creator that inserts after the
     lock blocks until purge commits, then fails the FK.
   - **Early fail-fast check.** The same count beside the active-terminal
     refusal (step 2), so a project with checkouts does not reach the
     destructive phases. This one is advisory; the in-transaction check is
     the correctness boundary.
   - Existing cron retries pick the project up again once an operator has
     deleted those checkouts through the existing worktree and clone delete
     tools. Those tools already remove Cargo targets (see the citation under
     (b)) and keep dirty checkouts.
   - The guard adds about ten lines and no deletion mechanism, so nothing is
     lost when the purge moves to Rust; the contract ports unchanged.
   - Scope: the guard prevents registry-cascade loss for registered
     worktrees and clones only.

   **Creator side of the race.** A creator that loses the race fails its
   INSERT after its directory exists. Worktree creation then calls its
   rollback when the insert raises:
   `src/gobby/worktrees/creation.py:169-191`
   `excerpt_hash=8f4ca0dbd270695b0e0078ebdb5d80cf7f6b7ecdb2f7b5155a7f51d3df2ae8d3`.
   That rollback is best-effort. `_cleanup_git_worktree` awaits
   `delete_worktree` but discards its `GitOperationResult`, and only an
   exception is logged. A `success=False` result ("Failed to remove worktree
   even with fallback") with the directory still on disk passes as normal
   completion:
   `src/gobby/worktrees/creation.py:257-275`
   `excerpt_hash=18c6b880bdaf462638203b21aa64ac6cabfe8bc26bf31a9f6591c230675782c3`.
   Clone isolation records the on-disk path in `partial_state` before the
   insert (`src/gobby/agents/isolation_clone.py:171-185`
   `excerpt_hash=3adebc08fcf85ac1837b8a53937dba03ef08eb5decdfec1ba1fff799e1764472`),
   and `cleanup_environment` deletes a partial clone on prepare failure
   (`src/gobby/agents/isolation_clone.py:218-219`
   `excerpt_hash=9333c23254b32c0da96ab886a5ec0fa7f2d30441b5d0dc95793473c6914e97ff`).
   The guard therefore prevents registry-cascade orphans. It prevents
   directory orphans only when the creator's compensation succeeds. Three
   gaps remain, and all are prerequisites:
   - **Worktree compensation result is ignored.** The minimal repair makes
     `_cleanup_git_worktree` inspect the result and, on failure, log and
     return the exact unreclaimed path and the cleanup error, so the
     creation failure reports it. No generic sweeper. This is a current
     cleanup defect, routed to the Orchestrator as found work.
   - Whether every clone spawn caller invokes `cleanup_environment` after a
     failed insert was not traced. Until it is, the clone guarantee is
     withheld.
   - Neither creation path refuses a soft-deleted project (no `deleted_at`
     check in either). A creator could keep registering checkouts for a
     soft-deleted project and hold its purge off indefinitely. Creation
     admission should refuse soft-deleted projects.

   Isolated tests in `tests/projects/`, on the isolated test hub:
   - a purge of a project with one active `worktrees` row, and separately
     with one `clones` row, returns `failed` and leaves the project row, its
     cron jobs, vectors and hub rows untouched. Once the row is gone, the
     same purge returns `purged`;
   - controlled interleaving: pause a worktree creator after its git
     worktree exists and before registration, let purge pass the early
     count, then resume registration. Either the final check fails the
     purge with the row kept, or the insert fails and the creator removes
     its directory. No registry row or directory is orphaned. Repeat for a
     clone once the caller cleanup is traced;
   - failed compensation: with `delete_worktree` stubbed to return
     `success=False` after a failed insert, worktree creation reports the
     exact unreclaimed path and the cleanup error in its failure result and
     log. It does not report a clean rollback.
2. **Explicit gap: backups and main-checkout Cargo targets.** Purge cannot
   infer these from surviving rows, because soft delete has already released
   `project_checkouts`. Before specifying any retirement, trace the backup
   writers and their retention and restore policy, and identify which
   main-checkout targets Gobby owns. Any cleanup contract must capture that
   ownership evidence before soft delete releases it. Source checkouts and
   foreign targets are preserved. The current evidence justifies no generic
   filesystem sweep.
3. **FK delete action per table (Josh decision).** `metrics_events`,
   `metrics_events_archive` and `tool_schema_hashes` have no FK to
   `projects`. Orphan rows alone do not prove that CASCADE is the wanted
   behavior:
   - `.gobby/plans/completed/hub-data-retention.md:62` (#19655) keeps
     `metrics_events_archive` aggregates indefinitely, so CASCADE there
     contradicts the existing policy. The options are CASCADE, `SET NULL`
     (anonymize, which needs a rule for aggregation-key collisions once
     `project_id` is cleared), or no FK with the rows kept.
   - The writers, readers and project-removal semantics of `metrics_events`
     (raw rows, 30 days) and `tool_schema_hashes` were not traced. Trace them
     before choosing their action.
   - Whatever is chosen lands as a schema change in
     `crates/gcore/assets/schema/baseline.sql`, which Rust owns and which
     therefore survives the migration.
   - `memory_dream_truth_state` is excluded: it has zero orphans and its
     project semantics were not traced.
   - Isolated test: a purge on the isolated test hub (port 60892) produces
     the chosen outcome for seeded rows in each table.
4. **The 53 orphan rows.** Adding a FK requires resolving them first. Before
   any delete, export the exact rows to a verified file (keys, counts, schema
   identity, and a restore procedure tested on the isolated hub). The
   alternative is Josh's explicit approval of an irreversible loss.

Rollback: revert the guard commit. The FK migration needs a down-migration
that drops the constraints. Deleted orphan rows come back only from the
verified export.

## (b) Orphan worktrees under removed projects

### Evidence

Registry (`registry_owner` query, Appendix B): 9 `worktrees` rows and 4
`clones` rows, all `active`, and every registered path exists. All belong to
the gobby project except `~/Projects/gobby-web-dev`, which belongs to
gobby-web. `git worktree list --porcelain` for the gobby repository shows
the main checkout, the 7 lane worktrees and `~/Projects/gobby-wiki`. The
gobby-web repository's list shows `~/Projects/gobby-web` and
`~/Projects/gobby-web-dev`. Those two lists cover every repository with a
registered worktree. The 4 registered clones are standalone repositories,
checked by path only. None of the paths below has a registry row or an entry
in those two lists, and `lsof -d cwd` shows no process with its cwd inside any of them
(re-checked 2026-10-06 01:15 UTC).

| Path | Size | mtime | Contents and status |
|---|---:|---|---|
| `~/.gobby/worktrees/gobby-cli/task-396-module-clustering-from-the-dependency-gr` | 1.0 GB | 2026-08-03 | Only a real `target/` dir is left; `git rev-parse` reports it is not a repository. `gobby-cli` is not a project row. |
| `~/.gobby/worktrees/test-project/gobby-integration-1-readiness-epic` | 8 KB | 2026-06-20 | `README.md` plus a `.git` pointing at a pytest tmp dir (`pytest-730/test_build_readiness_cascades_0`). |
| `~/.gobby/worktrees/epic-22010-native-ask` | 0 | 2026-09-11 | Empty directory. |
| `~/.gobby/worktrees/game-goblins` | 0 | 2026-09-27 | Empty directory. The project is live. |
| `~/.gobby/clones/gobby` | 0 | 2026-09-11 | Empty directory. |

The pytest leak is not live today:

```
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/build_pipeline/test_automation_readiness.py -q -p no:cacheprovider
```

That run printed `Pytest: 1 passed in 6.02s`. The `stat` mtimes of
`~/.gobby/worktrees` and the `test-project` paths were the same before and
after. `tests/conftest.py` now points `GOBBY_HOME` at a per-test temp dir.

Worktree deletion already removes the checkout's Cargo targets:

- `src/gobby/worktrees/deletion.py:150-153`
  `excerpt_hash=ea60b17bd065cc792444b6b183f71d13dcd4a3bcfdace501e9d2af46a978f241`

A `gcode search` found no sweep that reconciles `~/.gobby/worktrees/*`
directories against registry rows.

### Root cause

- For future orphans, the cause is (a): purge cascades the registry rows and
  leaves the directories.
- For the existing relics, attribution is an evidence gap. The `gobby-cli`
  leftover is a real `target/` dir from before #22532 (Worktrees share one
  cargo target dir, so concurrent builds of a crate link each other's
  artifacts), and no registry row, task or transcript ties it to a specific
  removal path.
- The `test-project` dir came from a historical test-isolation leak that no
  longer reproduces.
- The empty project-level dirs are what remains after a project's last
  worktree is removed. They are harmless.

### Minimal fix

No new code beyond (a). The in-transaction purge guard there stops purge from
cascading away registered worktree rows. A racing creator's directory is
removed only when its compensation succeeds, which needs the rollback-result
repair listed in (a). The same guarantee for clones waits on the
caller-cleanup trace and the soft-deleted admission check listed in (a).

The relics need a one-time operator removal, approved by Josh. Immediately
before removing anything, check:

- the path is absent from `worktrees`, `clones` and every repository's
  `git worktree list`;
- `lsof +D <path>` is empty;
- no process has its cwd inside the path;
- the size matches this report.

Rollback: the `gobby-cli` `target/` is a rebuildable build cache. The
`test-project` dir holds only a test fixture `README.md`.

## (c) Old `~/.gobby/cache/cargo-target` (21 GB)

### Evidence

| Entry | Size | mtime |
|---|---:|---|
| `wt-main` | 12 GB | 2026-09-19 |
| `wt-gobby-22529` | 4.6 GB | 2026-09-18 |
| `wt-gobby-22544` | 3.4 GB | 2026-09-18 |
| `wt-lane-22581-gcode-import-communities` | 1.0 GB | 2026-09-19 |
| `wt-gobby-22544-gterm` | 297 MB | 2026-09-18 |
| `1d7a5bc4-6530-4642-aec8-ff41025caaa2` | 0 (empty dir) | 2026-09-12 |
| `5baa0e37-e8a6-4c0a-b226-7510ca894595` | 0 (empty dir) | 2026-09-15 |

**Ownership.** Gobby's only legacy shape is `cache/cargo-target/<project-id>`:

- `src/gobby/agents/cargo_target.py:63-69`
  `excerpt_hash=901be38a5bee4812e1b6042771be31dae5a845b8406aa6bac25c21337dbc7095`
- The migration branch relinks an exact legacy symlink to v2 and never deletes
  anything: `src/gobby/agents/cargo_target.py:197-203`
  `excerpt_hash=371a3de1f484af448ab53fe55203180153d7c5414ef30475db1186c9e692992e`
- `gcode grep` for `cargo-target[^-]` across `src`, `crates`, `scripts` and
  `.gobby/roles` finds only that function and the sandbox root.
- No source, document or commit contains `cargo-target/wt-`; `git log -S` and
  `gcode grep` both come back empty.

**Creator.** Agent sessions set the variable by hand, during the 2026-09-18/19
shared-target incident that #22532 fixed. Each `wt-*` dir has at least one
transcript under `~/.gobby/session_transcripts/` containing
`CARGO_TARGET_DIR=…/cache/cargo-target/<dir>`:

| Dir | Transcripts |
|---|---|
| `wt-main` | `05ec4c9c-2573-4177-b12f-679b31231331.jsonl.gz` |
| `wt-gobby-22529` | `c1af71ce-098f-4e67-8d44-6e2d3810b5d3.jsonl.gz` |
| `wt-gobby-22544` | `1e9b3d25-46ba-47b7-a900-344c3295dd6d.jsonl.gz`, `01a0b791-f74f-7b53-8b8a-2c362dae075f.jsonl.gz` |
| `wt-gobby-22544-gterm` | `1e9b3d25-46ba-47b7-a900-344c3295dd6d.jsonl.gz`, `01a0b791-f74f-7b53-8b8a-2c362dae075f.jsonl.gz` |
| `wt-lane-22581-gcode-import-communities` | `01a0bbeb-fff0-7b03-9d7d-f2b3faafbb54.jsonl.gz` |

The search covered the 742 transcripts modified between 2026-09-17 and
2026-09-23 (Appendix C). The `wt-*` dirs were never Gobby-owned.

**No current use**, within the inventory below (checked 2026-10-05, with the
cwd and config checks repeated 2026-10-06 01:15 UTC):

- no file in any entry is newer than 2026-09-20 (`find -newermt`);
- `lsof` shows no open file under the root, and `lsof -d cwd` shows no
  process cwd inside it;
- no process environment carries a `CARGO_TARGET_DIR` into it;
- `~/.cargo/config` and `~/.cargo/config.toml` do not exist. The repo
  `.cargo/config.toml`, `~/.zshrc`, `~/.zprofile`, `~/.zshenv` and
  `~/.profile` do not reference it. `~/.bashrc` and `~/.bash_profile` do not
  exist;
- no checkout's `target` link points into it. The main checkout, all 7 lane
  worktrees and `ask-probe-source` resolve into `cargo-target-v2`;
  `gobby-cli/task-396` and `gobbyai-crane` have real dirs. Checkouts outside
  the registry, the two worktree lists and `~/.gobby/clones` were not
  inspected.

### Root cause and migration responsibility

#22532 owned only Gobby's exact legacy symlink. That migration is complete:
no checkout links the legacy root any more. The 21 GB belongs to ad hoc agent
caches that no Gobby migration may delete, because the standing rule is to
keep foreign target dirs. Retiring them is operator maintenance.

### Minimal fix

No code change. A one-time `rm -rf` of the five `wt-*` dirs and the two empty
UUID dirs, approved by Josh. Immediately before it runs, repeat every no-use
check above and confirm each path is a real directory, not a symlink, with
the size this report records.

Rollback: none needed. These are rebuildable build caches, and any consumer
that turns up later rebuilds.

### Related observation (not a miss)

`cache/cargo-target-v2` is 119 GB, up from 40 GB on 2026-09-26. Every entry maps
to a live checkout:

- main checkout `gobby-07e4b6f8281b4433`: 61 GB;
- lane worktrees: 22, 16, 15, 3.0 and 1.6 GB;
- `-agent` targets and `ask-probe-source`: 0.

This is Cargo's own artifact growth inside live targets, which Cargo never
garbage-collects. Whether to prune live targets periodically is a separate
product question.

## (d) `unmodeled_observation_events` (495 MB total)

### Evidence

**Execution.** The loop runs `prune_events_older_than(retention_days=30)` at
daemon start and then every 24 h:

- `src/gobby/runner_maintenance/telemetry_loops.py:48-55`
  `excerpt_hash=2afea379277967a4ff0e451aaeceab27f40a04681c12659ef0ad1ce2ad6bc633`
- spawn: `src/gobby/runner_lifecycle_periodic.py:232-238`
  `excerpt_hash=92e107e011b03f48008cc5b5528dd3d4ced9daf3913df7873c833941a6594f16`

The daemon logs record 182 `Periodic unmodeled-observation cleanup` runs since
2026-09-13, for example `2026-10-03 10:08:05 ... removed 7930 old occurrence rows`.
Daemon log times are CDT.

Failure visibility: a failed prune logs `Error in unmodeled observation
cleanup loop: <message>` through `logger.error`, without a traceback, and
the loop then sleeps the full 24 h. Nothing retries sooner:

- `src/gobby/runner_maintenance/telemetry_loops.py:75-78`
  `excerpt_hash=28a7b75fbee53ae5ad4d79e653b597bef63f96e27335ffac7df33fd78878bffd`
- That error line appears 0 times across the retained `daemon.log*` and
  `errors.log*` files (checked 2026-10-06).

**Cutoff.** The prune deletes by `last_seen_at`:

- `src/gobby/storage/unmodeled_observations.py:267-275`
  `excerpt_hash=8039f07940606d6a23b55d3d066502ae0420e9049b3c4ab6ace13eda5458d47b`
- Live `min(last_seen_at)` is `2026-09-05 22:29 UTC`, and only 192 rows are
  past 30 days, which is under one loop period of drift. **The prune keeps up.**

**Refresh on replay is designed behavior.** A duplicate observation sets
`last_seen_at = NOW()`:

- `src/gobby/storage/unmodeled_observations.py:181-185`
  `excerpt_hash=879b42a04bfbe23f31c703e8f29a6c3ee867623275757049fbb4b0faa2a93eca`
- The #17403 (T2: Bounded DB-backed unknown-observation telemetry subsystem)
  validation requires "reprocess ... leaves count=1 (only last_seen_at moves)".
- Effect: 99,412 rows were re-observed more than a day after first sight, and
  9,852 rows first seen over 30 days ago survive because transcript re-reads
  refresh them.

**Volume.** 461,477 live rows across 3,550 sessions, holding about 93 MB of
tuple data. New rows run 11k-24k per day (2026-09-28..10-05). The aggregate
table has 95 keys, and its counts sum to the live row count.

| source/kind/name | count | share |
|---|---:|---:|
| codex/tool_name/`exec` | 387,817 | 84% |
| codex/tool_name/`wait` | 12,276 | 2.7% |
| grok/tool_name/`run_terminal_command` | 12,257 | 2.7% |
| grok/tool_name/`use_tool` | 10,027 | 2.2% |

These tools reach the occurrence writer because `classify_tool` returns
`unknown` for them. Neither `is_shell_tool` (`_SHELL_TOOLS` in
`src/gobby/hooks/_normalization_shell.py`) nor `TOOL_TYPE_MAP` lists them:

- `src/gobby/sessions/transcript_render_blocks.py:333-336`
  `excerpt_hash=ad13704bdf2c3174078530075f4c228f30d6afeabc828941b53c5d244d0b5961`
- `src/gobby/sessions/transcript_tool_metadata.py:64-76`
  `excerpt_hash=5b6bc338c307ddf9e438adbdee6186586c6af58a13a0b7d703700c5a0174c43f`

Sample keys:

- codex `exec`: `["raw","status"]`
- codex `wait`: `["cell_id","max_tokens","yield_time_ms"]`
- grok `run_terminal_command`: `["command","description"]`
- grok `use_tool`: `["tool_input","tool_name"]`

**Bytes.**

| Relation | Size |
|---|---:|
| heap | 113 MB |
| indexes, total | 382 MB |
| `idx_unmodeled_observation_events_group_recompute` | 183 MB (1,077 scans) |
| `unmodeled_observation_events_dedup_key` | 135 MB |
| `idx_unmodeled_observation_events_last_seen` | 42 MB |
| `unmodeled_observation_events_pkey` | 21 MB (0 scans) |

`last_seen_at` is a key column in two of these indexes, so the duplicate-path
UPDATE can never be HOT: `n_tup_hot_upd = 0` of 14,503 updates in the
cumulative counters. Every refresh therefore writes new entries into all four
indexes.

The group-recompute index works out to about 430 bytes per row against a key
of about 80 bytes. That points to bloat, but it is an **estimate**:
`pgstattuple` is not installed, so leaf density was not measured.

**Autovacuum.** Settings are stock (`autovacuum` and `track_counts` on):
50 + 0.2 × reltuples 446,135, a threshold of about 89k dead tuples. The table
held 22,671 dead tuples (28,903 at the 2026-10-06 re-check), with
`last_autovacuum` null and `vacuum_count = autovacuum_count = 0`.

The window those counters cover is not established. PostgreSQL 18.4 started
at `2026-10-05 05:51 UTC`, and `pg_stat_database.stats_reset` is null, so no
explicit reset was recorded. PostgreSQL keeps cumulative statistics across a
clean shutdown and resets them after crash recovery
(https://www.postgresql.org/docs/18/monitoring-stats.html). Whether the
05:51 start followed a clean shutdown was not checked. The counters may
therefore cover about 17 hours or a much longer span. The table's lifetime
vacuum history is an **evidence gap**.

### Root cause

Missed execution is ruled out. The size has three sources:

1. **The volume driver.** A designed one-row-per-occurrence guard meets a
   high-frequency tool that is never modeled. The telemetry did its job by
   surfacing Codex `exec`, but nothing downstream turned that into modeling.
2. **Index churn.** Non-HOT refresh updates write into every index.
3. **Reclaim.** Space already allocated to the table and its indexes is not
   handed back to the operating system without VACUUM FULL or REINDEX.

### Product decisions needed (Josh)

1. **How to model the four tools.** Codex `exec` looks like a code-cell
   executor rather than a shell call, judging by `raw`/`status` paired with
   `wait`'s `cell_id`. That reading is an evidence gap until the Codex tool
   contract is checked. Because `classify_tool` is not provider-aware, a bare
   `exec` entry would also apply to other providers.
2. **Whether occurrence counts are still wanted.** The alternative is
   presence plus a sample per key. With presence only, the occurrence table
   could not grow with tool volume.
3. **Keep the 30-day window and refresh-on-replay?** Both are current design.

### Minimal current-daemon fix, after decision 1

Data-only: add the four names, with their chosen types, to `TOOL_TYPE_MAP` or
`_SHELL_TOOLS`. It is a few lines of data that ports to Rust as data, with no
new mechanism.

Codex `exec` and `wait` reach `classify_tool` with their names unchanged. The
parser's `custom_tool_call` branch copies `payload["name"]` and folds `status`
into the input, which matches the `raw`/`status` sample keys:

- `src/gobby/sessions/transcripts/codex.py:556-561`
  `excerpt_hash=9ebb9add827f1b672e38f072071366cd39dcc716a6a0f2c1f2666a4f413ea086`

If a bare `exec` key would be ambiguous across providers, the Codex-specific
mapping belongs in that branch.

- Isolated test: extend `tests/sessions/test_transcript_renderer.py::test_classify_tool`
  so the four names classify as known.
- Isolated test through the real provider paths: parse a Codex
  `custom_tool_call` for `exec` and `wait`, and a Grok `run_terminal_command`
  and `use_tool`, through the parser into the renderer with a tracker. None
  writes an occurrence row, checked on the isolated test hub. A bare
  `classify_tool` call does not exercise the Codex normalization.
- Expected effect, conditional: the four names make up about 91.5% of the
  snapshot (422,377 of 461,477 rows). If modeling stops every occurrence
  write and replay refresh for them, their rows become eligible 30 days
  after their final `last_seen_at`, and the next daily prune removes them.
  Future row counts still depend on arrivals and replay for other names, and
  allocated disk does not shrink on its own.

Index redesign is not recommended now. Refresh churn should fall roughly in
proportion to the volume removed. That is an estimate to re-measure 30 days
after the fix.

### Maintenance and rollback conditions

- No VACUUM or REINDEX from size alone.
- After the volume fix has aged 30 days, measure bloat before acting:
  - install `pgstattuple` (a schema change, approval needed) and run
    `pgstatindex()` read-only;
  - or use a reviewed bloat-estimate query.
- Only measured bloat justifies `REINDEX INDEX CONCURRENTLY` on the bloated
  indexes. Concurrent mode lets ordinary writes continue through most of the
  rebuild, but it still takes a `SHARE UPDATE EXCLUSIVE` lock, waits for
  transactions that could use the index, needs disk for a second copy of the
  index, and cannot run inside a transaction block. A failure leaves an
  invalid index behind, in one of two states
  (https://www.postgresql.org/docs/18/sql-reindex.html#SQL-REINDEX-CONCURRENTLY).
  Ownership is pending. #22956 does not cover this: its criteria cover only
  the clean-window VACUUM FULL of the tables holding dropped-column TOAST.
  The Orchestrator assigns explicit ownership and scope, and Josh approves
  the exact index list before execution. It then runs as an
  operator-controlled bounded maintenance step:
  - check free disk against the index size, and check for blockers in
    `pg_stat_activity`;
  - set reviewed `lock_timeout` and `statement_timeout` values;
  - run each `REINDEX INDEX CONCURRENTLY` as its own statement outside any
    transaction block, one index at a time;
  - after any cancellation or failure, list the table's indexes with
    `pg_index.indisvalid` and handle the invalid leftover by its suffix,
    which may carry a numeric disambiguator (`_ccnew1`, `_ccold2`):
    - `_ccnew[N]` failed before the swap. It is the transient replacement,
      and the original index is still valid and in use. Confirm that, drop
      only that exact `_ccnew[N]` index, and retry the reindex if still
      approved.
    - `_ccold[N]` failed after the swap. The rebuild itself succeeded and
      only the old index could not be dropped. Confirm the index under the
      original name is valid and still backs the same constraint (for the
      `pkey` and `dedup_key` indexes, check `pg_constraint.conindid`), then
      drop only that exact `_ccold[N]` index. Do not retry the reindex.
  - The approval for each index states explicitly that it covers dropping
    that index's exact leftover. Any other invalid index stops the run for
    review.

  Concurrent mode alone does not prove that no maintenance window is needed.
- A `VACUUM FULL` of the `unmodeled_observation_events` heap needs the same
  routing: explicit ownership from the Orchestrator, Josh's exact approval,
  and an announced clean window. #22956 (Decide retention and cleanup for
  token_events, unmodeled_observation_events, dropped-column TOAST and the
  old cargo target) is the clean-window precedent for its own dropped-column
  TOAST work. It does not own this.
- Rollback for the data fix: revert the commit. New occurrences then resume
  being recorded.

## (e) `token_events` retention

### Evidence

**Policy.** `.gobby/plans/completed/hub-data-retention.md`, written under
#19655 (Define retention policy for high-volume operational tables, closed
2026-08-05):

- line 63: `token_events` is kept 180 days, deleted by `event_at` in daily
  batches of 10,000 rows;
- line 94: `token_event_days: 180`;
- line 121: `idx_token_events_event_at ON token_events(event_at, id)`.

**Implementation absent.**

- `gcode grep` finds no `token_event_days` and no hub-retention loop in `src`.
- The only deletes are per session:
  `src/gobby/storage/token_events.py:244-251`
  `excerpt_hash=d7e7dc5b00119a61b8445bc7db55dac1cde2055f6eca4705b2a33527b90b54b6`
- Neither `baseline.sql` nor the live DB has an `event_at`-leading index: only
  `idx_token_events_dedup`, `idx_token_events_session (session_id, event_at)`
  and the pkey, which has 0 scans.
- `search_tasks` finds no implementation task for the #19655 plan.

**Data.**

- 944,705 rows, 446 MB (heap 225 MB, indexes 221 MB).
- `min(event_at) = 2026-07-31 06:33 UTC`, with 0 rows older than 180 days.
  **The first row becomes eligible on 2027-01-27.**
- Rows per month: Jul 965, Aug 211,856, Sep 533,564, Oct 1-5 198,320. That is
  about 40k rows per day recently.
- 130,919 dead tuples, from per-session delete-and-rebuild churn. The
  autovacuum threshold is about 188k.
- No `token_events` row lacks a `sessions` row.

### Root cause

The policy was specified and closed as a planning document, but never
expanded into implementation work. Nothing is overdue today, so the gap is
latent.

### Retention definition

The existing policy holds: 180 days by `event_at`. No new product decision is
required. The lifetime-totals dependency below is an implementation
dependency and an evidence gap, not a new TTL decision.

One input for Josh: at the current rate, the 180-day steady state is about
7 million rows. The 3.4 GB figure assumes unchanged workload and storage
density. A shorter window reduces steady-state rows. Bytes per row, index
layout and reclaim also affect disk.

**Lifetime totals depend on retained rows today.** #19655 line 63 says
"session-level usage totals remain on `sessions`". The current processor
seeds its running totals from `get_session_totals`, which sums the session's
`token_events` rows (`src/gobby/storage/token_events.py:262-274`). It then
recomputes the totals from those rows and writes them back to `sessions`:

- `src/gobby/sessions/processor_usage.py:116`
  `excerpt_hash=5e133a8ea8dce62a28baf81f250143d3fba772e132cd5656117fe9d0797b53db`
- `src/gobby/sessions/processor_usage.py:300-314`
  `excerpt_hash=ccf5a40044e63d7d2ac601932cbea927bd985c9d66a7c7b43c1f71f81b194de2`

A plain 180-day delete would therefore shrink the lifetime totals of any
session that spans the cutoff and later receives a usage update. A full
transcript replay is a second, untraced path that could reinsert expired
rows.

### Minimal fix

1. **Schema (Rust-owned).** Add `idx_token_events_event_at (event_at, id)` to
   `crates/gcore/assets/schema/baseline.sql`.
2. **Lifetime totals first.** Before deletion is enabled, the usage path must
   stop deriving `sessions` totals from retained `token_events` rows alone,
   and replay must be traced against the cutoff. This is a prerequisite of
   the delete contract, and it lands in the same daemon.
3. **Delete contract.** Implement the full #19655 contract
   (`hub-data-retention.md:32-41,166-169`) in whichever daemon owns periodic
   maintenance when it lands, before 2027-01-27:
   - one hub-global loop, with an advisory lock electing a single owner per
     cycle;
   - ordered batches of 10,000 by `event_at`, one transaction per batch;
   - at most 20 batches per cycle, with a 100 ms cooperative yield between
     batches;
   - the deleted count logged.

   An index plus a capped daily DELETE alone is not the full contract.

A new Python loop now would be discarded mechanism; the deadline leaves room
to land it once. Isolated tests:

- seeded rows at 179 days, 180 days ± 1 s and 181 days delete only past the
  cutoff;
- batch bound and per-cycle cap;
- an empty table is a no-op;
- prune an old event, ingest a new event in the same session, and replay its
  transcript: the session's lifetime totals change only by the new usage,
  and the expired row is not permanently reinserted.

Rollback: disable the loop. Deleted rows are gone, which is why the cutoff
test pins the boundary.

## Found work for the Orchestrator (gobby#14972)

1. **The orphaned code-index sweep fails every cycle, and the cause is never
   logged.** `_sweep_orphaned_index_projects` warns `Orphaned code-index
   project 63dac488-3956-5023-a761-6d0ad76c2601 projection cleanup failed;
   retaining for retry`. It did so on 2026-10-05 at 13:53, 14:51, 15:46, 16:28
   and 17:29 CDT (daemon log local time). As of 18:07 CDT, 17:29 was the
   latest failure.
   - It passes `exc_info=True`
     (`src/gobby/code_index/maintenance.py:303-308`
     `excerpt_hash=cb0a0c0aaa2e2107165ad6ffb5bf90298f35aeb89721be2926c383b8e683f843`),
     but no traceback appears in `code-index-maintenance.log`, `daemon.log` or
     `errors.log`.
   - The project still holds 28,892 `code_calls`, 5,589 `code_symbols` and
     171 `code_indexed_files` rows, last written at `2026-10-05 04:13 UTC`,
     with no `code_indexed_project_states` row.
   - Owner: code-index (gcode) maintenance. #22958 (Plan code-index
     symbol-vector reconciliation and path-aware content retention) is closed
     and covers vectors and content GC. It does not cover this sweep.
2. Observation only, nothing broken: `cargo-target-v2` has grown 3× in 9 days
   inside live targets. See (c).
3. Ownership needed: future `unmodeled_observation_events` REINDEX and heap
   `VACUUM FULL`, if measured bloat justifies them after the volume fix. No
   current task covers them; #22956 owns only the dropped-column TOAST
   VACUUM FULL. See (d), Maintenance and rollback conditions.
4. **Worktree creation rollback ignores a failed removal.**
   `_cleanup_git_worktree` (`src/gobby/worktrees/creation.py:257-275`)
   discards the `GitOperationResult` from `delete_worktree`, so a
   `success=False` removal leaves the directory on disk with only a normal
   creation-failure message. The minimal repair inspects the result and
   reports the exact unreclaimed path and cleanup error. No runtime
   reproduction was run, and no existing relic is attributed to this path.
   Owner: worktree lifecycle. See (a), Creator side of the race.

## Related work

- #22956 (Decide retention and cleanup for token_events,
  unmodeled_observation_events, dropped-column TOAST and the old cargo target)
  owns only the clean-window VACUUM FULL of the tables holding dropped-column
  TOAST. Its items 1, 2 and 4 are answered here. Future
  `unmodeled_observation_events` index and heap maintenance has no owner; it
  is routed to the Orchestrator as found work.
- #22958 (Plan code-index symbol-vector reconciliation and path-aware content
  retention) owns Qdrant and content-GC semantics.
- #19655 (Define retention policy for high-volume operational tables) owns the
  retention policy.
- #17403 (T2: Bounded DB-backed unknown-observation telemetry subsystem)
  designed the occurrence table.
- #22532 (Worktrees share one cargo target dir, so concurrent builds of a
  crate link each other's artifacts) built the v2 target layout.

## Appendix A: diagnostic script

Save the script as `diag.py`. Run it from the repository root, feeding it a
query file on stdin:

```
uv run python diag.py < queries.sql
```

Each query in the file is written `name|SQL`, and queries are separated by a
line containing only `;;`. The script opens the database through gobby's own
CLI runtime without migrations, and never prints connection details.

```python
import json
import sys

from gobby.cli.runtime import CliRuntime

runtime = CliRuntime(None)
db = runtime.require_database(apply_migrations=False)

QUERIES = {name: sql for name, sql in (line.split("|", 1) for line in sys.stdin.read().split("\n;;\n") if line.strip())}

with db.transaction() as conn:
    conn.execute("SET TRANSACTION READ ONLY")
    conn.execute("SET LOCAL statement_timeout = '120s'")
    for name, sql in QUERIES.items():
        try:
            rows = conn.execute(sql).fetchall()
            print(f"== {name}")
            for row in rows:
                print(json.dumps(dict(row), default=str))
        except Exception as exc:  # report and stop: the transaction is aborted
            print(f"== {name} ERROR {type(exc).__name__}: {exc}")
            break

runtime.close()
```

## Appendix B: queries

```
sizes|SELECT c.relname, pg_size_pretty(pg_total_relation_size(c.oid)) AS total, pg_size_pretty(pg_relation_size(c.oid)) AS heap, pg_size_pretty(pg_indexes_size(c.oid)) AS idx, pg_size_pretty(COALESCE(pg_total_relation_size(c.reltoastrelid),0)) AS toast, s.n_live_tup, s.n_dead_tup, s.last_autovacuum FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace LEFT JOIN pg_stat_user_tables s ON s.relid=c.oid WHERE c.relkind='r' AND n.nspname='public' ORDER BY pg_total_relation_size(c.oid) DESC LIMIT 25
;;
churn|SELECT relname, n_tup_ins, n_tup_upd, n_tup_hot_upd, n_tup_del, n_dead_tup, autovacuum_count FROM pg_stat_user_tables WHERE relname IN ('unmodeled_observation_events','token_events')
;;
identity|SELECT current_user, session_user, current_setting('row_security') AS row_security, (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS rolsuper, (SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user) AS rolbypassrls
;;
rls_tables|SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = current_schema() AND c.relname IN ('unmodeled_observation_events','token_events','metrics_events','metrics_events_archive','tool_schema_hashes','worktrees','clones','projects','sessions') ORDER BY c.relname
;;
policies|SELECT tablename, policyname, roles::text, cmd FROM pg_policies WHERE tablename IN ('unmodeled_observation_events','token_events','metrics_events','metrics_events_archive','tool_schema_hashes','worktrees','clones','projects','sessions') ORDER BY tablename, policyname
;;
stats_reset|SELECT datname, stats_reset, pg_postmaster_start_time() AS postmaster_start FROM pg_stat_database WHERE datname = current_database()
;;
version|SELECT current_setting('server_version') AS server_version, current_setting('autovacuum') AS autovacuum, current_setting('track_counts') AS track_counts
;;
table_vacuum|SELECT relname, last_vacuum, last_autovacuum, vacuum_count, autovacuum_count, n_live_tup, n_dead_tup FROM pg_stat_user_tables WHERE relname IN ('unmodeled_observation_events','token_events') ORDER BY relname
;;
idx_usage|SELECT relname, indexrelname, pg_size_pretty(pg_relation_size(indexrelid)) AS size, idx_scan FROM pg_stat_user_indexes WHERE relname IN ('unmodeled_observation_events','token_events') ORDER BY relname, pg_relation_size(indexrelid) DESC
;;
uoe_age|SELECT count(*) AS n, min(last_seen_at) AS min_last, count(*) FILTER (WHERE first_seen_at < now()-interval '30 days') AS first_gt30d, count(*) FILTER (WHERE last_seen_at < now()-interval '30 days') AS last_gt30d, count(*) FILTER (WHERE last_seen_at > first_seen_at + interval '1 day') AS refreshed_gt1d, count(DISTINCT session_id) AS sessions, pg_size_pretty(sum(pg_column_size(t.*))) AS live_bytes FROM unmodeled_observation_events t
;;
uoe_daily|SELECT date_trunc('day', first_seen_at)::date AS d, count(*) FROM unmodeled_observation_events WHERE first_seen_at > now()-interval '8 days' GROUP BY 1 ORDER BY 1
;;
uo_top|SELECT source, kind, name, count, sample_keys FROM unmodeled_observations ORDER BY count DESC LIMIT 8
;;
te_age|SELECT count(*) AS n, min(event_at) AS min_event, count(*) FILTER (WHERE event_at < now()-interval '180 days') AS gt180d FROM token_events
;;
te_month|SELECT date_trunc('month', event_at) AS m, count(*) FROM token_events GROUP BY 1 ORDER BY 1
;;
fk_actions|SELECT c.table_name, COALESCE((SELECT string_agg(pc.confdeltype::text, ',') FROM pg_constraint pc JOIN pg_class t ON t.oid=pc.conrelid JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum = ANY(pc.conkey) WHERE pc.contype='f' AND t.relname=c.table_name AND a.attname='project_id' AND pc.confrelid='projects'::regclass), 'NO_FK') AS on_delete FROM information_schema.columns c JOIN information_schema.tables x ON x.table_schema=c.table_schema AND x.table_name=c.table_name AND x.table_type='BASE TABLE' WHERE c.table_schema='public' AND c.column_name='project_id' ORDER BY 2, 1
;;
no_fk_orphans|SELECT 'metrics_events' AS t, project_id, count(*) FROM metrics_events m WHERE project_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM projects p WHERE p.id=m.project_id) GROUP BY 2 UNION ALL SELECT 'metrics_events_archive', project_id, count(*) FROM metrics_events_archive m WHERE project_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM projects p WHERE p.id=m.project_id) GROUP BY 2 UNION ALL SELECT 'tool_schema_hashes', project_id, count(*) FROM tool_schema_hashes m WHERE project_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM projects p WHERE p.id=m.project_id) GROUP BY 2
;;
code_index_orphans|SELECT project_id, count(*) FROM code_calls t WHERE NOT EXISTS (SELECT 1 FROM projects p WHERE p.id=t.project_id) AND NOT EXISTS (SELECT 1 FROM code_indexed_project_states s WHERE s.project_id=t.project_id) GROUP BY 1
;;
te_orphans|SELECT count(*) AS token_events_without_session FROM token_events t WHERE NOT EXISTS (SELECT 1 FROM sessions s WHERE s.id = t.session_id)
;;
soft_deleted|SELECT id, name, deleted_at FROM projects WHERE deleted_at IS NOT NULL
;;
projects|SELECT id, name FROM projects ORDER BY name
;;
registry_owner|SELECT 'worktree' AS kind, project_id, worktree_path AS path, status FROM worktrees UNION ALL SELECT 'clone', project_id, clone_path, status FROM clones ORDER BY 1, 3
```

## Appendix C: filesystem commands

```
du -sh ~/.gobby/cache/cargo-target/* ~/.gobby/cache/cargo-target-v2/*/* ~/.gobby/worktrees/* ~/.gobby/clones/*
for c in ~/Projects/gobby ~/.gobby/worktrees/*/* ~/.gobby/clones/*; do [ -L "$c/target" ] && echo "$c -> $(readlink "$c/target")"; done
for d in ~/.gobby/cache/cargo-target/*; do find "$d" -type f -newermt 2026-09-20 | head -1; done
lsof 2>/dev/null | grep -F '/.gobby/cache/cargo-target/'
ps -Ewwax | grep -oE 'CARGO_TARGET_DIR=[^ ]*cache/cargo-target/[^ ]*' | sort -u
grep -h -c 'Periodic unmodeled-observation cleanup' ~/.gobby/logs/daemon.log*
grep -h '63dac488-3956-5023-a761-6d0ad76c2601' ~/.gobby/logs/code-index-maintenance.log
du -sh ~/.gobby/backups/*
git -C ~/Projects/gobby worktree list --porcelain | grep '^worktree '
git -C ~/Projects/gobby-web-dev worktree list --porcelain | grep '^worktree '
git -C ~/.gobby/worktrees/gobby-cli/task-396-module-clustering-from-the-dependency-gr rev-parse --git-common-dir
lsof -d cwd -Fn | grep -E '^n.*(/\.gobby/cache/cargo-target/|/\.gobby/worktrees/(gobby-cli|test-project|epic-22010-native-ask|game-goblins)|/\.gobby/clones/gobby$)'
for f in ~/.cargo/config ~/.cargo/config.toml .cargo/config.toml ~/.zshrc ~/.zprofile ~/.zshenv ~/.bashrc ~/.bash_profile ~/.profile; do [ -f "$f" ] && { grep -q -E 'cargo-target([^-]|$)' "$f" && echo "MATCH $f" || echo "clean $f"; }; done
for n in 'wt-main' 'wt-gobby-22529' 'wt-gobby-22544[^-]' 'wt-lane-22581-gcode-import-communities' 'wt-gobby-22544-gterm'; do echo "== $n"; find ~/.gobby/session_transcripts -name '*.jsonl.gz' -newermt 2026-09-17 ! -newermt 2026-09-23 -print0 | xargs -0 zgrep -l -E "CARGO_TARGET_DIR=[^ \"]*cargo-target/$n"; done
```

The `zgrep` loop ran on 2026-10-06 for all five dirs, and each returned at
least one transcript. The `lsof -d cwd` command printed nothing, and the
config loop printed only `clean` lines.
