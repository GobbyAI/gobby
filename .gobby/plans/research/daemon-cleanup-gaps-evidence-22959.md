# Current-daemon cleanup gaps: evidence report

Task: #22959 (Investigate current-daemon cleanup gaps across project removal,
worktrees, caches and telemetry), under #22949 (Lane 7 - Planning/research).
Author: Plan Writer 3 (gobby#15468). Measured 2026-10-05 between 22:35 and
23:30 UTC. Source binding is HEAD `d830d904737191dacbdd7d153d22992d1ee5f154`.

Method: every measurement was read-only. Database reads used the diagnostic
script in Appendix A, which opens a `READ ONLY` transaction with a 120 s
statement timeout and never applies migrations. Filesystem reads used `du`,
`stat`, `find`, `readlink`, `lsof`, `ps` and `grep`. Nothing was deleted,
vacuumed, reindexed or migrated. `~/.gobby/bootstrap.yaml` and
`~/.gobby/local_cli_token` were not read.

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
| (a) Project removal leaves state | Confirmed. The purge never touches the filesystem, and four tables with no FK are never deleted. | A small current-daemon guard plus a schema FK. |
| (b) Orphan worktrees | Confirmed as relics: 1.0 GB plus empty dirs. None has a registry row. Creator attribution is an evidence gap. | Josh-approved operator removal. The guard in (a) prevents new orphans from purge. |
| (c) Old 21 GB `cargo-target` | Confirmed stale. The 21 GB was created ad hoc by agents. Gobby's own legacy entries are empty. | Josh-approved operator removal. No code change. |
| (d) `unmodeled_observation_events` size | The prune keeps up. Size comes from volume plus index churn: 84% of rows are one unmodeled Codex tool. | A product decision, then a data-only modeling fix. |
| (e) `token_events` retention | The policy exists (180 days, #19655) and was never implemented. No row is eligible until 2027-01-27. | Schema index plus a delete contract, landing before 2027-01-27. |

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

No step touches the filesystem: worktree or clone directories, Cargo targets
and `~/.gobby/backups/<project-uuid>` are all left in place.

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
- No project row is currently soft-deleted.

### Root cause

The purge was built around hub rows and FK cascade. Its contract has no
filesystem step and no list of tables without an FK. Cascading `worktrees` and
`clones` rows also erases the only registry evidence that those directories
exist.

Evidence gap: no lifecycle record survives purge, because
`project_lifecycle_events` cascades. Whether projects `5baa0e37` and `3d14159f`
were removed by today's purge or by an older delete path cannot be proven.

### Minimal current-daemon fix

1. **Purge guard.** `_purge_project` returns `PurgeOutcome.failed` while the
   project still has `worktrees` or `clones` rows, with a message naming the
   count. Existing cron retries then pick the project up again once an
   operator has deleted those checkouts through the existing worktree and
   clone delete tools. Those tools already remove Cargo targets (see the
   citation under (b)) and keep dirty checkouts.
   - This is a guard of about five lines. It adds no deletion mechanism to
     purge, so nothing is lost when the purge moves to Rust; the contract
     ports unchanged.
   - Isolated test: in `tests/projects/`, a purge of a project with one
     active `worktrees` row returns `failed` and leaves the project row in
     place. Once the row is gone, the same purge returns `purged`.
2. **Schema FK.** Add `ON DELETE CASCADE` FKs from `metrics_events`,
   `metrics_events_archive` and `tool_schema_hashes` to `projects`.
   - This is a schema change in `crates/gcore/assets/schema/baseline.sql`,
     which Rust owns and which therefore survives the migration.
   - The migration must first delete the 53 orphan rows listed above. That
     is a destructive step, so Josh approves it before landing.
   - `memory_dream_truth_state` is excluded: it has zero orphans and its
     project semantics were not traced.
   - Isolated test: a purge on the isolated test hub (port 60892) removes
     seeded rows from all three tables.

Rollback: revert the guard commit. The FK migration needs a down-migration
that drops the constraints; the deleted orphan rows are not restorable,
which is why the approval gate applies.

## (b) Orphan worktrees under removed projects

### Evidence

Registry: 9 `worktrees` rows and 4 `clones` rows, all `active`, and every
registered path exists. The gobby repository's `git worktree list` shows the
main checkout, the 7 lane worktrees and `~/Projects/gobby-wiki`.
`~/Projects/gobby-web-dev` belongs to the gobby-web project. None of the
paths below has a registry row.

| Path | Size | mtime | Contents and status |
|---|---:|---|---|
| `~/.gobby/worktrees/gobby-cli/task-396-module-clustering-from-the-dependency-gr` | 1.0 GB | 2026-08-03 | Only a real `target/` dir is left. `gobby-cli` is not a project row. |
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

No new code. The purge guard in (a) stops purge from creating new orphans.

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

**Creator.** Agent sessions set the variable by hand. Transcripts
`~/.gobby/session_transcripts/05ec4c9c-2573-4177-b12f-679b31231331.jsonl.gz` and
`c1af71ce-098f-4e67-8d44-6e2d3810b5d3.jsonl.gz` contain
`CARGO_TARGET_DIR=/Users/josh/.gobby/cache/cargo-target/wt-main` and
`.../wt-gobby-22529`, during the 2026-09-18/19 shared-target incident that
#22532 fixed. The `wt-*` dirs were never Gobby-owned.

**No current use**, checked 2026-10-05:

- no file in any entry is newer than 2026-09-20 (`find -newermt`);
- `lsof` shows no open file under the root;
- no process environment carries a `CARGO_TARGET_DIR` into it;
- `~/.cargo/config*`, the repo `.cargo/config.toml` and the shell profiles do
  not reference it;
- no checkout's `target` link points into it. The main checkout, all 7 lane
  worktrees and `ask-probe-source` resolve into `cargo-target-v2`;
  `gobby-cli/task-396` and `gobbyai-crane` have real dirs.

### Root cause and migration responsibility

#22532 owned only Gobby's exact legacy symlink. That migration is complete:
no checkout links the legacy root any more. The 21 GB belongs to ad hoc agent
caches that no Gobby migration may delete, because the standing rule is to
keep foreign target dirs. Retiring them is operator maintenance.

### Minimal fix

No code change. A one-time `rm -rf` of the five `wt-*` dirs and the two empty
UUID dirs, approved by Josh. Immediately before it runs, repeat the four
no-use checks above.

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
UPDATE can never be HOT: `n_tup_hot_upd = 0` of 14,503 updates since stats
start. Every refresh therefore writes new entries into all four indexes.

The group-recompute index works out to about 430 bytes per row against a key
of about 80 bytes. That points to bloat, but it is an **estimate**:
`pgstattuple` is not installed, so leaf density was not measured.

**Autovacuum.** Settings are stock: 50 + 0.2 × reltuples 446,135, a threshold
of about 89k dead tuples. The table holds 22,671 dead tuples, and
`last_autovacuum` is null. Postgres started at `2026-10-05 05:51 UTC`, so the
cumulative stats cover only about 17 h. Whether autovacuum ever ran before
that is an **evidence gap**.

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

- Isolated test: extend `tests/sessions/test_transcript_renderer.py::test_classify_tool`
  so the four names classify as known.
- Rendering one Codex `exec` block with a tracker writes no occurrence row,
  checked on the isolated test hub.
- After about 30 days, the existing prune shrinks the live rows by about 90%
  without any maintenance.

Index redesign is not recommended. Once the volume is fixed, churn falls by
the same factor.

### Maintenance and rollback conditions

- No VACUUM or REINDEX from size alone.
- After the volume fix has aged 30 days, measure bloat before acting:
  - install `pgstattuple` (a schema change, approval needed) and run
    `pgstatindex()` read-only;
  - or use a reviewed bloat-estimate query.
- Only measured bloat justifies `REINDEX INDEX CONCURRENTLY` on the bloated
  indexes. That runs without blocking writes and needs no clean window.
- `VACUUM FULL` of the heap belongs in the #22956 (Decide retention and
  cleanup for token_events, unmodeled_observation_events, dropped-column TOAST
  and the old cargo target) clean window.
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
required.

One input for Josh: at the current rate, the 180-day steady state is about
7 million rows. Assuming linear scaling, that is roughly 3.4 GB on disk.
Shortening the window is the only lever if that is too large.

### Minimal fix

1. **Schema (Rust-owned).** Add `idx_token_events_event_at (event_at, id)` to
   `crates/gcore/assets/schema/baseline.sql`.
2. **Delete contract.** Implement the #19655 contract in whichever daemon owns
   periodic maintenance when it lands, before 2027-01-27: daily, batches of
   10,000 by `event_at`, at most 20 batches per run, with the deleted count
   logged.

A new Python loop now would be discarded mechanism; the deadline leaves room
to land it once. Isolated tests:

- seeded rows at 179 days, 180 days ± 1 s and 181 days delete only past the
  cutoff;
- batch bound and per-run cap;
- an empty table is a no-op.

Rollback: disable the loop. Deleted rows are gone, which is why the cutoff
test pins the boundary.

## Found work for the Orchestrator (gobby#14972)

1. **The orphaned code-index sweep fails every cycle, and the cause is never
   logged.** `_sweep_orphaned_index_projects` warns `Orphaned code-index
   project 63dac488-3956-5023-a761-6d0ad76c2601 projection cleanup failed;
   retaining for retry`. It did so at 13:53, 14:51, 15:46, 16:28 and 17:29 UTC
   on 2026-10-05, and probably later.
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

## Related work

- #22956 (Decide retention and cleanup for token_events,
  unmodeled_observation_events, dropped-column TOAST and the old cargo target)
  owns the clean-window VACUUM FULL. Its items 1, 2 and 4 are answered here.
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
stats_window|SELECT pg_postmaster_start_time() AS started
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
registry|SELECT 'worktree' AS kind, worktree_path AS path, status FROM worktrees UNION ALL SELECT 'clone', clone_path, status FROM clones
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
```
