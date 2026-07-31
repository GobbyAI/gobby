# Foundation Cleanup — gcore Schema Authority

Plan artifact: `.gobby/plans/gcore-schema-authority.md` (canonical). Plan kind:
implementation. New root epic.

> **Plan ID:** gcore-schema-authority

## Context
`kind: framing`

Plan #2 from `.gobby/plans/two-daemon-hub.md` Decision Record item 8, activated.
The DB/codebase audit (two-daemon-hub.md Appendix Parts 2–4) found: a hijacked
migration slot (346) breaking `cron_jobs.display_name` in production, ~1,400+
lines of import-graph-verified dead Python, 5 dead tables costing ~967K wasted
hot-path reads per stats window, 15 dead columns, dead Rust/web/deps, and a
46-file migration chain (306–351) ready to flatten. End state: dead schema and
dead code removed, a flattened reproducible baseline, and the gcore Rust crate
as authoritative owner of the data schema (DDL: baseline, migrations, schema
contract). Machine identity is re-keyed to install-generated UUIDs
(`machines.id UUID` PK). The Part 1 hub bloat is purged behind verified
backups and Qdrant/FalkorDB are reconciled to live consumers (P2) — a clean
data model for the new hub PC. Python keeps its CRUD; feature-crate build-out
is a later plan.

**Data-deletion policy (amended by Josh 2026-07-30; supersedes the original
no-data-deletion prime directive).** The enumerated Part 1 bloat is purged in
this plan — "I don't need the data today" — gated on verified full backups
(0.4, re-run immediately before the purge). Ongoing retention-policy
machinery stays out of scope for the future retention plan. Outside the
Decision 11 enumeration, live-table data remains untouched — `recall_*` research data in
particular; the 3 leaked empty `gobby_test_*` pytest schemas stay the only
other authorized deletion.

**Decision Record (elicited with Josh, locked):**
1. P3 flattens **after** M0 (#17488) machine-scoping migrations land and are
   applied on the hub; live 346 divergence repaired first. Within P1, the
   machine-identity redesign (2.18/2.19) lands **before** M0's machine-scoping
   migrations so M0 stamps `machine_id UUID REFERENCES machines(id)` on
   worktrees/clones/agent_runs/cron_runs instead of TEXT. The M0 artifact
   still says `machine_id TEXT NOT NULL` + slot 343
   (`m0-shared-datastores-bridge.md:169-199`, verified) — 2.19 amends it
   before M0 expansion (Codex review, blocker). Migration numbers in
   this plan are re-verified against disk + live `MAX(version)` at
   implementation (352/353 were consumed mid-planning).
2. Schema baseline + migrations + runner machinery live in **gcore (library,
   `postgres` feature)**; new **`gobby-daemon` crate → `gdaemon` binary** (no
   daemon behavior yet) exposes `schema apply|verify|version`; Python daemon +
   pytest shell out; Python `MigrationRunner` + baseline file deleted in P4.
3. TTS: reuse existing `voice.enabled`/`voice.tts_enabled`; no new flag; remove
   `voice = []` extra; chatterbox-tts stays required with its 9 uv pins.
4. Decompose all 3 oversized gcode Rust files in this plan; adopt out-of-line
   `#[path] mod tests;` convention in crates guidance.
5. Legacy paths: remove 20, keep 3 (tmux.conf preference, placeholder
   force-enable, transcript marker stripping), evidence-gate github legacy uuid
   seeds (sequenced after #19367).
6. CodeIndexConfig: per-field disposition (no blanket keep); hardcoded gcode
   defaults stay hardcoded; `indexing.extra_excludes` noted as future follow-up.
7. Webhook-stack consolidation folded in: httpx survives, aiohttp dep
   removed; one shared `WebhookTransport` carries the executor's
   SSRF/pinning semantics for both stacks (2.11). No outbound signing
   exists in either stack — the earlier HMAC-preservation claim is
   corrected; none is added.
8. Machine identity: `machines.id UUID PRIMARY KEY` replaces the opaque TEXT
   `machine_id` key (#17427 contract retired) — no text-key fallback. New ids
   are install-generated uuid4 (`py-machineid` removed). `~/.gobby/machine_id`
   stays the local identity anchor (file-first; the DB derives from it); a
   one-time app-level migration remaps the live machine's hash key to a fresh
   UUID and rewrites the file, sequenced after 2.21 retires legacy Fernet.
   Boot-time `machines` registration becomes canonical. Non-live machine
   rows are retired, never mapped — no legacy id survives the cutover.
   `sessions.machine_id` becomes a nullable UUID FK — all non-UUID values
   map to NULL under a `NULLS NOT DISTINCT` natural key, behind a
   zero-unmapped gate. Sentinel→NULL mapping can collapse previously
   distinct session natural keys (Codex F7): a projected-key collision
   preflight runs first, and colliding rows resolve by a deterministic
   survivor policy (latest activity wins; FKs rewritten to the survivor;
   duplicate rows removed — authorized as part of this cutover, an
   explicit Decision 11 extension). Every retired machine id gets a
   per-ID tombstone row so offline machines re-key deterministically at
   next boot, and identity rotation is resumable per identity — one
   completed journal row never stands in for the full legacy inventory.
   `bin_update_state` re-keys to
   `(machine_id, tool_name)`. Pack ships the identity file; unpack skips it by
   default with `--restore-identity` opt-in (D1 contract).
9. project.json: stays committed (confirmed after considering untracking — the
   committed UUID is what makes two machines resolve one project per
   `shared-remote-stack.md:82-83`, and the verification payload is repo
   config); committed-UUID keying stays — identity is declared, not derived
   (git-derived ids and machine-association auto re-key rejected: shallow
   clones, history rewrites, fork ambiguity, wrong-discriminator). The
   contributor-flow defects fold into 2.20; no `--new-id` affordance.
   `linear_*` lives in the `projects` row (its authoritative home) — the file
   mirror is write-noise and stops.
10. State JSONLs: `.gobby/tasks.jsonl` + `.gobby/memories.jsonl` leave git.
   They were push-point backups — backup becomes machine-local; GitHub issues
   are the public task surface. History scrub before repo publication is a
   recorded follow-up.
11. Part 1 disposition (Josh, 2026-07-30 — amends the prime directive): the
   enumerated bloat is purged in this plan behind verified backups ("I don't
   need the data today"): one-time purges + writer-side fixes (rule-eval
   allow telemetry redirected to the rolling-log + Prometheus/OTel surface —
   an allow is audit trail too, so it is rerouted, never silently dropped)
   + `token_events` index drops + BM25 verification + code-index probe and
   Qdrant/FalkorDB orphan cleanup + historical dream-journal
   `merge`/`supersede` snapshot rows so the dream CHECK constraints tighten
   to the live action set (0.4, 2.3, 2.12, P2). Ongoing retention-policy
   machinery (TTLs, cadences, spans volume tuning) remains the future
   retention plan's. Full backup — docker volumes + pg_dump + Qdrant
   snapshots + FalkorDB RDB — precedes all destructive work and re-runs
   immediately before the purge, so nothing is lost.
12. Out-of-scope disposition (Josh, 2026-07-30): every deferred-work item in
   Out of Scope becomes a leaf task in a follow-up epic filed at this plan's
   close-out — tasks are NOT created during planning or execution. The epic
   also carries plan-drafting tasks fleshing out the drafted #17488-adjacent
   plans (future retention plan; hub-PC datastore move; shared daemon with
   machine-local execution / feature-crate build-out per the
   `two-daemon-hub.md` roadmap). Already-tracked items link their existing
   ids instead of duplicating. Exclusion: fleet management
   (`machines.owner_user_id` real FK + user enrollment, M3) stays put — not
   pre-0.5.0 work.

**Evidence standard (every removal deliverable):** verification evidence
(import-graph, call-graph, row count, config state) plus an explicit
kept-adjacent list. pg_stat counters alone are insufficient (PG18 discards on
unclean shutdown; ~4-day window observed) — re-verify with `count(*)`,
`max(updated_at)`, identity-sequence `last_value`.

## Constraints
`kind: framing`

- No backward compatibility anywhere (pre-0.5.0).
- Platform scope (resolves the POSIX-vs-multi-platform conflict — Codex F15,
  planning resolution 2026-07-30): the hub-side cutover and destructive
  tooling (identity cutover, destructive gate, epoch, flatten) target POSIX
  hosts (macOS/Linux) in 0.5.0 — no Windows hub support pre-0.5.0. Exclusive
  file locking and durable replacement go through one small
  `utils/durable_file.py` abstraction (flock + fsync file/parent-dir on
  POSIX) so a later Windows port swaps the implementation, not the callers.
  Existing Windows-tolerant client code (WSL path handling, kill
  strategies) is unaffected.
- Recall/experiment tables (`recall_*`, shadow snapshots) exempt from removal —
  academic-paper research data, collection ongoing. Schema still ports in P4.
- DB registry rows are source of truth over YAML templates.
- Hosted-path constraint: schema/migration machinery must not foreclose gdaemon
  as future hosted backend (storage traits backable by direct-PG or remote-API;
  client DSN access is temporary).
- Rust work: load `rust` skill + follow crates/CLAUDE.md; rebuild AND reinstall
  `~/.gobby/bin/{gcode,ghook,gwiki,gdaemon}` as part of acceptance.
- Dream CHECK constraints tighten to the live action set (2.12) — the
  earlier historical-values keep is reversed (Josh 2026-07-30; Decision 11
  authorizes the row purge).
- Per-surface validation: focused pytest (`GOBBY_TEST_PROTECT=1`), `uv run ruff
  check src/`, `mypy src/`, `cargo check`/`clippy -p <crate>`, `npm run build`.

---

## P0: Breakage + migration integrity
`kind: framing`

Prerequisite for everything. Execution order inside P0: 0.1 → 0.4 → 0.7 →
0.5 → 0.2 → 0.3, with 0.8 (runner-maintenance decomposition) any time
before its first P1 consumer (2.4). 0.6 (M0-artifact amendment) is NOT an
execution-phase leaf: it executes during the planning session itself, in
the post-approval sequence before either plan registers or expands (Codex
round 7); its section below records the spec and stays in the DAG as the
verification gate. The backup tooling, the shared maintenance epoch, and
the runner-level destructive gate land
BEFORE the first destructive migration file (355) exists on disk, because
`MigrationRunner.apply_pending` (`storage/migrations.py:122-150`) applies
any pending file unattended from daemon boot (`runner_init/helpers.py:99`)
and from ~25 CLI entry points (`storage/hub/runtime.py:20` defaults
`apply_migrations=True`) — transcript discipline and task ordering cannot
stop it (Codex review, blocker). Gates: no P3 (flatten) work until 0.3's
diff is fully reconciled; no destructive migration applies anywhere except
through 0.5's gated path, inside an open 0.7 maintenance epoch, backed by
a fresh restore-verified 0.4 manifest that records that same epoch id
(epoch-bound, not merely fresh — Codex round 7).
The chain applies strictly in slot order: `_discover_migrations` sorts
whatever files are present and accepts gaps (`migrations.py:218-248`,
verified), so order is enforced twice — 0.5's contiguous-range guard in
the runner, plus formal slot-chain task dependencies across every
migration-shipping leaf (execution notes).

### 0.1 Fix repo config breakage [category: config]
`kind: deliverable`

Targets:
- `schemas/diagnose-output.v2.schema.json::*` — scope-reason: mirror re-synced byte-identical from the canonical ghook copy
- `.gitleaks.toml`
- `.github/coderabbit.yaml`

Sync `schemas/diagnose-output.v2.schema.json` from the canonical
`crates/ghook/schemas/` copy (drifted: missing `local_token_file_present`,
`auth_401_remediation`; `schema-mirror-check.yml` sha256-fails any PR touching
`schemas/**`). Delete the 0-byte `.gitleaks.toml` (restores gitleaks defaults —
currently zero rules = scanning silently disabled while pre-commit runs gitleaks
v8.30.0). Delete `.github/coderabbit.yaml` (unread duplicate; its 7 path rules
match zero files).
Evidence standard (restated for expansion — this section is
self-contained): each of the three fixes ships its own verification
evidence in the session transcript (mirror sha256 comparison against the
ghook copy; gitleaks default-ruleset run output; zero-consumer proof for
the coderabbit file). Kept-adjacent:
`crates/ghook/schemas/diagnose-output.v2.schema.json` (the canonical
copy — untouched source of the sync); the pre-commit gitleaks hook entry
itself; `.github/workflows/schema-mirror-check.yml` (the enforcing
check, unchanged).

**Acceptance:**
- 0.1.1 - Mirror byte-identical to ghook copy; schema-mirror-check passes.
  file: `schemas/diagnose-output.v2.schema.json`.
- 0.1.2 - `.gitleaks.toml` absent; pre-commit gitleaks hook runs with default
  rules and passes. behavior: "gitleaks default ruleset active" in pre-commit run.
- 0.1.3 - `.github/coderabbit.yaml` deleted. file: `.github/coderabbit.yaml`.
- 0.1.4 - Per-fix evidence + the kept-adjacent ledger recorded. behavior:
  "0.1 evidence ledger" in session transcript.

### 0.2 Repair hijacked migration slot 346 + drop orphan tmux tables [category: code] (depends: 0.4, 0.5)
`kind: deliverable`

Targets:
- `src/gobby/storage/migrations/355_reconcile_346_cron_display_name.sql`
- `tests/storage/test_migration_contract.py::*` — scope-reason: migration-355 contract coverage added

Slot 346 was consumed on the live hub by an abandoned tmux-input-arbiter WIP
(never committed on any branch), so the repo's `346_cron_display_name.sql` is
recorded applied but never ran: live `cron_jobs` lacks `display_name` while
`storage/cron_display.py`, `/api/cron/jobs*`, MCP `update_cron_job`, and CronTab
(#19160) all use it (`cron_models.py:76` degrades silently). Ship migration 355
(slot 354 is 0.7's bookkeeping/ledger precursor; 352/353 were claimed
mid-planning — re-verify the head at implementation) doing `ALTER TABLE cron_jobs ADD COLUMN
IF NOT EXISTS display_name TEXT` plus `DROP TABLE IF EXISTS
tmux_input_requests, tmux_input_pane_states` (zero code references at HEAD;
the writer was the arbiter WIP itself running in the local daemon — pg_stat
shows writes through 2026-07-28, wake/`/compact` tmux deliveries to codex
panes, ceasing when the WIP was discarded; re-verify no new writes at drop
time). Precedent: `306_reconcile_live_hub_schema_drift.sql`. Migration 355 is
destructive-marked (`-- gobby:destructive`, 0.5): it never auto-applies —
it lands through the gated apply against a fresh 0.4 manifest.
Kept-adjacent: `346_cron_display_name.sql` stays in the chain (correct for
fresh installs); `cron_models.py` defensive fallback stays until P3.

**Acceptance:**
- 0.2.1 - Migration 355 exists, is dual-shape safe (fresh + live), and
  carries the destructive marker. file:
  `src/gobby/storage/migrations/355_reconcile_346_cron_display_name.sql`.
- 0.2.2 - Live hub has `cron_jobs.display_name`; tmux tables gone. behavior:
  "column present, orphan tables absent" via psql check in session transcript.
- 0.2.3 - Contract test covers 355. test: `tests/storage/test_migration_contract.py`.

### 0.3 Build fresh-vs-live schema diff harness [category: code] (depends: 0.2)
`kind: deliverable`

Targets:
- `scripts/schema_diff.py`
- `src/gobby/storage/migrations/356_reconcile_live_hub_schema_drift_v2.sql`

Script dumps a fresh-from-migrations schema (reuse the canonical build path the
pytest fixture already exercises: `PostgresHubDatabase.apply_migrations` into a
scratch `gobby_test_*` schema, per `tests/fixtures/postgres.py:274-278`) and
normalized-diffs it against the live hub schema (`pg_dump --schema-only`
normalized). Every divergence gets an explicit resolution — a reconcile
migration (356) or a documented accept — before P3. This is the non-negotiable
flatten gate: flattening an unreconciled schema bakes contamination into the
baseline. The 41 string-match contract tests provably miss definition drift
(`docs/reviews/storage-core.md:101-106`); this harness is the real check.
The harness also emits a canonical normalized seed-row manifest (the
baseline's seed INSERTs, `postgres_baseline_schema.sql:2402+`) and compares
rows across builds — DDL equivalence alone cannot prove registry/bootstrap
data equivalence (Codex review). Seed comparison has two modes (Codex
review): exact normalized equality is required only between machine-built
schemas (migrated-fresh vs flattened-fresh); the live hub is checked in
invariant mode — required seed rows present by natural key, no unexpected
rows in seed-owned namespaces — with documented mutable-field exclusions
(user-toggled `enabled`, timestamps, definition drift owned by the registry
refresh path), because installed DB registry rows are authoritative live
state. One divergence is pre-recorded from the runtime-DDL audit:
`memory_dream_truth_state` exists on the live hub only via
`ensure_dream_schema` runtime DDL (`memory/dream/storage_schema.py:152` —
in no baseline or migration); the reconcile migration adopts it into the
chain (4.4 deletes the runtime path later).

**Acceptance:**
- 0.3.1 - Diff script exists, reproducible, documented usage. file:
  `scripts/schema_diff.py`.
- 0.3.2 - Zero unexplained divergences between fresh and live; resolutions
  recorded. behavior: "clean diff output" in session transcript.
- 0.3.3 - Reconcile migration ships if needed (else explicitly recorded as
  not-needed). file: `src/gobby/storage/migrations/356_reconcile_live_hub_schema_drift_v2.sql`.

### 0.4 Hub backup command + verified-restore manifest [category: code] (depends: 0.1)
`kind: deliverable`

Targets:
- `src/gobby/cli/hub_backup/cli.py`
- `src/gobby/cli/hub_backup/_manifest.py`
- `src/gobby/cli/hub_backup/_stores.py`
- `src/gobby/cli/hub_backup/_verify.py`
- `src/gobby/cli/postgres_backup.py::*` — scope-reason: v2 manifest rewrite touches the backup/verification surface broadly

New package, split up front — command (`cli.py`), v2 manifest model +
schema (`_manifest.py`), per-store backup/restore drivers
(`_stores.py`), scratch-restore verification (`_verify.py`) — so no
module approaches the 1,000-line ceiling (Codex F14).

Hub backup gets its own command — `gobby hub-backup` (Codex review:
routine `gobby pack` keeps its machine-migration semantics and never
gains surprise datastore stops; the new command is the only
stop-consistent path). It builds on `create_postgres_backup`
(`cli/postgres_backup.py:49`) but the verification contract is rewritten
(Codex review, blocker — today `verified: True` is set after
`_verify_dump_with_pg_restore`, which only pipes the dump through
`pg_restore --list`, `postgres_backup.py:246-260`: archive readability,
not restorability). A versioned manifest (v2) separates
`archive_verified` from `restore_verified`, each with method + timestamp,
and adds a stable source-DB identity (`pg_control` system identifier +
database name + `pg_database.oid` — a same-name drop/recreate changes
the oid, so name alone can false-match; review finding) with a
separately recorded `backup_starting_head` (Codex
review, blocker: identity must survive the destructive batch's own head
changes), per-artifact sha256, and per-table row-count probes. `restore_verified` is earned only by an actual
pg_restore into a scratch database with row counts checked against the
manifest probes; a v1 manifest or bare `verified: True` NEVER satisfies
the destructive gate (0.5). The run is daemon-quiesced end-to-end
(review finding; precedent: `pack()` already stops the daemon for
consistency, `cli/pack.py:350`) — the daemon stops first and restarts
last in the `finally`, so the PG dump, volume tarballs, and store
snapshots are mutually consistent. Under an open maintenance epoch that
`finally` restart is suppressed (Codex round 7 — restarting the daemon
between backup and mutation would hand the hub back to the workload the
epoch exists to fence): the backup completes and RETURNS CONTROL to the
epoch's owning destructive command; the daemon restarts only when the
orchestrator releases the epoch (0.7). Scope per run: `pg_dump -Fc` plus a separate
`pg_dumpall --globals-only` (pg_dump has no such flag) — the globals
dump is replayed into the isolated scratch PostgreSQL and roles/ACLs
verified, since producing the archive alone proves nothing about
restorability (review finding); stop-consistent
volume tarballs of `gobby-postgres`, `services-qdrant-1`,
`services-falkordb-1` (`gobby-postgres-test-1` excluded — disposable
pytest scratch) with clean service stop and restart in a `finally`;
Qdrant collection snapshots (snapshot API) restored into a scratch
collection as their check; a FalkorDB `BGSAVE` RDB copy loaded in a
scratch container as its check; free-space preflight; 0700 directory /
0600 artifacts; secret-free manifest; the 5.1 allow-audit rotating log
surface joins the inventory (files + checksums + extraction check) once
it exists (Codex review). Destination
`~/.gobby/backups/hub/<timestamp>/`, encrypted transfer when copied to
the new hub PC. The manifest is a named, versioned cross-language
contract — `gobby-hub-backup-manifest` schema v2, JSON-schema'd in
`_manifest.py` — because its consumers span languages: the Python
producer here and gcore's Rust gated-apply reader (4.1) both validate
against shared compatibility fixtures (Codex F12). When the run executes
inside an open maintenance epoch (0.7), the manifest records the epoch
id, binding the backup to the destructive window it covers — and under
an epoch that binding is REQUIRED authorization (Codex round 7): a
destructive step consuming this manifest verifies
`manifest.epoch_id == active_epoch.id` AND that the manifest's recorded
`backup_starting_head` exactly matches the target DB's current schema
head at apply time, so a recent standalone backup — or an epoch-bound
backup taken before an intervening head change — never authorizes the
batch. Freshness
is a checked invariant, not transcript
evidence (Codex review): the manifest carries what 0.5 needs to verify
max age (default 24h) and target-DB stable-identity match at apply time;
5.2 re-runs the backup immediately before purging.

**Acceptance:**
- 0.4.1 - `gobby hub-backup` covers PG (`-Fc` + `pg_dumpall
  --globals-only`) + Qdrant + FalkorDB + volume tarballs; daemon stopped
  first and restarted last; services restart even on failure; v2
  manifest with distinct
  `archive_verified`/`restore_verified` states, fingerprint, checksums,
  row-count probes; allow-audit log files enter the inventory once 5.1
  lands. file: `src/gobby/cli/hub_backup/cli.py`.
- 0.4.2 - Initial full backup completed with `restore_verified` earned
  for all three stores via scratch restores, incl. a globals replay with
  role/ACL verification. behavior: "backup manifest + restore checks" in
  session transcript.
- 0.4.3 - Freshness/identity machine-checked: the gate refuses a manifest
  older than max age, lacking `restore_verified`, or
  fingerprint-mismatched. test: `tests/cli/` hub-backup focused run.

### 0.5 Destructive-migration gate in the runner [category: code] (depends: 0.4, 0.7)
`kind: deliverable`

Targets:
- `src/gobby/storage/migrations.py::*` — scope-reason: destructive-marker directive, halt-before-destructive gate, contiguity guard, and batch resume land across the runner
- `src/gobby/cli/schema.py`
- `tests/storage/test_migration_contract.py::*` — scope-reason: new destructive-gate, marker-audit, and batch-resume contract tests

`MigrationRunner.apply_pending` (`storage/migrations.py:122-150`) applies
every pending file unattended — from daemon boot
(`runner_init/helpers.py:99`) and from every `runtime_hub_database` entry
point (`storage/hub/runtime.py:20` defaults `apply_migrations=True`, ~25
CLI paths); nothing can stop a destructive migration that has landed on
disk (Codex review, blocker). Add a `-- gobby:destructive` marker
directive (dual to the existing `-- gobby:non-transactional`). Default
application applies the pending prefix and halts fatally BEFORE the first
pending destructive-marked migration, with an actionable error naming the
gated command. The explicit path — `gobby schema apply --destructive` —
loads the newest 0.4 manifest and requires `restore_verified`, max age ≤
24h (configurable), a target-DB stable-identity match,
`manifest.epoch_id` equal to the open epoch's id, and an exact match
between the manifest's `backup_starting_head` and the target DB's
current schema head before applying (Codex round 7 — freshness and
identity alone would accept a standalone backup that does not cover the
batch's window). Bookkeeping-first (review finding, blocker — a local
last-completed pointer cannot survive a crash after a migration's DB
commit but before the receipt write, and version-only rows let another
runner apply different bytes under an already-recorded version: the 346
failure class): migration 354 — the chain's first slot, a
non-destructive precursor the old runner auto-applies, shipped by 0.7 —
adds nullable `filename`/`checksum` columns to `schema_migrations`
(3.1's columns, pulled ahead of the destructive chain) plus the
`maintenance_epochs`/`destructive_batches` ledger tables; from 354
onward the runner records filename and checksum inside each migration's
own transaction (bookkeeping already commits atomically with the
migration, `migrations.py:122-150`).
Resumability in authoritative shared storage (Codex F5 — a local intent
file dies with the initiating machine, leaving no way for another
operator to prove or resume a partially committed batch): before its
first mutation the gated apply commits an immutable destructive-batch
intent row to `destructive_batches` — batch identity, owning
maintenance-epoch id, backup-manifest sha256, and the ordered migration
filenames/checksums it will apply; completed progress is derived from
the DB-attested filename/checksum rows, never from a local pointer, so
batch state lives entirely in the hub. The
whole batch runs inside an open maintenance epoch (0.7) — the gated
apply refuses to start without one — and under one continuously held
advisory lock (the
existing schema-scoped lock); resume requires the applied rows to be an
exact (version, filename, checksum) prefix of the intent row, and works
from any machine that can reach the hub. Contiguity
guard (review finding, blocker — `_discover_migrations` sorts present
files and accepts gaps, `migrations.py:218-248`, so a parallel leaf's
higher slot could apply first): default application refuses a pending
set that is not contiguous with the applied head; a missing
intermediate slot halts with an actionable error. Proved empty-state
path: a schema whose baseline
was applied in this same run (fresh install, pytest `gobby_test_*`
fixtures — `_classify_baseline_state` already distinguishes it) skips
the gate, so fresh builds and CI never halt. Marker audit: every
destructive slot in the allocation manifest (execution notes) carries
the marker; the contract test scans migration files for destructive
statements (DROP TABLE/COLUMN/INDEX/SCHEMA/TYPE, ALTER TABLE … DROP
CONSTRAINT, TRUNCATE, data-deleting DML — including statements inside DO
blocks; Codex review) and fails if any lacks it. Ports to gcore in P4
(4.1) — keep it dependency-light.

**Acceptance:**
- 0.5.1 - Boot/CLI halts before a pending destructive migration; fresh-
  schema and pytest paths unaffected. test:
  `tests/storage/test_migration_contract.py`.
- 0.5.2 - Gated apply verifies `restore_verified` + freshness + stable
  identity + epoch binding (`manifest.epoch_id` = open epoch) + exact
  pre-batch schema-head match, and refuses each failure mode
  individually. test: `tests/storage/` gate focused run.
- 0.5.3 - Marker audit: destructive SQL without the marker fails the
  contract suite, incl. TRUNCATE/DROP CONSTRAINT/DO-block cases. test:
  `tests/storage/test_migration_contract.py`.
- 0.5.4 - Interrupted destructive batch resumes from DB-attested
  bookkeeping after every committed migration — including a crash after
  commit but before any receipt write, and including resume from a
  different machine reading only hub state; a prefix mismatch or a
  different-bytes-same-version runner refuses loudly. test:
  `tests/storage/` batch-resume focused run.
- 0.5.5 - The contiguity guard halts on a gapped pending chain; the
  gated apply refuses to run without an open maintenance epoch. test:
  `tests/storage/test_migration_contract.py`.

### 0.6 Amend the M0 artifact to UUID-native machine scoping [category: config]
`kind: deliverable`

Targets:
- `.gobby/plans/m0-shared-datastores-bridge.md`

The canonical M0 contract still specifies `343_machine_scope.sql` with
`machine_id TEXT NOT NULL` backfilled from `sessions.machine_id`
(`m0-shared-datastores-bridge.md:169-199`, verified) — contradicting this
plan's identity redesign. The draft previously assigned the amendment to
2.19, which sequences it after the work that depends on it (Codex F3,
blocker), and round 7 sharpened the timing further: an implementation
leaf is still too late, because expansion itself consumes the contract.
Resolution — the amendment executes DURING THE PLANNING SESSION: in the
post-approval sequence, immediately after this artifact is written and
validated and BEFORE either plan is registered or expanded (Execution
notes carry the step). This section is the amendment's spec and stays in
the DAG as the verification gate — at execution time 0.6 verifies the
amended artifact is the registered one and re-amends only if drift is
found; 2.18 and 2.19 both carry `depends: 0.6`. The amendment: machine-scoping
columns become `machine_id UUID REFERENCES machines(id)`, the backfill
joins the UUID-keyed sessions column, M0's migration slots reallocate to
the post-364 range (365+; 364 is 5.4's reserved BM25-disposition slot)
under the serialized allocator, and the amended artifact re-validates
(`uv run gobby plans validate`) and re-registers. 2.19 verifies this
prerequisite instead of performing it; 3.0 gates P3 on the amended M0's
migrations being applied.

**Acceptance:**

- 0.6.1 - M0 artifact specifies UUID-native machine scoping with post-364
  slots; no `machine_id TEXT` remains in its spec. file:
  `.gobby/plans/m0-shared-datastores-bridge.md`.
- 0.6.2 - Amended artifact validates and re-registers. behavior:
  "validate + register output" in session transcript.

### 0.7 Shared maintenance epoch: DB-enforced fence, orchestrator, ledgers [category: code] (depends: 0.4)
`kind: deliverable`

Targets:
- `src/gobby/storage/migrations/354_migration_bookkeeping.sql`
- `src/gobby/storage/maintenance_epoch.py`
- `src/gobby/cli/hub_maintenance.py`
- `src/gobby/storage/hub/runtime.py::*` — scope-reason: every runtime hub-database entry point gains the courtesy epoch admission diagnostic
- `src/gobby/runner_init/helpers.py::*` — scope-reason: daemon boot surfaces the courtesy epoch error instead of a raw connection failure
- `src/gobby/cli/hub_backup/cli.py`
- `tests/storage/test_maintenance_epoch.py`

Hub-wide quiescence is currently observed, not enforced (Codex F4,
blocker): `gobby hub-backup` stops the LOCAL daemon, but a remote daemon
can reconnect between the backup and the purge/flatten it gates, making
the backup stale the moment it is trusted. Migration 354 (the chain's
first slot, a non-destructive precursor the old runner auto-applies)
ships the bookkeeping columns (0.5) plus two ledger tables:
`maintenance_epochs` (id, opened_at, opened_by, scope note, released_at,
released_by_command) and `destructive_batches` (0.5's intent rows plus
per-target completion receipts for 5.2/5.3).

Enforcement lives in the database, at the connection boundary (Codex
round 7 — a Python-side admission check is a client-side fence: gcode
and gwiki resolve the DSN and connect from Rust,
`crates/gcode/src/db/resolution.rs:22` and
`crates/gwiki/src/support/env.rs:15` verified, and an old daemon binary
predating the check never runs it). Migration 354 also installs a PG18
LOGIN event trigger in the hub database: its function is a no-op while
no `maintenance_epochs` row is open (one indexed SELECT per login);
while a row is open, every new connection must present the epoch token
as a startup GUC (`options='-c gobby.maintenance_epoch=<epoch-id>'`) or
the login is rejected with an actionable error naming the epoch and the
orchestrator. That fences EVERY client — Python entry points, gcode/
gwiki Rust ingress, and pre-protocol daemon binaries — with zero client
wiring; Qdrant/FalkorDB ingress is fenced transitively because their
only writers (daemon services and owner CLIs) are hub-DB clients first.
The token IS the epoch row id: it authorizes deliberate maintenance
participation (the fence's threat model is unattended reconnection, not
adversaries) and reaches child processes via child-only env. The
documented repair escape is a superuser session with
`-c event_triggers=off`. Epoch open protocol: insert and commit the
epoch row (fence immediately active for all new logins), THEN
`pg_terminate_backend` every other application connection, then verify
`pg_stat_activity` is clean — enforcement before observation, and
termination (not observation) handles connections that predate the
fence (round 7). The Python admission checks in `runtime.py`/boot
remain as courtesy diagnostics only — a fast, actionable error before
the DB rejection — never the enforcement.

One executable orchestrator owns the lifecycle (Codex round 7 — the
draft referenced opening, resuming, and aborting epochs with no command
owning them): `gobby hub-maintenance` (`src/gobby/cli/hub_maintenance.py`).
`hub-maintenance run <campaign>` for campaign ∈ {schema-apply, purge,
reconcile, identity-cutover, flatten} owns the full sequence: begin
(open protocol above) → epoch-bound fresh backup (0.4, restart
suppressed) → the campaign's mutation/apply steps → verify → release
(the daemon restarts here and only here). `status` reports the open
epoch and its batch state from hub tables; `resume` re-enters an
interrupted campaign from any machine using only hub state; `abort` is
evidence-gated — explicit operator confirmation plus a recorded
disposition of partial state. Destructive commands refuse to run
outside an orchestrator-owned epoch. This also dissolves the
boot-vs-fence contradiction round 7 flagged in 2.18: the identity
cutover is the identity-cutover campaign, not a daemon-boot step — boot
keeps only non-epoch identity work (fresh-identity registration and
tombstone re-key).

Per-target receipts carry a component-level state machine (Codex round
7 — external-store deletions and PG receipt commits cannot be atomic):
pending → applied → verified, with the pending row written before the
component mutation. A crash after a Qdrant/FalkorDB deletion but before
its receipt leaves `pending`; resume re-derives the truth from that
component's exact idempotent postcondition (target collection/graph
absent, delete predicate returns zero rows), completes the receipt, and
continues — a `pending` receipt whose postcondition does not hold
re-runs its component instead of failing the resume.

Epochs stay open across backup AND mutation: released only by the
owning orchestrator run that finishes the batch (or an explicit
evidence-gated abort), never by timeout — a crashed batch leaves the
epoch open, which is correct: the hub stays fenced until an operator
resumes or aborts. Consumers: 0.5's gated apply, 5.2's purge, 5.3's
reconcile apply, 2.18's cutover, and 3.2's flatten cutover all run as
orchestrator campaigns; `gobby hub-backup` accepts `--epoch <id>` and
records it in the manifest (0.4).

**Acceptance:**

- 0.7.1 - Migration 354 ships bookkeeping columns + both ledger tables +
  the login-fence trigger; auto-applies as a non-destructive precursor.
  file: `src/gobby/storage/migrations/354_migration_bookkeeping.sql`.
- 0.7.2 - With an epoch open, a tokenless login is rejected BY THE
  DATABASE — pinned for a Python client, a Rust (gcode/gwiki-style)
  connection, and a bare psycopg connection simulating a pre-protocol
  daemon; with no epoch open, logins are unaffected; Python entry points
  surface the courtesy diagnostic. test:
  `tests/storage/test_maintenance_epoch.py`.
- 0.7.3 - Epoch open terminates pre-existing foreign connections and
  verifies clean `pg_stat_activity`; release happens only via the owning
  orchestrator run or evidence-gated abort. test:
  `tests/storage/test_maintenance_epoch.py`.
- 0.7.4 - `gobby hub-maintenance` run/status/resume/abort lifecycle
  works end-to-end; resume re-enters an interrupted campaign from hub
  state alone (different-machine simulation); destructive commands
  refuse to run outside an orchestrator-owned epoch. test:
  `tests/cli/` hub-maintenance focused run.
- 0.7.5 - Receipt state machine: a crash injected between an
  external-store deletion and its receipt resumes via the component's
  idempotent postcondition; a pending receipt whose postcondition does
  not hold re-runs its component. test:
  `tests/storage/test_maintenance_epoch.py`.

### 0.8 Decompose runner_maintenance.py into a package [category: refactor]
`kind: deliverable`

Targets:
- `src/gobby/runner_maintenance.py::*` — scope-reason: whole module decomposed into a package; file deleted
- `src/gobby/runner_maintenance/__init__.py`
- `src/gobby/runner_maintenance/binaries.py`
- `src/gobby/runner_maintenance/messaging.py`
- `src/gobby/runner_maintenance/telemetry_loops.py`
- `src/gobby/runner_maintenance/storage_hygiene.py`
- `src/gobby/runner_maintenance/isolation.py`
- `src/gobby/runner_maintenance/lifecycle.py`

`runner_maintenance.py` sits at 998 lines (verified 2026-07-30), and
2.4, 5.1, and 4.4 all add or change behavior there — the first of them
to land would breach the 1,000-line ceiling mid-task (Codex round 7).
Split it FIRST, as a pure mechanical decomposition (load
`decompose-monolith`; no behavior change): the module is a flat
collection of independent maintenance loops plus shutdown machinery
(outline verified), grouping naturally into `binaries` (bin-freshness
loop), `messaging` (hook-inbox drain, zombie messages, comms cleanup),
`telemetry_loops` (span cleanup, unmodeled-observation cleanup, metric
snapshots, recall drift), `storage_hygiene` (skills purge, chat
attachments, approval timeouts — later joined by 2.4's test-schema
sweep and 5.1's session-variable expiry), `isolation` (expired
isolation cleanup, tmux window repair), and `lifecycle` (shutdown
source, signal handlers, pid cleanup, vector rebuild). `__init__.py`
re-exports the loop entry points so `runner_init` consumers keep their
import paths. Final grouping is the skill's call; every resulting
module lands well under the ceiling. Later leaves (2.4, 5.1, 4.4)
target the extracted modules, and each carries `depends: 0.8`.
Correction to the round-7 finding, recorded: 0.7 does NOT touch this
module — its admission diagnostics live in `storage/hub/runtime.py` and
`runner_init/helpers.py`, and the maintenance loops are quiesced during
an epoch by the daemon being stopped, so no epoch code lands here.

**Acceptance:**
- 0.8.1 - Package replaces the module; every file < 1,000 lines; import
  paths preserved via `__init__` re-exports; no behavior change.
  behavior: "line counts + import check" in session transcript.
- 0.8.2 - Daemon boots and maintenance loops register unchanged. test:
  `tests/` runner-maintenance focused run.

---

## P1: Dead schema + dead code removal
`kind: framing`

**Goal**: Every verifiably dead table, column, file, branch, dep, and template
removed with per-item evidence; live features enumerated as kept. Every
removal deliverable in this phase carries the global evidence standard
inside its own section (self-contained for expansion): verification
evidence per item plus an explicit kept-adjacent ledger, with pg_stat
counters alone insufficient.

### 2.1 Rewrite claims reader off workflow_states [category: code]
`kind: deliverable`

Targets:
- `src/gobby/cli/tasks/_utils/claims.py::get_claimed_task_owners`
- `src/gobby/mcp_proxy/tools/apply_persona.py::*` — scope-reason: last workflow_states writer removed

`get_claimed_task_owners` (`claims.py:13-90`) JOINs frozen March-2026
`workflow_states` data. Authoritative claim store is
`tasks.claimed_by_session_id` (written `storage/tasks/_creation.py:33`, swept by
`storage/tasks/_automation.py:192-246`). Rewrite onto `SELECT id,
claimed_by_session_id FROM tasks WHERE claimed_by_session_id IS NOT NULL`
joined to active sessions. Remove the last `workflow_states` writer of the
`session_task` key (`apply_persona.py:276` — verify current target and remove
the write with it). Callers unchanged: `cli/tasks/_crud_listing.py:132,184`.

**Acceptance:**
- 2.1.1 - Claims resolve from tasks table; no workflow_states reference remains
  in claims path. symbol: `get_claimed_task_owners`.
- 2.1.2 - CLI listing still marks claimed tasks. test:
  `tests/cli/tasks/` focused run.

### 2.2 Drop dead tables [category: code] (depends: 2.1, 0.3, 0.4, 0.5)
`kind: deliverable`

Dropped set: `savings_ledger`, `session_memories`, `rule_overrides`,
`workflow_states`, `tool_embeddings`.

Targets:
- `src/gobby/storage/migrations/357_drop_dead_tables.sql`
- `src/gobby/workflows/engine/core.py::*` — scope-reason: rule_overrides read steps removed from the engine
- `tests/storage/test_migration_contract.py::*` — scope-reason: dropped-table fixtures updated
- `tests/storage/test_rule_overrides.py::*` — scope-reason: whole file deleted

Migration 357 (destructive-marked — 0.5's gated path) drops, each
re-verified by row count + code refs before drop:
- `savings_ledger` — populated relic: 94,544 rows verified live 2026-07-30
  (the audit's "0 rows" was a reset-stats artifact — `n_live_tup` 0 with
  19 MB on disk); zero src/crates references (savings moved to daemon/API
  reporting); rows archived by the 0.4 backup before the drop.
- `session_memories` + its 2 FKs — no writer ever existed; superseded by
  `memories.source_session_id` (1,525 rows); reads were FK-cascade scans.
- `rule_overrides` — no writer ever built; delete the read step:
  `WorkflowEngine._load_session_overrides` (`core.py:502-511`),
  `_apply_overrides` (`core.py:513-525`), both `offload` calls
  (`core.py:292-293`). Ends 666K wasted probes (one SELECT per rule eval).
- `workflow_states` — frozen relic (writes stopped 2026-03-02), reader
  rewritten in 2.1.
- `tool_embeddings` (+ moot `text_hash`) — identity sequence `last_value` NULL
  (never held a row, reset-proof); live feature uses the Qdrant collection.
Delete `tests/storage/test_rule_overrides.py`; update contract-test fixtures
(`test_migration_contract.py:537,580`, `tests/workflows/test_rule_engine.py:779,803,3372`,
`tests/agents/test_merge_orchestrator_contract.py:187`).
Kept-adjacent: `memories.source_session_id` path; Qdrant `tool_embeddings`
collection; all recall_* tables (exempt).

**Acceptance:**
- 2.2.1 - Migration 357 drops all five tables + FKs; dual-shape safe. file:
  `src/gobby/storage/migrations/357_drop_dead_tables.sql`.
- 2.2.2 - Rule-eval pipeline has no override probe. symbol:
  `WorkflowEngine.evaluate`.
- 2.2.3 - Row-count/sequence evidence for each table recorded pre-drop.
  behavior: "evidence block per table" in session transcript.
- 2.2.4 - Focused suites green. test: `tests/workflows/test_rule_engine.py`.

### 2.3 Drop dead columns + dead indexes [category: code] (depends: 2.2, 0.4, 0.5)
`kind: deliverable`

Targets:
- `src/gobby/storage/migrations/358_drop_dead_columns.sql`
- `tests/storage/test_migration_contract.py::*` — scope-reason: dropped-column/index fixtures updated

Migration 358 (destructive-marked — 0.5's gated path) drops the
strict-pass dead columns: `tasks.assignee` (zero
mentions across 441 referencing files), `task_artifacts.last_reviewed_plan_hash`
/`plan_review_attempts`/`qa_attempts`/`epic_qa_attempts`/`merge_attempts`,
`inter_session_messages.read_at`. (`workflow_states.*` and
`tool_embeddings.text_hash` are moot via 2.2.) Each column re-verified by
token-absence sweep at implementation time. Migration 358 also drops the 3
zero-scan secondary indexes on `token_events` (~84 MB of write amplification
on a high-volume table; names pinned at implementation via a
`pg_stat_user_indexes` re-check plus a query-shape sweep of `token_events`
readers — plain btrees, so `idx_scan` counters are trustworthy here, unlike
the ParadeDB case in 5.4).

**Acceptance:**
- 2.3.1 - Migration 358 ships with per-column and per-index verification
  evidence. file:
  `src/gobby/storage/migrations/358_drop_dead_columns.sql`.
- 2.3.2 - Contract tests updated. test: `tests/storage/test_migration_contract.py`.

### 2.4 gobby_test_* schema hygiene: leaked-schema drops + leased startup sweep [category: code] (depends: 0.8)
`kind: deliverable`

Targets:
- `src/gobby/runner_maintenance/storage_hygiene.py`
- `tests/fixtures/postgres.py::*` — scope-reason: lease acquisition at schema creation + name-contract validation
- `tests/storage/test_manager_surface_parity.py::*` — scope-reason: hardcoded never-dropped schema fixture replaced

Drop the 3 leaked empty pytest schemas (verified empty test artifacts —
authorized exception to no-data-deletion). Add a daemon-startup sweep reusing
the `_cleanup_orphaned_schemas` logic (`tests/fixtures/postgres.py:86-123`) —
today it only runs when a new pytest session starts, so SIGKILL leaks
accumulate. Age is treated as a hint, never as abandonment proof (Codex
F8, blocker — a long-running or paused test would be dropped mid-flight
by an age-only sweep): every schema creator acquires a schema-specific
advisory lease (session-scoped PG advisory lock keyed on the schema
name) and holds it for the fixture's lifetime; the sweeper may drop a
schema only after try-acquiring that same lease AND rechecking
eligibility (age + 6-part name contract) while holding it. A live test
holds its lease, so the try-acquire fails and its schema survives
regardless of age; a SIGKILL'd test's lease died with its connection, so
the sweep proceeds. Enforce the 6-part name contract at creation time
(`postgres.py:216` interpolates `worker_label` unvalidated → unsweepable
schemas). Fix `test_manager_surface_parity.py:41-60`'s hardcoded, never-dropped
`gobby_test_schema`. Schema-authority note (review finding): this sweep
issues production `DROP SCHEMA` from Python — an explicit kept-until-P4
surface; 4.4 re-homes it (same lease semantics) behind
`gdaemon schema sweep-test-schemas` so
P4's zero-persistent-Python-DDL claim holds.

**Acceptance:**
- 2.4.1 - Startup sweep registered in daemon maintenance; drops aged
  `gobby_test_*` schemas only under an acquired lease + recheck; a held
  lease (live test) blocks the drop, pinned by test. file:
  `src/gobby/runner_maintenance/storage_hygiene.py`.
- 2.4.2 - Creation-time validation rejects labels breaking the 6-part
  contract; fixtures acquire the lease at creation. test:
  `tests/fixtures/test_postgres_safety.py`.
- 2.4.3 - Hub has zero leaked schemas post-cleanup. behavior: "psql schema list
  clean" in session transcript.

### 2.5 Delete dead Python files [category: code]
`kind: deliverable`

Targets:
- `src/gobby/utils/mathutil2.py::*` — scope-reason: whole file deleted
- `src/gobby/cli/pipelines_runtime.py::*` — scope-reason: whole file deleted
- `src/gobby/code_index/prune_storage.py::*` — scope-reason: whole file deleted
- `src/gobby/servers/routes/stage_routes.py::*` — scope-reason: whole shim deleted
- `src/gobby/servers/routes/stages.py::*` — scope-reason: zero-route module-level router global removed; endpoints stay
- `src/gobby/workflows/task_actions.py::*` — scope-reason: whole file deleted
- `src/gobby/workflows/summary_actions.py::*` — scope-reason: whole file deleted
- `src/gobby/postgres_pgsearch_assets.py::*` — scope-reason: whole file deleted
- `src/gobby/skills/injector.py::*` — scope-reason: whole file deleted
- `src/gobby/plans/convergence_regression.py`
- `docs/architecture/source-tree.md`

Delete the 9 import-graph-verified dead files (~760 lines) plus their
shim-testing test files (`tests/utils/test_mathutil2.py`,
`tests/workflows/test_task_actions.py`, `tests/workflows/test_summary_actions.py`,
`tests/skills/test_injector.py`, `tests/plans/test_convergence_regression.py`).
**Audit correction:** `servers/routes/stages.py` is LIVE (mounted at
`servers/_app_routes.py:49,71`, 6 endpoints) — it stays; remove only its
zero-route module-level `router` global (`stages.py:78`) and the
`stage_routes.py` shim. Update `docs/architecture/source-tree.md:48` (drops
`postgres_pgsearch_assets`). Kept-adjacent: `stages.py` router factory; the
live `_sync_postgres_pgsearch_assets` in `cli/installers/postgres.py:225`.
Evidence standard (per the global rule, restated for expansion): each
deleted file ships import-graph evidence in the session transcript;
pg_stat/usage counters alone are insufficient.

**Acceptance:**
- 2.5.1 - 9 files + shim tests deleted; import graph clean (`uv run mypy src/`
  + daemon boot). behavior: "daemon starts clean" in session transcript.
- 2.5.2 - stages.py endpoints still serve. test: `tests/servers/routes/test_stage_routes.py`.

### 2.6 Remove postgres-activate ritual + SQLite residue [category: code] (depends: 0.4, 0.5, 2.3)
`kind: deliverable`

Targets:
- `src/gobby/cli/postgres.py::*` — scope-reason: activate cutover ritual (~230 lines) removed
- `src/gobby/config/bootstrap.py::*` — scope-reason: database_path + hub_backend keys removed from the schema
- `src/gobby/search/__init__.py::*` — scope-reason: FTS5 alias indirection removed
- `src/gobby/search/keyword.py::*` — scope-reason: tasks_fts alias mapping removed
- `src/gobby/storage/migrations/359_drop_pgaudit_probe.sql`
- `data/postgres-pgsearch/initdb.d/02-pgaudit.sql`
- `crates/gcore/src/bootstrap.rs::*` — scope-reason: database_url no longer gated on hub_backend
- `crates/gcode/src/db/resolution.rs::*` — scope-reason: hard-fail path follows the trimmed bootstrap
- `CLAUDE.md`

Remove: `gobby postgres activate` cutover ritual (~230 lines incl. pgaudit
probes, cutover tickets, flags that only raise, references to nonexistent
`docs/runbooks/`); `_pgaudit_probe` sentinel table (migration 359, destructive-marked —
0.5's gated path — plus its container seeding in
`data/postgres-pgsearch/initdb.d/02-pgaudit.sql:5` so fresh containers
stop recreating it);
dead `bootstrap.yaml` `database_path` key (read by nothing — Rust confirmed:
zero hits); `hub_backend: Literal["postgres"]` single-valued selector — this is
a cross-surface removal: Python stops writing/validating the key AND
`crates/gcore/src/bootstrap.rs:145-160` stops gating `database_url` on it
(`crates/gcode/src/db/resolution.rs:175-192` hard-fail updated); FTS5 alias
indirection in `search/` (`tasks_fts` → real table mapping); the 3
`HubDatabase | HubDatabase` degenerate unions. Fix CLAUDE.md's stale
`migrate-from-sqlite` mention (command does not exist). Rebuild + reinstall
gcode/ghook/gwiki after the Rust edit. Evidence standard (restated for
expansion): each removal ships zero-reference evidence + a kept-adjacent
note in the session transcript; counters alone are insufficient.

**Acceptance:**
- 2.6.1 - `gobby postgres activate` gone; `_pgaudit_probe` dropped. file:
  `src/gobby/cli/postgres.py`.
- 2.6.2 - bootstrap.yaml schema has neither `database_path` nor `hub_backend`;
  Rust reads `database_url` directly. symbol: `parse_hub_database_bootstrap`.
- 2.6.3 - gcode DSN resolution works against the trimmed bootstrap. behavior:
  "gcode search succeeds post-reinstall" in session transcript.
- 2.6.4 - Focused pytest + cargo check green. test: `tests/config/` focused run.

### 2.7 Remove no-op CLI/API surfaces [category: code]
`kind: deliverable`

Targets:
- `src/gobby/cli/build.py::*` — scope-reason: unregistered command removal sweeps the click group wiring
- `src/gobby/cli/export_import.py::*` — scope-reason: whole dead command group deleted
- `src/gobby/cli/__init__.py::*` — scope-reason: export/import command registration removed from the CLI root
- `src/gobby/cli/tasks/crud.py::*` — scope-reason: discarded flag removal touches the close command surface
- `src/gobby/servers/routes/code_index.py::rebuild_graph`
- `src/gobby/mcp_proxy/tools/sessions/_handoff.py::*` — scope-reason: ignored full param removed from the handoff tool contract
- `src/gobby/hooks/claude_code.py`

Remove: unregistered `build stop`/`build resume` click commands
(`cli/build.py:422-435`); the dead `export_import` group
(`cli/export_import.py` + its `cli/__init__.py` registration); discarded
`tasks close --skip-validation/--force` flags (`_ = (skip_validation,
force)`); the ignored MCP handoff `full` param
(`mcp_proxy/tools/sessions/_handoff.py`); the deprecated-and-ignored HTTP
`rebuild_graph?limit=` (`servers/routes/code_index.py:403-412`);
`claude_code.py` docstring documenting nonexistent `set_legacy_mode`.
Exact sites re-verified at implementation. Evidence standard applies
per surface: removal ships with its zero-consumer evidence and a
kept-adjacent note; pg_stat/usage counters alone are insufficient.

**Acceptance:**
- 2.7.1 - All six surfaces removed or corrected; CLI/API help output clean.
  file: `src/gobby/cli/build.py`.
- 2.7.2 - Focused route/CLI tests green. test: `tests/cli/` focused run.

### 2.8 Remove dead config fields incl. CodeIndexConfig disposition [category: code]
`kind: deliverable`

Targets:
- `src/gobby/config/app.py::*` — scope-reason: dead config models + accessors removed across the app config
- `src/gobby/config/build.py::*` — scope-reason: 7 parsed-then-discarded fields removed
- `src/gobby/config/code_index.py::*` — scope-reason: six dead fields removed per the disposition
- `src/gobby/config/persistence.py::*` — scope-reason: removed keys enter drop_removed_keys for the last run
- `web/src/components/settings/sections/RuntimeInfrastructureSection.tsx::*` — scope-reason: settings form drops the removed fields in lockstep

Remove the Part 2 enumerated dead sets: entire `ContextInjectionConfig`; the 7
parsed-validated-discarded `BuildConfig` fields; `TaskExpansionConfig` research
knob set; `CommunicationsConfig.inbound_enabled`/`outbound_enabled`; the 8
`DaemonConfig.get_*_config()` accessors alive only via their own tests.
CodeIndexConfig disposition is complete (full-field trace 2026-07-31; no
implementation-time remainder — Codex review). Removed, six dead fields:
`exclude_patterns` + `max_file_size_bytes` + `content_extensions` (zero
consumers; gcode decides via its own hardcoded `DEFAULT_EXCLUDES`,
`crates/gcode/src/index/indexer/util.rs:9-26`, already drifted from the
Python list); `auto_index_on_commit` (no hook or indexer read site);
`languages` (nothing passes a language list to gcode);
`qdrant_collection_prefix` (validation-only mirror of
`databases.qdrant.collection_prefix` — runtime reads the other field,
`runner_init/services.py:173`; this one can only reject configs).
`auto_index_on_commit` and `languages` are editable in the web settings
UI — `RuntimeInfrastructureSection.tsx:50,65,298,395` and its test update
in lockstep. Kept, everything else, consumer-traced: `enabled`,
`maintenance_*`, `missing_root_purge_observations`,
`nightly_full_reindex_*`, `embedding_enabled`, `graph_enabled`,
`sync_worker_*` (incl. the breaker set), and the `symbol_summary` nested
model (all four fields plus inherited `profile`/`candidates`).
`extra="forbid"` means every removed field also enters `drop_removed_keys`
for its one last run. Also delete stale config_store rows for removed keys
via the existing removed-key-drop machinery. Rust reads none of
`code_index.*` (verified).

**Acceptance:**
- 2.8.1 - Enumerated sets removed; config loads; flatten_config emits no
  orphan keys. symbol: `DaemonConfig`.
- 2.8.2 - Six dead CodeIndexConfig fields removed; web settings form updated
  in lockstep; kept-field consumer trace recorded. behavior: "per-field
  disposition" in session transcript.
- 2.8.3 - Focused config tests green. test: `tests/config/` focused run.

### 2.9 Remove legacy config-chain migration paths [category: code] (depends: 2.8)
`kind: deliverable`

Targets:
- `src/gobby/config/_loading.py::*` — scope-reason: legacy config migrators removed
- `src/gobby/config/wiki_migration.py::*` — scope-reason: whole file deleted
- `src/gobby/config/feature_candidate_defaults.py::*` — scope-reason: stale-default deleter removed
- `src/gobby/config/build.py::*` — scope-reason: _merge_legacy_cap removed
- `src/gobby/config/code_index.py::*` — scope-reason: drop_removed_keys retired after its last run

Split from the original catch-all legacy deliverable into 2.9/2.21/2.22/
2.23 (Codex review: bounded review + clean task closure). Remove, each
behind a hub-state verification proving the migration already ran or has
nothing to migrate: `_migrate_legacy_config`, `wiki_migration.py` (whole
file), `_drop_legacy_embedding_config_store_keys`,
`_migrate_code_index_symbol_summary_config_store_keys`,
`_drop_removed_config_store_keys` legacy sets,
`_migrate_default_ui_mode_config_store_row`,
`delete_stale_default_feature_candidate_rows`,
`CodeIndexConfig.drop_removed_keys`, `_merge_legacy_cap`. (2.8 interplay:
the removed-key-drop machinery is used once more in 2.8, then retired.)
Evidence standard (restated for expansion — this section is
self-contained): every removal ships its hub-state proof (config_store
query showing the migration already ran or has nothing to migrate) in
the session transcript; counters alone are insufficient. Kept-adjacent:
the live config load chain itself (`_loading.py` load/merge/validate
path — only the legacy migrators die); the removed-key-drop machinery
until 2.8's final run; the wiki config section and its consumers (only
`wiki_migration.py` dies); `feature_candidate_defaults.py`'s live
default-seeding path (only the stale-row deleter dies).

**Acceptance:**
- 2.9.1 - 9 config-chain paths removed with per-item hub-state evidence.
  behavior: "per-item evidence ledger" in session transcript.
- 2.9.2 - Config loads; daemon boots clean. test: `tests/config/` focused
  run.
- 2.9.3 - Kept-adjacent ledger recorded (load chain, wiki section,
  live default seeding). behavior: "2.9 kept ledger" in session
  transcript.

### 2.10 Consolidate duplicate utilities, _sanitize_url first [category: code] (depends: 2.5)
`kind: deliverable`

Targets:
- `src/gobby/utils/url_sanitize.py`
- `src/gobby/utils/http_retry.py`
- `src/gobby/clones/git.py::*` — scope-reason: leaking _sanitize_url variant replaced by the shared util
- `src/gobby/workflows/webhook_executor.py::*` — scope-reason: canonical sanitize semantics extracted from this file before 2.11 deletes it
- `src/gobby/mcp_proxy/manager.py::*` — scope-reason: duplicate truncate_tool_brief + legacy aliases removed; consumers re-point at server_registry
- `src/gobby/cli/_build_daemon.py::_daemon_error_message`
- `src/gobby/cli/cron.py::_daemon_error_message`
- `src/gobby/cli/pipelines.py::*` — scope-reason: duplicate _daemon_error_message and get_project_path consolidated onto the canonical implementations
- `src/gobby/communications/adapters/base.py::*` — scope-reason: Retry-After header parsing re-based onto the shared helper
- `src/gobby/integrations/linear_graphql.py::*` — scope-reason: Retry-After header parsing re-based onto the shared helper
- `src/gobby/mcp_proxy/importer.py::*` — scope-reason: Retry-After header parsing re-based onto the shared helper

`_sanitize_url` exists twice with divergent redaction semantics — the
`clones/git.py:20-38` variant preserves query strings (leaks `?token=` into git
command logs) and mangles IPv6; the `webhook_executor.py:553-560` variant
strips query/fragment and re-brackets IPv6. Extract the webhook-variant
semantics into one shared util; both call sites consume it (behavior change for
clones logging is the intended fix). Then the remaining families, each
with its sites named (Codex round 7 — the families were prose-only):
`truncate_tool_brief` — canonical stays
`mcp_proxy/client_manager/server_registry.py:21`; the `manager.py:50`
duplicate and the `_truncate_tool_brief` aliases go, consumers re-point.
`_daemon_error_message` ×4 — canonical `cli/_build_daemon.py:294`;
`cli/cron.py:53` + `cli/pipelines.py:94` re-point (the fourth,
`cli/pipelines_runtime.py:44`, is deleted whole by 2.5 — the
`depends: 2.5` edge is the overlap ordering round 7 asked for).
`get_project_path` — `cli/pipelines.py:44` and the 2.5-deleted
`cli/pipelines_runtime.py:19` are the duplicate pair; consolidate onto
one CLI-facing helper. Retry-After parsing ×3 — `communications/
adapters/base.py:290`, `integrations/linear_graphql.py:470`,
`mcp_proxy/importer.py:420` each hand-parse the header; one shared
`utils/http_retry.py` helper (parse + clamp) replaces all three.
Kept-adjacent (recorded corrections):
`build/control_artifacts.py::get_project_path` is NOT a duplicate — a
different function (resolves from DB by project id) — it stays;
`servers/routes/auth.py:115` SETS the Retry-After header (producer, not
parser) — it stays.

**Acceptance:**
- 2.10.1 - Single `sanitize_url` with query/fragment stripping + IPv6
  re-bracketing; both call sites migrated. symbol: `sanitize_url`.
- 2.10.2 - All four named families each resolve to one canonical
  implementation with duplicates deleted; per-family site evidence +
  the kept-adjacent corrections recorded. behavior: "duplicate sweep
  results" in session transcript.
- 2.10.3 - Focused tests green. test: `tests/clones/` focused run.

### 2.11 Consolidate webhook stacks onto httpx; drop aiohttp [category: code] (depends: 2.10)
`kind: deliverable`

Targets:
- `src/gobby/utils/webhook_transport.py`
- `src/gobby/workflows/webhook_executor.py::*` — scope-reason: whole file deleted
- `src/gobby/workflows/pipeline_webhooks.py::*` — scope-reason: consumer re-bases onto the shared transport
- `src/gobby/hooks/webhooks.py::*` — scope-reason: dispatcher send path re-bases onto the shared transport
- `pyproject.toml`
- `uv.lock`

Two full webhook stacks exist: httpx `WebhookDispatcher` (`hooks/webhooks.py:76`,
multiple consumers) and aiohttp `WebhookExecutor`
(`workflows/webhook_executor.py:95`, sole consumer `pipeline_webhooks.py:14,38`).
`webhook_executor.py` is the only aiohttp importer in src/gobby; the dep is
pinned `>=3.14.1` solely for its 2026 advisories. Design locked (behavior
inventory 2026-07-31; Codex review — the earlier either/or is resolved and
the HMAC claim corrected: neither stack signs requests, `import hmac`
appears only in inbound verifiers, so there is no signing behavior to
preserve and none is added). One shared httpx `WebhookTransport`
(`utils/webhook_transport.py`) carries the executor's transport semantics:
scheme/host validation; resolve-time non-global-address blocking +
pinned-DNS connection (rebinding defense, `webhook_executor.py:74-92,
497-516`) with `trust_env=False` on the restricted transport — httpx
honors `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` by default (verified:
`webhooks.py:105-110` sets no trust_env), and a proxy re-resolves the
original hostname, bypassing the validated pinned IP entirely (Codex
F10, blocker) — behind an `allow_private_addresses` policy flag; method
allowlist `{DELETE, GET, PATCH, POST, PUT}`; header name/CRLF validation;
redirects disabled; bounded response reads (caller-set cap); optional
capped exponential retry; typed diagnostics via 2.10's `sanitize_url`.
`pipeline_webhooks.py` consumes it directly — its used surface is exactly
`execute(url, method, headers, dict payload, timeout=30)` reading
`success`/`status_code`/`body`/`error`; the executor's
secrets-interpolation, template-engine, webhook-registry, and callback
arms have no production caller (constructed bare at
`pipeline_webhooks.py:38`) and are removed as recorded dead surface.
`WebhookDispatcher` re-bases its send path on the same transport with
`allow_private_addresses=True` — today it has zero SSRF/pinning protection
and an uncapped backoff doubler (`webhooks.py:334-337`); gaining the
pinning and the cap is the intended behavior change — while keeping its
policy layer intact: event filtering, fixed payload shape, env-var
expansion, fail-closed blocking semantics, 64 KiB response cap via the
transport's bound parameter. Delete `webhook_executor.py`; remove
`aiohttp` from dependencies (+ its advisory comment).

**Acceptance:**
- 2.11.1 - Pipeline webhooks deliver via the shared transport with unchanged
  payload behavior; SSRF-blocking + pinning tests green. test:
  `tests/workflows/` webhook-focused run.
- 2.11.2 - `aiohttp` absent from pyproject and `uv.lock`; zero imports remain.
  file: `pyproject.toml`.
- 2.11.3 - Dispatcher rides the same transport: fail-closed blocking
  semantics preserved, backoff capped, private addresses still allowed for
  local endpoints. test: `tests/hooks/` webhook-focused run.
- 2.11.4 - Transport edge semantics pinned by tests (Codex review): TLS
  SNI + certificate hostname verification against the ORIGINAL hostname
  when connecting via pinned IP; multi-address DNS answers all validated
  and pinned; response cap enforced mid-stream on chunked bodies; retries
  limited to idempotent methods (a non-idempotent request retries only
  when provably never sent); hostile `HTTP_PROXY`/`HTTPS_PROXY`/
  `ALL_PROXY` environment variables never route a pinned request through
  a proxy (trust_env=False pinned by test). test: `tests/utils/`
  webhook-transport focused run.

### 2.12 Remove memory-dream merge arm + _legacy fallbacks [category: code] (depends: 0.4, 0.5, 2.19)
`kind: deliverable`

Targets:
- `src/gobby/storage/migrations/363_dream_check_tighten.sql`
- `src/gobby/memory/dream/duplicates.py::*` — scope-reason: whole file deleted
- `src/gobby/memory/dream/planner.py::*` — scope-reason: merge-arm symbols removed per the verified manifest
- `src/gobby/memory/dream/models.py::*` — scope-reason: DuplicateGroup + merge/supersede action names removed
- `src/gobby/memory/dream/orchestrator.py::*` — scope-reason: duplicate_groups plumbing removed
- `src/gobby/memory/dream/apply.py::*` — scope-reason: merge/legacy arms removed; revert gains the fail-closed forfeiture check
- `src/gobby/memory/dream/storage_journal.py::*` — scope-reason: merge-only journal helpers removed
- `src/gobby/memory/dream/storage_runs.py::*` — scope-reason: revert_forfeited joins the run-status vocabulary and terminal-status set
- `src/gobby/memory/dream/storage_schema.py::*` — scope-reason: CHECK mirror updated in lockstep with migration 363

Feature is LIVE — removal exactly per the verified manifest, nothing more.
Verification (call-graph + runtime, completed during planning): `plan.py:23`
`_VALID_ACTIONS = {keep, delete, refresh, review, promote}` excludes
merge/supersede — `validate_dream_plan` degrades unknown actions to `keep`, so
the merge arm is unreachable even with populated `duplicate_groups`; sole
producer passes `duplicate_groups=[]` (`orchestrator.py:433`); zero
merge/supersede lines across ~225 MB of logs (Jun 12–Jul 30); `hasattr(store.db,
"transaction")` gates always true in production (HubDatabase protocol).
Remove merge arm (~300 lines): `duplicates.py` whole file; `DuplicateGroup` +
`to_prompt_dict` (`models.py:134-147`); `"merge"`,`"supersede"` from
`DreamActionName` (`models.py:11`); `_duplicate_merge_actions`,
`_same_project_duplicate_groups`, `_referenced_ids` (`planner.py:255-303`);
`duplicate_groups` param + scoping (`planner.py:40,60-64,99-100`;
`orchestrator.py:433`); `_merge`, `_supersede`, `_delete`,
`_restore_rows_for_failed_action` + dispatch branches
(`apply.py:441-445,497-500,693-903`); `transfer_crossrefs`,
`record_applied_snapshot` (`storage_journal.py:194-220,259-276`).
Remove legacy arm (~314 lines): `_revert_dream_run_legacy`,
`_apply_action_legacy`, `_soft_hide_legacy`, `_promote_legacy`,
`_refresh_legacy`, `_advance_cursor_legacy`, `_restore_promote_row` + the 3
hasattr gates.
Kept (the entire live pipeline): `apply_dream_plan`, `revert_dream_run`
transactional body, `_apply_action`, `_apply_fenced_action`, `_advance_cursor`,
`_reconcile`, `plan.py` entirely, `storage_journal.restore_crossrefs` +
`insert_snapshot`, remaining models. CHECK constraints tighten to the live
action set (Josh 2026-07-30, reversing the earlier keep — dead enum values
are their own cost, and dream can re-run): migration 363 (allocation
manifest; destructive-marked — 0.5's gated path) narrows the action
CHECKs to `{keep, delete, refresh, review, promote}` in the hub schema,
with `storage_schema.py` mirrored (constraint changes ship as migrations
+ baseline, never runtime rewrites). The historical `merge`/`supersede`
snapshot purge operates at RUN granularity (Codex F9, blocker —
`revert_dream_run` replays `list_snapshots(run_id)` and marks the run
reverted, `apply.py:101-257` verified, so purging only the
merge/supersede rows of a run would let a later revert replay the
remainder and stamp a partial restore as `reverted`): migration 363
purges the entire reversible state — all snapshot rows — of every run
containing at least one `merge`/`supersede` snapshot, and stamps those
runs `revert_forfeited` (run-level tombstone status); `revert_dream_run`
fails closed on a forfeited run with an explicit error, never a partial
replay (backup-gated per 0.4; authorized under Decision 11 — reverting
those historical runs is forfeited, accepted). The new status enters the
RUNTIME contract, not just the data (Codex round 7 — migration 363 would
write a status the code doesn't know): `revert_forfeited` joins
`RUN_TERMINAL_STATUSES` (`storage_runs.py:26` verified — currently
`{completed, failed, reverted, revert_failed, interrupted, partial}`),
so run admission (the single-running partial index path), status
display, pruning, and repeated-revert handling all treat forfeited runs
as terminal, and the status-vocabulary exhaustiveness tests update with
it. Update dream test fakes rather than preserving `_legacy` paths for
them.

**Acceptance:**
- 2.12.1 - Manifest symbols removed; live pipeline untouched; dream run
  completes post-change. behavior: "successful dream run" in daemon logs or
  `dream_runs` row in session transcript.
- 2.12.2 - Dream suites green without `_legacy` fakes. test:
  `tests/memory/test_dream.py`.
- 2.12.3 - CHECK constraints match the live action set; affected runs
  purged whole with row-count evidence and stamped `revert_forfeited`;
  `revert_dream_run` fails closed on forfeited runs and replays untouched
  runs normally. test: `tests/memory/` revert-focused run.
- 2.12.4 - `revert_forfeited` is in `RUN_TERMINAL_STATUSES`; admission,
  display, pruning, and repeated-revert paths handle it; exhaustiveness
  tests cover the full vocabulary. symbol: `RUN_TERMINAL_STATUSES`.

### 2.13 TTS: remove voice=[] extra; pin no-torch-at-import [category: code]
`kind: deliverable`

Targets:
- `pyproject.toml`
- `uv.lock`
- `tests/voice/test_lazy_import.py`

Josh's decision, not relitigated: chatterbox-tts STAYS required (agents
reinstall without extras); the 9 `[tool.uv]` pins stay. Remove the no-op
`voice = []` extra (`pyproject.toml:80`). Gating already exists and is reused:
`voice.enabled` default False + `voice.tts_enabled` (`config/voice.py:67-76`),
lazy `importlib` provider load (`providers.py:19-50`), function-local torch
imports, warmup gated (`warmup.py:110,135-137`). Add a regression test pinning
that importing gobby server/voice modules never imports `torch`/`chatterbox`
while disabled.

**Acceptance:**
- 2.13.1 - `voice = []` extra gone; chatterbox-tts + pins present. file:
  `pyproject.toml`.
- 2.13.2 - No-torch-at-import regression test. test:
  `tests/voice/test_lazy_import.py`.

### 2.14 Remove dead Rust mediawiki/wayback + dead deps [category: code]
`kind: deliverable`

Targets:
- `crates/gwiki/src/ingest/mediawiki.rs::*` — scope-reason: whole file deleted
- `crates/gwiki/src/ingest/wayback.rs::*` — scope-reason: whole file deleted
- `crates/gwiki/src/ingest/mod.rs::*` — scope-reason: mod declarations + test-only call sites removed
- `crates/gwiki/Cargo.toml`
- `crates/gcode/Cargo.toml`
- `crates/ghook/Cargo.toml`
- `crates/gcore/Cargo.toml`
- `crates/gcore/tests/public_boundary.rs::*` — scope-reason: dependency string-assertions updated
- `Cargo.lock`
- `docs/guides/gcore-development-guide.md`

Delete `mediawiki.rs` (157 lines — all external refs are under `#[cfg(test)]`)
and `wayback.rs` (550 lines — sole external refs `ingest/mod.rs:702,1140` are
under `#[cfg(test)]` at `:686`) plus their `mod` declarations and test-only
call sites. Kept-adjacent: `SourceKind::MediaWiki`/`Wayback` variants
(`sources/types.rs:26-57`, `credibility.rs:218`) stay — live. Remove dead deps:
`ignore` + `dirs` (gcode), `dirs` (ghook), `postgres-types` (gcore — also
update the manifest string-assertions in `public_boundary.rs:24,35` and
`gcore-development-guide.md:215,237`), `linked-hash-map` (gwiki), duplicate
`gobby-core` dev-dep (gwiki `Cargo.toml:75`). `ureq` stays (live `savings.rs`).
Rebuild + reinstall all binaries.

**Acceptance:**
- 2.14.1 - Files + deps gone; workspace builds; clippy clean per crate.
  behavior: "cargo check + clippy per crate" in session transcript.
- 2.14.2 - public_boundary contract test updated and green. test:
  `crates/gcore/tests/public_boundary.rs`.
- 2.14.3 - gwiki ingest suites green. behavior: "cargo test -p gobby-wiki" in
  session transcript.

### 2.15 Remove dead web modules + dead deps; Python dead deps [category: code]
`kind: deliverable`

Targets:
- `web/package.json::*` — scope-reason: dead deps removed; @types moved to devDependencies
- `web/src/hooks/useAgentSpawn.ts::*` — scope-reason: whole file deleted
- `web/src/hooks/useSessionTokenEvents.ts::*` — scope-reason: whole file deleted
- `web/src/hooks/useAgentRuns.ts::*` — scope-reason: whole file deleted
- `web/src/utils/isolationColors.ts`
- `web/src/components/ui/Textarea.tsx::*` — scope-reason: whole file deleted
- `web/src/hooks/useVoiceCapabilities.ts::*` — scope-reason: whole file deleted
- `web/package-lock.json`
- `pyproject.toml`
- `uv.lock`

Delete the 6 per-export-verified orphan web modules (~21 KB; exact paths
re-verified at implementation). Remove web deps `recharts`, `react-arborist`,
`@dagrejs/dagre`; move the 2 misplaced `@types/*` to devDependencies. Python:
remove `tomli-w`, `opentelemetry-instrumentation-logging`,
`opentelemetry-semantic-conventions`, `anthropic` (instrumentor patches an SDK
nothing calls — remove the instrumentor arm with it); move `pygments` to dev
(CVE pin shipped to prod). Evidence standard (restated for expansion):
per-export/per-dep zero-consumer evidence + kept-adjacent notes in the
session transcript.

**Acceptance:**
- 2.15.1 - Web builds clean with modules + deps removed. behavior:
  "npm run build" in session transcript.
- 2.15.2 - Python deps trimmed; `uv sync` + daemon boot green. file:
  `pyproject.toml`.

### 2.16 Retire template-registry chaff, DB registry verified [category: config]
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/dev.yaml::*` — scope-reason: retired never-run pipeline template deleted
- `src/gobby/install/shared/workflows/qa.yaml::*` — scope-reason: retired never-run pipeline template deleted
- `src/gobby/install/shared/workflows/pipelines/merge-clone.yaml::*` — scope-reason: retired never-run pipeline template deleted
- `src/gobby/install/shared/workflows/pipelines/merge-worktree.yaml::*` — scope-reason: retired never-run pipeline template deleted
- `src/gobby/install/shared/workflows/pipelines/spawn-developer.yaml::*` — scope-reason: retired never-run pipeline template deleted
- `src/gobby/install/shared/workflows/pipelines/spawn-qa.yaml::*` — scope-reason: retired never-run pipeline template deleted
- `src/gobby/install/shared/workflows/pipelines/wiki-research.yaml::*` — scope-reason: retired never-run pipeline template deleted
- `src/gobby/install/shared/workflows/pipelines/nightly-fixes.yaml::*` — scope-reason: abandoned pipeline template deleted
- `src/gobby/install/shared/workflows/agents/nightly-linter.yaml::*` — scope-reason: retired agent template deleted
- `src/gobby/install/shared/workflows/agents/nightly-test-fixer.yaml::*` — scope-reason: retired agent template deleted
- `src/gobby/install/shared/workflows/rules/CLAUDE.md`
- `src/gobby/install/shared/skills/gcode/SKILL.md`

(Path correction from planning verification: bundled pipeline/agent
templates live under `install/shared/workflows/` — the `dev`/`qa`
pipelines are the root `workflows/*.yaml` files and the rest sit in
`workflows/pipelines/` + `workflows/agents/`; the top-level
`install/shared/pipelines|agents/` dirs named by CLAUDE.md do not exist.)

Split from the original template/docs catch-all into 2.16/2.24/2.25
(Codex review). DB registry is source of truth — re-verify enabled/run
state per row before touching. Retire the 7 never-run pipelines (`dev`,
`merge-clone`, `merge-worktree`, `qa`, `spawn-developer`, `spawn-qa`,
`wiki-research`) + abandoned `nightly-fixes` pipeline with its
`nightly-linter`/`nightly-test-fixer` agents (template removal + DB row
retirement). Retire the 5 disabled rules and the zero-consumer
`memory/digest_update` prompt; drop the dead `tag:sync` selector from the
23 agents carrying it; correct the `monolith-enforcement` template/doc
drift (group is ENABLED in DB). Fix `workflows/rules/CLAUDE.md`
(nonexistent `messaging` group + `deprecated/` dir, 4 groups omitted,
12/22 counts wrong, tombstoned `pipeline-worker.yaml` cite). Regenerate
stale `skills/gcode/SKILL.md` from the current source asset.
Kept-adjacent: `expand-task` + `review` pipelines (active); all 29
disabled `*-steps` workflow rows (per-session activation semantics —
untouched).

**Acceptance:**
- 2.16.1 - Registry rows retired with pre-verification evidence per row.
  behavior: "DB registry verification ledger" in session transcript.
- 2.16.2 - rules CLAUDE.md accurate against DB. file:
  `src/gobby/install/shared/workflows/rules/CLAUDE.md`.
- 2.16.3 - gcode SKILL.md regenerated. file:
  `src/gobby/install/shared/skills/gcode/SKILL.md`.

### 2.17 Decompose the 3 oversized gcode files [category: refactor]
`kind: deliverable`

Targets:
- `crates/gcode/src/commands/status/prune.rs::*` — scope-reason: decomposed into cohesive sub-modules
- `crates/gcode/src/commands/codewiki/build_parts/curated_content.rs::*` — scope-reason: decomposed into cohesive sub-modules
- `crates/gcode/src/commands/codewiki/types.rs::*` — scope-reason: decomposed into cohesive sub-modules
- `crates/CLAUDE.md`

Load `decompose-monolith` + `rust` skills. Split `prune.rs` (1,602 raw /
~1,066 non-test), `curated_content.rs` (1,242 / ~1,239 — tests already
out-of-line via `#[path]`), `codewiki/types.rs` (1,029 / no tests) into
cohesive sub-modules, each file < 1,000 lines. Extract `prune.rs`'s inline test
module out-of-line using the `#[path = ".../tests.rs"] mod tests;` pattern
`curated_content.rs` already uses; record that pattern as the crates convention
in `crates/CLAUDE.md`. No behavior change; existing tests green. Rebuild +
reinstall gcode.

**Acceptance:**
- 2.17.1 - All three files < 1,000 lines post-split; no new clippy warnings.
  behavior: "line counts + clippy" in session transcript.
- 2.17.2 - Out-of-line test convention documented. file: `crates/CLAUDE.md`.
- 2.17.3 - gcode suites green. behavior: "cargo test -p gobby-code" in session
  transcript.

### 2.18 Machine identity: machines.id UUID PK [category: code] (depends: 2.21, 2.6, 0.4, 0.5, 0.7, 0.6)
`kind: deliverable`

Targets:
- `src/gobby/storage/migrations/360_identity_cutover_journal.sql`
- `src/gobby/storage/migrations/361_machines_uuid_identity.sql`
- `src/gobby/storage/identity_cutover.py`
- `src/gobby/utils/durable_file.py`
- `src/gobby/runner_init/helpers.py::*` — scope-reason: boot keeps only non-epoch identity work — fresh-identity registration and tombstone re-key
- `src/gobby/cli/hub_maintenance.py`
- `src/gobby/utils/machine_id.py::*` — scope-reason: generator becomes uuid4-only; file read/write moves to durable_file
- `src/gobby/storage/machines.py::*` — scope-reason: boot-time canonical registration + upsert throttle + UUID validation
- `src/gobby/storage/bin_update_state.py::*` — scope-reason: re-key to (machine_id, tool_name) with machines FK
- `src/gobby/runner_init/storage.py::*` — scope-reason: registration wiring at daemon init
- `src/gobby/cli/pack.py::*` — scope-reason: pack/unpack identity-file contract (--restore-identity)
- `pyproject.toml`
- `tests/utils/test_utils_machine_id.py::*` — scope-reason: uuid4-only generator + durable-replace tests
- `tests/storage/test_machines.py::*` — scope-reason: cutover, tombstone, and registration tests

Josh's decision (reverses the earlier opaque-TEXT keep): `machines` gets
`id UUID PRIMARY KEY`; the TEXT `machine_id` column is removed with no
text-key fallback. Generator becomes unconditional `uuid4()`
(`_generate_machine_id`, `machine_id.py:135-153`; `py-machineid` dep removed,
`pyproject.toml:32` sole consumer). `~/.gobby/machine_id` stays the local
identity anchor — its value IS `machines.id`; a machine must know its own row
before it can query the shared hub, so the file is checked first and the DB
derives from it. Staged identity cutover (Codex review 2026-07-30; retirement, not mapping —
no legacy id survives): live inventory shows 15 machine rows (4 uuid-shaped,
plus `comms`, `pipeline`, `cron`, five `web:*`, two
`live-browser-verification-*`) and sessions carrying `legacy-missing:<uuid>`,
`unknown`, `system` besides the uuid majority. (1) Inventory every
`machines.machine_id` and distinct `sessions.machine_id` value. (2)
Classify: the live local machine (identity file present) is remapped
app-level to a fresh uuid4 — a file must be rewritten, so not pure SQL;
runs only after 2.21 retires legacy Fernet (`secrets.py:576` derives key
material from `get_machine_id()` — rewriting the file first would strand
un-migrated secrets). Every other machine row is retired: row deleted, its
sessions' `machine_id` set NULL (origin survives in `source`). (3)
Zero-unmapped gate: before 2.19 adds the FK, assert every remaining
`sessions.machine_id` is NULL or equals a registered `machines.id` — the
migration fails loudly otherwise. Offline machines re-enter via the same
app-level remap at their next boot: stale identity file → fresh uuid4 +
registration; their historical sessions were already NULLed.
Crash-safe cutover orchestration (Codex review): the remap spans two media —
a DB row and the filesystem identity anchor — and no transaction covers
both, so it runs as a dedicated module (new
`storage/identity_cutover.py`) invoked as the identity-cutover campaign
of `gobby hub-maintenance` (0.7) with the daemon stopped and the epoch
open — never from daemon boot, which the epoch fences (Codex round 7:
the earlier boot-time invocation contradicted boot refusing admission
during an open epoch); it runs under the migration
advisory lock, driven by durable PER-IDENTITY journal rows `{old_id,
new_id, phase}` with phases `started → db_committed → file_committed` —
one row per rotated or retired identity, so the rotation is resumable
identity-by-identity and one completed row never stands in for the full
legacy inventory (Codex F7); the journal table
ships as migration 360 (allocation manifest) — a non-destructive
precursor the old runner auto-applies. Migration 360 also (a) drops the
`sessions.machine_id` NOT NULL constraint
(`postgres_baseline_schema.sql:179`, verified) — the cutover's
retirement step writes `machine_id = NULL` while 362, which converts
the column, lands only later, so without this precursor the cutover
could never reach `db_committed` (Codex F6, blocker); the exact
intermediate sequence 360 → cutover-NULLs-sessions → 361 → 362 is
pinned by test — and (b) ships `retired_machine_identities`: one
tombstone row per retired machine id (old id, retired_at, disposition),
written by the cutover for every non-live row it retires; a boot whose
identity file matches a tombstone treats the file as stale and re-keys
fresh (uuid4 + registration) deterministically. Sequencing (reworked for the orchestrator model — the earlier two-boot
`to_regclass` dance existed only because the cutover ran at boot):
migration 360 (journal + fence + tombstones) is a non-destructive
precursor the runner auto-applies at ordinary boot; the identity-cutover
campaign preflights `to_regclass` for the journal table and refuses to
start until 360 is applied; migration 361 is
destructive-marked — it never auto-applies and lands only through 0.5's
gated path (as a step of the same campaign) after the journal reaches
`file_committed`, with its DO-block
guard kept as defense-in-depth: legacy-shaped machines row present but
journal not `file_committed` → RAISE, so the PK conversion never runs
against a half-remapped machine (fresh installs with no legacy row pass
trivially). File replacement is durable, not merely atomic (review finding; lesson:
durable replacement requires file AND parent-directory fsync — implemented
once in `utils/durable_file.py`, the POSIX locking/durable-replace
abstraction whose platform scope is resolved in Constraints, Codex F15): temp
write + fsync, atomic rename, fsync of `~/.gobby`, then a readback
verifying the UUID before the journal advances to `file_committed` —
power loss cannot strand `file_committed` behind a lost rename. Every
phase resumes idempotently via `hub-maintenance resume`: a resume
finding `db_committed` with a stale file rewrites the file and advances;
a resume finding `file_committed` no-ops; an ordinary boot with 361
pending halts at the destructive gate rather than corrupting. Fault-injection tests cover a crash at each DB/file boundary.
Global writer quiescence (Codex review, blocker): the cutover runs
inside an open maintenance epoch (0.7) — every daemon connected to the
hub stopped and fenced from reconnecting at admission;
preflight fails the cutover if `pg_stat_activity` shows any other
application connection; the local identity file is held under an
exclusive lock (`utils/durable_file.py`) for the duration. Quiescence is enforced in the
database, not observed once (review findings — preflight is
point-in-time, a writer can start between preflight and fence
visibility, and four legacy machine rows are already uuid-shaped, so a
value-shape fence cannot distinguish them): migration 360 installs a
deny-all identity-write fence — triggers on machines/sessions rejecting
EVERY identity-column mutation once a cutover journal row exists,
except transactions presenting the cutover's transaction-local
capability (`SET LOCAL gobby.identity_cutover = <journal token>`,
checked by the trigger). The cutover activates the fence (inserts the
journal row) and takes its machine/session inventory in one transaction
under `LOCK TABLE machines, sessions IN EXCLUSIVE MODE`, THEN runs the
`pg_stat_activity` preflight and re-inventories — nothing can slip
between observation and enforcement; 361 removes the fence with the
TEXT column. App-level
machines/sessions write paths in `src/gobby/storage/machines.py` and the
session stores (hook-ingress upsert included) reject
non-UUID machine ids from cutover onward; tests cover a legacy
writer racing fence activation, a uuid-shaped legacy writer mid-window,
and an old daemon reconnecting between preflight and 361 — all
rejected, not interleaved. Rollback is restore-based
and documented: the cutover's fresh 0.4 manifest includes
`~/.gobby/machine_id` and its checksum (Codex review — the rollback
identity copy lives inside the verified manifest, not beside it), so
backup and identity restore the pre-cutover state together. Boot-time `machines` upsert becomes canonical registration (today
rows appear only on session-create/hook-ingress —
`src/gobby/storage/machines.py:107-134`;
promotes M0 P4's checklist note; required before anything can FK machines).
Registration also throttles `upsert_seen`: the hub shows ~619K writes per
4-day stats window against 15 machine rows — every hook ingress re-upserts —
so `last_seen` refreshes at most once per interval after the redesign.
`bin_update_state` re-keys PK → `(machine_id, tool_name)` with FK to
`machines(id)` (today `tool_name` alone with a blind upsert of
`binary_path`/`target` — two daemons overwrite each other every updater cycle;
no session linkage so M0's backfill recipe can't apply); existing rows
backfill to the sole registered machine. Pack/unpack contract (D1
coordination): pack keeps shipping the file; unpack skips it by default
(existing file wins; absent → fresh uuid4 on first boot), `--restore-identity`
opt-in for same-machine disaster recovery — wrong-identity clone is the
dangerous failure, fresh identity the benign one. Rust `gcore/src/machine.rs`
+ ghook envelope stamping unchanged (opaque string passthrough); hook-ingress
upsert now validates UUID shape. `GOBBY_MACHINE_ID` env and sandbox symlink
unchanged in shape.
Kept-adjacent: machines metadata columns (hostname/os/label/tailscale_name/
owner_user_id/first_seen/last_seen); `read_machine_id_from_home` contract;
M0's machine-scoping migrations (authored UUID-native after 2.19).

**Acceptance:**
- 2.18.1 - Generator is uuid4-only; `machineid` import and dep gone. symbol:
  `_generate_machine_id`.
- 2.18.2 - Migration 361 ships `machines.id UUID PK` (TEXT column gone) +
  `bin_update_state` re-key; dual-shape safe; destructive-marked. file:
  `src/gobby/storage/migrations/361_machines_uuid_identity.sql`.
- 2.18.3 - Staged cutover verified: inventory ledger, live-machine remap
  (machines row + rewritten local file agree), non-live rows retired with
  sessions NULLed and one `retired_machine_identities` tombstone per
  retired id, zero-unmapped gate passes. behavior: "cutover ledger +
  gate evidence" in session transcript.
- 2.18.4 - Fresh-home boot generates uuid4 and registers it in `machines` at
  daemon startup. behavior: "fresh-home boot check" in session transcript.
- 2.18.5 - Unpack skips identity by default; `--restore-identity` restores it.
  behavior: "pack/unpack round-trip" in session transcript.
- 2.18.6 - Cutover journal resumes from every phase AND per identity (a
  partially retired inventory resumes where it stopped); fault-injection
  at each DB/file boundary green; 361's guard refuses a half-remapped
  machine; the 360 → cutover-NULLs → 361 → 362 intermediate sequence is
  exercised end-to-end; a tombstoned identity file re-keys fresh at boot.
  test: `tests/storage/` identity-cutover focused run.
- 2.18.7 - Preflight fails on foreign `pg_stat_activity` connections;
  360's deny-all fence (activated under table locks before preflight)
  rejects an old writer reconnecting mid-window AND a uuid-shaped legacy
  writer (tests); re-inventory runs after activation; identity file
  flocked; identity-file replacement fsyncs file + parent dir and
  readback-verifies before the journal advances; identity file +
  checksum present in the cutover backup manifest; restore-based
  rollback documented. behavior: "quiescence + fence + rollback evidence"
  in session transcript.

### 2.19 Sessions machine attribution: UUID FK + sentinel policy [category: code] (depends: 2.18, 0.6, 0.4, 0.5)
`kind: deliverable`

Targets:
- `src/gobby/storage/migrations/362_sessions_machine_uuid_fk.sql`
- `src/gobby/storage/sessions/_upsert.py::*` — scope-reason: natural-key rebuild + normalize_machine_id removal touch the upsert path broadly
- `src/gobby/storage/sessions/_discovery.py::*` — scope-reason: placeholder machine-id set removed from discovery
- `src/gobby/storage/sessions/_registration_cache.py::*` — scope-reason: registration cache keys follow the UUID/NULL contract
- `src/gobby/storage/machines.py::*` — scope-reason: FK-facing lookups follow machines.id
- `src/gobby/servers/models.py::*` — scope-reason: machine_id leaves the web-chat/register request models
- `src/gobby/mcp_proxy/tools/workflows/_pipeline_execution.py::*` — scope-reason: pipeline session lookup re-keys onto source
- `web/src/lib/browserMachineId.ts::*` — scope-reason: whole file deleted
- `web/src/hooks/useChat/sessionRecords.ts::*` — scope-reason: wire contract drops browser machine identity
- `docs/contracts/identity-model.md`
- `tests/storage/test_migration_contract.py::*` — scope-reason: UUID/TEXT column allowlists updated

`sessions.machine_id` becomes `UUID NULL REFERENCES machines(id)`
(migration 362, destructive-marked — 0.5's gated path). It is part
of the session natural key (`idx_sessions_unique(external_id, machine_id,
source, project_id, session_type)`, name hard-coded at `_upsert.py:26`);
rebuild it `UNIQUE NULLS NOT DISTINCT` (PG15+; hub is PG18) so NULL-machine
sessions keep registration idempotency. All non-UUID `sessions.machine_id`
values map to NULL in the migration — the inventory is re-enumerated at
implementation (today: `comms`, `cron`, `pipeline`, `system`, `unknown`,
`unknown-machine`, `<source>`, `web:<uuid>`, `legacy-missing:<uuid>`,
`live-browser-verification-*`), classified by 2.18's cutover, and the
migration fails loudly on any value outside the classified set
(zero-unmapped gate) — session origin is already expressed by `source`.
Projected-key collision preflight (Codex F7, blocker): before rebuilding
the natural key, the migration projects every row's post-mapping key
(external_id, NULL-mapped machine_id, source, project_id, session_type)
and detects duplicate groups — previously distinct rows whose only
distinguisher was a sentinel machine_id would otherwise fail the
`UNIQUE NULLS NOT DISTINCT` rebuild mid-migration. Collisions resolve by
a deterministic survivor policy: latest activity wins (updated_at, then
created_at, then id); every FK reference to a losing row is rewritten to
the survivor; losing duplicate rows are removed — the explicit
Decision 11 extension recorded in Decision 8. The FK rewrite itself can
collide in CHILD tables (Codex round 7): `session_variables` is
one-row-per-session (`session_id UUID PRIMARY KEY`,
`postgres_baseline_schema.sql:824`, verified), so repointing a loser's
row onto a survivor that already has one violates the child PK. The
migration therefore builds a complete inventory of inbound FKs to
`sessions` plus each child's PK/unique constraints from `pg_constraint`
at execution time, projects child-level collisions per duplicate group,
and applies a recorded per-table merge/delete order: one-row-per-session
children keep the survivor's row and delete the loser's (no JSONB merge
is attempted — survivor state wins, recorded policy); multi-row children
repoint only where no unique conflict results and delete the loser's row
otherwise; the `sessions.parent_session_id` self-FK rewrites to the
survivor before loser deletion. Order per group: child merges → FK
rewrites → loser deletion → natural-key rebuild. The preflight emits its
collision ledger (parent and child level) before any mutation; zero
collisions is the expected live outcome, re-verified at execution.
Consumer changes: pipeline session lookup keyed on `machine_id='pipeline'`
(`_pipeline_execution.py:270`) re-keys onto `source`; web chat stops minting
browser-side machine identity (`browserMachineId.ts` deleted; `machine_id`
leaves the web-chat/register request models — the daemon stamps its own
identity server-side, browser continuity rides the existing external-id path,
wire contract updated in lockstep); `normalize_machine_id` + placeholder set
deleted; advisory-lock key strings (`postgres_pool.py:481,492`) carry the new
values transparently (single-hub migration; locks quiesce across the
migration restart). `docs/contracts/identity-model.md` rewritten: derivation
becomes session → `machines.id` → `owner_user_id`; the client-supplied-TEXT
contract (#17427) is retired. Contract-test UUID/TEXT column allowlists
(`test_migration_contract.py:505-605`) updated; test fixtures re-pointed
(~1,189 occurrences, mostly literal strings). M0-artifact prerequisite
(Codex F3 — previously this deliverable performed the amendment, which
sequenced it after the work depending on it): the amendment is 0.6's
deliverable, landing before M0 expansion consumes any slot and before
2.18/2.19 expand; 2.19 VERIFIES it — the registered M0 artifact is the
amended UUID-native version (post-364 slots, no TEXT machine_id) — and
refuses to close if the amendment is missing or stale.
Kept-adjacent: `sessions.source` + `external_id` semantics; natural-key
idempotency guarantees; hook `machine_id` payload field (now UUID or absent);
`GOBBY_MACHINE_ID` passthrough.

**Acceptance:**
- 2.19.1 - Migration 362 converts the column, rebuilds `idx_sessions_unique`
  NULLS NOT DISTINCT, maps sentinels to NULL, adds the FK; destructive-
  marked. file:
  `src/gobby/storage/migrations/362_sessions_machine_uuid_fk.sql`.
- 2.19.2 - Registration idempotency holds for machine-attributed and
  NULL-machine sessions. test: `tests/storage/` sessions-focused run.
- 2.19.3 - Web-chat session create and pipeline session lookup work
  post-change. test: `web/src/hooks/__tests__/` + `tests/workflows/` focused
  runs.
- 2.19.4 - identity-model.md reflects the UUID contract. file:
  `docs/contracts/identity-model.md`.
- 2.19.5 - M0 prerequisite verified: the registered M0 artifact is 0.6's
  amended UUID-native version (post-364 slots, no TEXT machine_id);
  collision preflight ledger recorded (zero expected). behavior: "M0
  amendment verification + collision ledger" in session transcript.
- 2.19.6 - Child-table policy exercised: a synthetic duplicate group
  with one-row-per-session children (session_variables), multi-row
  children, and a parent_session_id self-reference merges per the
  recorded order without constraint violations; the FK/uniqueness
  inventory is emitted from pg_constraint. test: `tests/storage/`
  survivor-merge focused run.

### 2.20 project.json contributor flow + untrack state JSONLs [category: code]
`kind: deliverable`

Targets:
- `.gobby/project.json::*` — scope-reason: committed isolation-marker keys stripped from the tracked file
- `.gitignore`
- `src/gobby/utils/project_init.py::*` — scope-reason: init flow registers the projects row + runs initial index instead of early-returning; write-time strip guard
- `src/gobby/storage/projects.py::*` — scope-reason: ensure_exists becomes targeted upsert with loud name-collision failure
- `src/gobby/hooks/git/pre-push`
- `src/gobby/sync/tasks.py::*` — scope-reason: pre-push export retargets to machine-local backup
- `src/gobby/sync/memories.py::*` — scope-reason: pre-push export retargets to machine-local backup
- `docs/guides/configuration.md`
- `CONTRIBUTING.md`

Committed-UUID keying stays (Decision 9). Contributor-flow fixes: strip the
drive-by-committed isolation marker (`parent_project_path`/`parent_project_id`,
commit `81f413c1a`) from the tracked project.json — today gcode's
self-reference check fails on any other machine and every invocation dies with
"parent code index missing" — and add the write/merge-time guard so markers
cannot be committed again (`initialize_project` guard; reuse
`WORKTREE_LOCAL_PROJECT_KEYS` as a strip list); `gobby init` on an existing
project.json registers the `projects` row (`ensure_exists`) and runs the gcode
initial-index step instead of early-returning (`project_init.py:204-225`);
`ensure_exists` upsert becomes targeted `ON CONFLICT (id)` and fails loudly on
an active-name collision (`storage/projects.py:265-280` currently swallows it
and binds to nothing); non-portable keys (`linear_*`, `parent_*`) never
written to the committed file (write-time ignore-list centralized in
`update_project_json_fields`, the choke point both Linear binding writers —
CLI `_persist_linear_binding` and daemon `ensure_project_binding` — converge
on; bindings are already read from the `projects` row); `_atomic_write_project_json` preserves file
mode (copy the `fchmod` pattern from `project_verification/refresh.py:198-226`).
State files (Decision 10): `.gobby/tasks.jsonl` + `.gobby/memories.jsonl`
untracked — allowlist entries removed from `.gitignore:231-238`, files removed
from the index (kept on disk); the pre-push hook stops auto-exporting/
committing them (`hooks/git/pre-push:28-40`); export retargets to a
machine-local backup path (`~/.gobby/backups/<project>/`) on the same trigger;
manual import (`gobby tasks restore`) unchanged. Public task visibility is
GitHub issues (existing sync, #19367). Docs: configuration.md documents the
project.json schema + commit policy; CONTRIBUTING/ONBOARDING describe the
clone → init → start story.
Kept-adjacent: committed project.json (id + verification/hooks/
validation_detection — the shared payload); tasks.jsonl format + restore
machinery; isolation-marker writing for real worktrees/clones
(`ensure_project_json_for_isolation`).

**Acceptance:**
- 2.20.1 - Fresh-clone simulation: gcode works, `gobby init` registers the row
  and indexes, working tree stays clean. behavior: "fresh-clone walkthrough"
  in session transcript.
- 2.20.2 - State JSONLs untracked; pre-push makes no auto-commit; local backup
  written at push. behavior: "push dry-run" in session transcript.
- 2.20.3 - `ensure_exists` targeted; name collision fails loudly. test:
  `tests/storage/` projects-focused run.
- 2.20.4 - Non-portable keys never re-emitted; file mode preserved. test:
  `tests/utils/` project_init-focused run.

### 2.21 Remove legacy secrets/auth migration paths [category: code]
`kind: deliverable`

Targets:
- `src/gobby/storage/secrets.py::*` — scope-reason: entire legacy-Fernet apparatus removed across the store
- `src/gobby/cli/secrets.py::*` — scope-reason: migrate CLI removed
- `src/gobby/runner_init/storage.py::*` — scope-reason: legacy auth-password migrator removed
- `src/gobby/communications/lifecycle.py::*` — scope-reason: plaintext webhook-secret migrator removed

Remove, each behind hub-state verification: `migrate_legacy_machine_id_secrets`
+ the entire legacy-Fernet apparatus
(`storage/secrets.py:62-64,278,307,575,595-700,726-745`) + the
`gobby secrets migrate` CLI; `_migrate_legacy_auth_password`
(`runner_init/storage.py:39-55`); `_migrate_plaintext_webhook_secrets`
(`communications/lifecycle.py:68-90` — verify no plaintext values
remain). Hard edge: 2.18's identity cutover runs only after this lands
(`secrets.py:576` derives key material from `get_machine_id()` —
rewriting the identity file first would strand un-migrated secrets).
Evidence standard (restated for expansion — this section is
self-contained): each removal ships hub-state proof (zero rows in the
legacy format; no plaintext webhook secrets; auth password already
migrated) in the session transcript. Kept-adjacent: the live secrets
encryption/decryption path and its key handling (everything not behind
the legacy-Fernet gates); the `gobby secrets` CRUD surface minus
`migrate`; webhook-secret encrypted storage and the INBOUND signature
verifiers (`import hmac` sites — verification, not signing, stays);
the auth password verification path.

**Acceptance:**
- 2.21.1 - Fernet apparatus + migrate CLI + auth/webhook migrators
  removed with hub-state evidence. behavior: "per-item evidence" in
  session transcript.
- 2.21.2 - Secrets store works post-Fernet-removal. test:
  `tests/storage/test_secrets.py`.
- 2.21.3 - Kept-adjacent ledger recorded (live crypto path, CRUD CLI,
  inbound verifiers, auth verification). behavior: "2.21 kept ledger"
  in session transcript.

### 2.22 Remove legacy installer/hook migration paths [category: code]
`kind: deliverable`

Targets:
- `src/gobby/cli/installers/droid.py::*` — scope-reason: legacy unwrap/cleanup helpers removed
- `src/gobby/cli/installers/qwen.py::*` — scope-reason: legacy hook types removed
- `src/gobby/cli/installers/hook_commands.py::*` — scope-reason: legacy hook-script detection removed
- `src/gobby/cli/_install_legacy.py::*` — scope-reason: whole file deleted
- `src/gobby/cli/install.py::*` — scope-reason: legacy shim call sites removed
- `src/gobby/cli/pack.py::*` — scope-reason: legacy hub-postgres.db skip removed

Remove: droid `_unwrap_legacy_hooks_wrapper` +
`_cleanup_legacy_droid_hooks_file`; qwen `_LEGACY_HOOK_TYPES`;
`_is_legacy_gobby_hook_script`; `cli/_install_legacy.py` neo4j shim (+
its 3 `cli/install.py` call sites); pack legacy `hub-postgres.db` skip.
Kept (3, recorded): tmux.conf path preference (convention);
`_is_legacy_discovery_placeholder` (live sync behavior);
`_strip_legacy_marker_blocks` (historical transcripts stay).

**Acceptance:**
- 2.22.1 - 6 paths removed; keeps recorded. behavior: "evidence ledger"
  in session transcript.
- 2.22.2 - Install/uninstall flows green. test: `tests/cli/installers/`
  focused run.

### 2.23 Remove legacy data-shape paths + gated github uuid seeds [category: code]
`kind: deliverable`

Targets:
- `src/gobby/wiki/scheduled_jobs.py::*` — scope-reason: legacy purge/reconcile jobs removed
- `src/gobby/storage/build_profiles.py::*` — scope-reason: legacy row-hash helpers removed
- `src/gobby/sync/tasks.py::*` — scope-reason: gated uuid-seed removal
- `src/gobby/sync/task_github_import.py::*` — scope-reason: gated uuid-seed removal

Remove `purge_legacy_wiki_research_jobs` +
`reconcile_stale_wiki_cron_scopes` (verify hub clean first);
`build_profiles.legacy_row_hash`/`_legacy_row_payload`. Gated (1): github
legacy uuid seeds (`sync/tasks.py:105-107`,
`sync/task_github_import.py:193-233`) — remove only if a hub query proves
zero old-seed imports. #19367 closed 2026-07-30 (verified via
gobby-tasks), so the former active-agent sequencing gate is lifted
(Codex nit); re-audit the seed sites against #19367's landed behavior
before removal — line numbers re-verified at implementation.
Evidence standard (restated for expansion — this section is
self-contained): each removal ships hub-state proof (clean wiki cron
scopes; no legacy-hash build_profiles rows; the zero-old-seed-imports
query for the gated item) in the session transcript. Kept-adjacent:
live wiki scheduled jobs and their cadence machinery (only the two
legacy purge/reconcile paths die); `build_profiles`' current row-hash
path; the github import/sync machinery itself including #19367's landed
external-issue sync (only the legacy uuid seeds are candidates, and
only behind the query gate).

**Acceptance:**
- 2.23.1 - Data-shape paths removed with hub-state evidence; gate
  decision recorded either way. behavior: "evidence + gate record" in
  session transcript.
- 2.23.2 - Wiki cron + build-profile suites green. test: `tests/wiki/` +
  `tests/storage/` focused runs.
- 2.23.3 - Kept-adjacent ledger recorded (live wiki jobs, current
  row-hash path, github sync machinery). behavior: "2.23 kept ledger"
  in session transcript.

### 2.24 Detection profiles: agy rename + grok.toml + manifest regen [category: config] (depends: 2.16)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/detection/gemini.toml`
- `src/gobby/install/shared/detection/agy.toml`
- `src/gobby/install/shared/detection/grok.toml`
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: regenerated wholesale via write_bundled_content_manifest

Decision locked (the earlier "if trivial" gate is resolved; profiles are
pure data keyed filename-stem = `id` = runtime provider slug, no code
registration, `agents/detection/registry.py:62-70`): rename `gemini.toml`
→ `agy.toml` with `id = "agy"` — no `gemini` provider slug exists at
runtime, so the file is unresolvable dead weight while `agy` sessions run
manifest-less with silent detection blindness
(`agents/detection/provider.py:24-40`); author `grok.toml` with full
14-rule coverage (rule ids are a hardcoded contract across
`idle_detector.py`/`stall_classifier.py`/`prompt_detector.py`, and
partial profiles degrade silently), seeded from the existing grok pane
prior art (`agents/plan_keystrokes.py:470-473`) and verified against a
live grok pane capture; regenerate
`install/bundled_content_manifest.json` via
`write_bundled_content_manifest` (`install/manifest.py:85-97`).

**Acceptance:**
- 2.24.1 - `agy.toml` + `grok.toml` resolve at runtime with 14/14 rule
  ids; grok rules verified against a live pane capture; bundled-content
  manifest regenerated. behavior: "manifest resolution check" in session
  transcript.

### 2.25 Delete scratch files + doc fixes [category: config]
`kind: deliverable`

Targets:
- `survey.json::*` — scope-reason: scratch file deleted
- `.gitattributes`
- `scripts/setup-firewall.sh`
- `scripts/migrate_index_to_plans_table.py`
- `docs/guides/web-ui.md`
- `docs/guides/release-guide.md`
- `docs/guides/README.md`
- `docs/reviews/sessions-nits-16817/analyzer-summarize.md`
- `docs/reviews/sessions-nits-16817/index-window.md`
- `docs/reviews/sessions-nits-16817/mailbox-storage.md`
- `docs/reviews/sessions-nits-16817/parser.md`
- `docs/reviews/sessions-nits-16817/processor.md`
- `docs/evidence/wiki-parity-2026-06/wp3-audit.txt`
- `docs/evidence/wiki-parity-2026-06/wp3-codewiki-regen.txt`
- `docs/evidence/wiki-parity-2026-06/wp3-codewiki-regen2.txt`
- `docs/evidence/wiki-parity-2026-06/wp3-codewiki-scoped.txt`
- `docs/evidence/wiki-parity-2026-06/wp3-codewiki-scoped2.txt`
- `docs/evidence/wiki-parity-2026-06/wp3-collect.txt`
- `docs/evidence/wiki-parity-2026-06/wp3-deposit-ingest.txt`
- `docs/evidence/wiki-parity-2026-06/wp3-deposit-read.txt`
- `docs/evidence/wiki-parity-2026-06/wp3-deposit-search.txt`
- `docs/evidence/wiki-parity-2026-06/wp3-ingest-file.txt`
- `docs/evidence/wiki-parity-2026-06/wp3-ingest-image.txt`
- `docs/evidence/wiki-parity-2026-06/wp3-ingest-url.txt`
- `docs/evidence/wiki-parity-2026-06/wp3-lint-mid.txt`
- `docs/evidence/wiki-parity-2026-06/wp3-qa-ghook-ask-daemon.txt`
- `docs/evidence/wiki-parity-2026-06/wp3-qa-ghook-ask-direct.txt`
- `docs/evidence/wiki-parity-2026-06/wp3-qa-ghook-read.txt`
- `docs/evidence/wiki-parity-2026-06/wp3-compile-explainer-v2.json::*` — scope-reason: raw evidence dump deleted
- `docs/evidence/wiki-parity-2026-06/wp3-compile-explainer.json::*` — scope-reason: raw evidence dump deleted
- `docs/evidence/wiki-parity-2026-06/wp3-compile-source.json::*` — scope-reason: raw evidence dump deleted
- `docs/evidence/wiki-parity-2026-06/wp3-deposit-ingest.json::*` — scope-reason: raw evidence dump deleted
- `docs/evidence/wiki-parity-2026-06/wp3-deposit-search.json::*` — scope-reason: raw evidence dump deleted
- `docs/evidence/wiki-parity-2026-06/wp3-health.json::*` — scope-reason: raw evidence dump deleted
- `docs/evidence/wiki-parity-2026-06/wp3-qa-ghook-search.json::*` — scope-reason: raw evidence dump deleted
- `docs/evidence/wiki-parity-2026-06/wp3-qa-q2-rrf-ask-daemon.json::*` — scope-reason: raw evidence dump deleted
- `docs/evidence/wiki-parity-2026-06/wp3-qa-q2-rrf-search.json::*` — scope-reason: raw evidence dump deleted
- `docs/evidence/wiki-parity-2026-06/wp3-qa-q3-uuid5-ask-daemon.json::*` — scope-reason: raw evidence dump deleted
- `docs/evidence/wiki-parity-2026-06/wp3-qa-q3-uuid5-search.json::*` — scope-reason: raw evidence dump deleted
- `docs/evidence/wiki-parity-2026-06/wp3-qa-q4-falkor-ask-daemon.json::*` — scope-reason: raw evidence dump deleted
- `docs/evidence/wiki-parity-2026-06/wp3-qa-q4-falkor-search.json::*` — scope-reason: raw evidence dump deleted
- `docs/evidence/wiki-parity-2026-06/wp3-search-hybrid.json::*` — scope-reason: raw evidence dump deleted
- `docs/evidence/wiki-parity-2026-06/wp3-search-sources.json::*` — scope-reason: raw evidence dump deleted
- `docs/architecture/architecture.md`
- `docs/architecture/coding-standards.md`
- `docs/architecture/development-guide.md`
- `docs/architecture/index.md`
- `docs/architecture/technology-stack.md`

Delete scratch: `survey.json`, empty `.gitattributes`, diverged
`scripts/setup-firewall.sh` fork, completed
`scripts/migrate_index_to_plans_table.py`. Doc fixes (every file named —
Codex round 7 replaced the former globs): `web-ui.md` dead
`artifacts.md` link; `release-guide.md` ghook 0.7.2→0.7.3;
`docs/guides/README.md` (the guides index) gains its 3 missing guide
entries; the 5 waived review stubs under
`docs/reviews/sessions-nits-16817/` are deleted (each is a one-line
scope-waiver, verified 2026-07-30); the 31 raw evidence dumps (16 .txt
+ 15 .json) under `docs/evidence/wiki-parity-2026-06/` are pruned;
`docs/architecture/` stamps refresh (5 files here — `source-tree.md` is
2.5's target). Kept-adjacent: `wp3-summary.md`, `wp3-codewiki-verify.md`,
`wp3-ask-daemon-latency-followup.md`, `wp3-vision-daemon-EMPTY.md`,
`wp3-vision-daemon-FINDING.md` (the summaries the dumps supported);
`docs/reviews/sessions-nit-disposition-16817.md` and
`docs/reviews/sessions.md` (the durable disposition record the stubs
point to). Evidence standard (restated for expansion): per-file
staleness evidence + the kept list in the session transcript.

**Acceptance:**
- 2.25.1 - Scratch files gone; doc fixes applied. file:
  `docs/guides/release-guide.md`.
- 2.25.2 - All 5 stubs + 31 dumps deleted; the named kept files remain;
  kept ledger recorded. behavior: "2.25 kept/deleted ledger" in session
  transcript.

---

## P2: Hub data hygiene
`kind: framing`

**Goal**: The Part 1 bloat purged behind verified backups, the writers that
produced it fixed so it doesn't regrow, and Qdrant/FalkorDB reconciled to
live consumers — a clean data model ready for the new hub PC. Scope is
exactly the Decision 11 enumeration; all other live-table data stays.
Independent of M0 and P3 — runs any time after P0, before the hub moves.

### 5.1 Redirect rule_eval telemetry; session-variable expiry [category: code] (depends: 0.3, 0.8)
`kind: deliverable`

Targets:
- `src/gobby/workflows/engine/evaluation.py::*` — scope-reason: allow-path telemetry rewrite spans the rule loop
- `src/gobby/telemetry/logging.py::*` — scope-reason: allow-audit surface joins the log-surface taxonomy
- `src/gobby/config/logging.py::*` — scope-reason: day-sized allow-audit rotation policy config added
- `src/gobby/telemetry/exporters.py::*` — scope-reason: eval counter + latency histogram instruments registered on the Prometheus reader
- `src/gobby/servers/routes/admin/_stats.py::*` — scope-reason: statistics become explicitly block-only
- `src/gobby/servers/pending_interactions.py::*` — scope-reason: abandoned waiter rows gain a terminal path
- `src/gobby/storage/session_lifecycle.py::*` — scope-reason: revival-horizon contract + variables sweep
- `src/gobby/storage/sessions/_field_update.py::_FieldUpdateMixin.revive_expired_terminal_session`
- `src/gobby/runner_maintenance/storage_hygiene.py`
- `src/gobby/mcp_proxy/metrics_store.py::*` — scope-reason: cleanup atomicity + reset scoping
- `src/gobby/mcp_proxy/metrics_events.py::*` — scope-reason: block-only statistics contract

Three writer-side fixes so 5.2's reclaim sticks:
- Rule-eval telemetry leaves PostgreSQL — redirected, not dropped (Josh: an
  allow is as much audit trail as a block, and the code agrees:
  `_run_rule_loop` only records rules that passed the tools pre-filter and
  their `when` condition, so allow rows are real "matched, evaluated,
  allowed in N ms" evidence — the audit's "no-ops" framing was too harsh;
  the volume argument stands). `_run_rule_loop`
  (`workflows/engine/evaluation.py:258-434`) stops appending allow-result
  `MetricsEventRecord`s to `metrics_events` (99.8% of its 9.92M rule_eval
  rows, ~368K/day) and instead (a) emits a structured line to a dedicated
  allow-audit rotating log (a new `classify_log_surface` taxonomy surface,
  `telemetry/logging.py:76` — the kind of surface the operator-managed OTel
  Collector filelog pipeline tails per the epic-12010 observability model:
  no in-process OTLP exporter) and (b) records OTel metric instruments —
  eval counter by rule/result + latency histogram — exported through the
  existing `PrometheusMetricReader`
  (`telemetry/exporters.py:64-65`) and Prometheus endpoint
  (`servers/routes/admin/_health.py:576-590`). Block results keep the PG
  path unchanged (`block_audit.py` centralization: one audit row per
  BlockGate). Durability contract (Codex review): the default rotation
  policy (10 MB × 5 backups, `config/logging.py:18`) would retain only
  hours at this volume, so the allow-audit surface gets its own explicit
  rotation config sized in days — configurable retention target, default
  ≥ 14 days at measured line volume, sizing math recorded — and the surface
  joins 0.4's backup scope. Deviation from the review's required revision,
  recorded: Codex asked to keep the PG allow-writer until a healthy OTel
  Collector + durable downstream sink exist end-to-end; no Collector runs
  on this host and epic-12010 deliberately makes Collector consumption
  optional operator infrastructure, so the file is the durable record and
  the cutover gate is instead: the sized surface + a delivery probe
  (structured allow lines observed during a live rule eval) land in the
  same change that removes the PG allow-writer. Outage behavior: allow-log
  write failure never blocks rule evaluation — it increments a Prometheus
  error counter and a rate-limited daemon-log warning; the block PG path is
  unaffected. The never-blocks claim is structural, not aspirational
  (Codex round 7): allow lines enqueue onto a bounded in-process queue
  (fixed capacity, default recorded with the sizing math) drained by one
  dedicated writer task; overflow drops the newest line and increments a
  dropped-lines counter on the Prometheus surface; shutdown drains under
  a hard deadline (default 2s) and records the residual drop count if it
  expires — file I/O never runs on the rule-eval path. Locked consumer contract (no open decision): historical
  admin/MCP statistics become explicitly block-only —
  `servers/routes/admin/_stats.py:66,275-284` and
  `mcp_proxy/metrics_events.py:80` plus their callers/tests updated;
  hours/days/all-time allow counts leave the PG-backed API. Process-lifetime
  allow counters/histograms are exposed separately on the Prometheus
  surface; the rotating structured log is the durable allow record and
  remains the record when `prometheus_enabled` is false.
- Session-variable clear-on-expiry: no general revival horizon exists today
  (Codex review) — `revive_expired_terminal_session`
  (`sessions/_field_update.py:159`) revives without an age limit, and the
  24h compact-workflow prune is not a session-wide boundary. The contract
  lands first: a single `SESSION_REVIVAL_HORIZON_HOURS` (24h past expiry)
  enforced in every revival path, then the maintenance step clears
  `session_variables` for sessions expired beyond that identical horizon
  (pattern: `prune_stale_compact_workflow_instances`,
  `storage/session_lifecycle.py:200-237`). Today spans/comms/attachments have loops in
  `runner_maintenance.py`; `session_variables` has none — 100% of its 648 MB
  payload belongs to expired/deleted sessions (8,978 expired + 164 deleted
  vs 2 active). The same maintenance pass terminal-izes abandoned
  `pending_interactions` rows (web-chat approval waiters,
  `servers/pending_interactions.py` — 40 rows stuck since 2026-04-16;
  durable rows outlive their in-memory waiters with no expiry path).
- `cleanup_old_metrics` made atomic — aggregate + delete in one transaction
  (`mcp_proxy/metrics_store.py:391-451`; finding
  `docs/reviews/mcp_proxy-core.md:200`); `reset_metrics`
  (`metrics_store.py:279-321`) gains explicit scope filtering in place of
  unfiltered truncation.

**Acceptance:**
- 5.1.1 - Allow-outcome rule evals write no `metrics_events` rows; they
  appear in the rotating surface log and as Prometheus-exposed
  counters/histograms; block rows unchanged. symbol: `_run_rule_loop`.
- 5.1.2 - `SESSION_REVIVAL_HORIZON_HOURS` enforced in every revival path;
  expiry sweep clears variables past that identical horizon only; abandoned
  `pending_interactions` rows reach a terminal status. test:
  `tests/storage/` session-lifecycle focused run.
- 5.1.3 - `cleanup_old_metrics` atomic; `reset_metrics` filtered. test:
  `tests/mcp_proxy/` metrics focused run.
- 5.1.4 - Allow-audit surface retention sized in days with recorded math;
  delivery probe shows allow lines in the new log; write-failure path
  degrades to counter + warning without blocking evals; queue overflow
  drops newest with a counted metric and the shutdown deadline is
  enforced (both pinned by test); the log files appear in the hub-backup
  manifest with checksums (Codex review). test:
  `tests/workflows/` telemetry-focused run.

### 5.2 One-time purge + space reclaim, fresh-backup gated [category: code] (depends: 5.1, 0.4, 0.7)
`kind: deliverable`

Targets:
- `src/gobby/cli/hub_purge/cli.py`
- `src/gobby/cli/hub_purge/_preflight.py`
- `src/gobby/cli/hub_purge/_phases.py`
- `scripts/hub_data_purge.sql`

The purge has an executable owner (Codex round 7 — a SQL file cannot
own epoch handling, backup invocation, filesystem/WAL checks, batch
creation, out-of-transaction vacuums, receipts, or release): the new
`gobby hub-purge` package — `cli.py` (command + registration as the
purge campaign under `gobby hub-maintenance`), `_preflight.py`
(quiescence, headroom, WAL budget), `_phases.py` (per-table DML
transactions, vacuums, receipts, release handoff) — while
`scripts/hub_data_purge.sql` survives as the immutable, sha256-pinned
purge PREDICATES the module loads: the delete predicates stay
reviewable and hash-bound; the orchestration is code.

Runs inside an open maintenance epoch (0.7) covering backup AND purge —
0.4's backup re-runs under the same epoch immediately before, so writes
since the initial backup are captured and no daemon can reconnect
between backup and purge (Codex F4). Execution is phase-safe and
resumable
(Codex review): fail-fast per statement; the preflight asserts quiescence
(daemon stopped AND `pg_stat_activity` shows no other connections to the
database) plus honestly sized headroom (Codex review): free space ≥ the
max per-relation `pg_total_relation_size` (heap + TOAST + all indexes —
`VACUUM FULL` rewrites the relation and rebuilds every index) plus an
honest WAL budget (review finding: `max_wal_size` is a soft checkpoint
target and cannot bound rewrite-generated WAL) — WAL sized to the
rewritten relations plus existing WAL/archive backlog, with
`pg_replication_slots` verified empty, no subscriptions, and archive
retention confirmed not to pin segments; old + new relation copies and
WAL accounted on their actual filesystems — plus a fixed filesystem
safety margin (20%, recorded); explicit
`lock_timeout`/`statement_timeout` policy; Phase A runs the DML deletes in
per-table transactions, Phase B runs `VACUUM (FULL, ANALYZE)` per table
OUTSIDE any transaction block (it cannot run inside one and takes ACCESS
EXCLUSIVE); per-table completion receipts live in `destructive_batches`
(0.7) — authoritative shared storage bound to the batch intent, DB
fingerprint, and purge-script sha256, so a rerun resumes from hub state
alone even if the initiating machine is lost (Codex F5, superseding the
earlier local-file ledger: the ledger tables ship in migration 354, so
they sit inside — not outside — schema authority), validated on rerun
before any skip; every delete predicate is idempotent. Purge script
(reproducible; per-table pre/post `count(*)` + `pg_total_relation_size`
ledger; Part 1 sizes re-verified at execution):
- `metrics_events`: delete `event_type='rule_eval' AND result='allow'` rows
  (~9.9M rows / 2.75 GB).
- `session_variables`: first run of 5.1's expiry sweep across the backlog
  (648 MB payload; 8,978 expired + 164 deleted sessions vs 2 active).
- `token_events`: full purge (352 MB — data not needed today; history lives
  in the backup; ongoing policy is the retention plan's).
- `loop_progress`: full purge (240 MB, 782K rows; old rows valueless).
- `step_executions`: strip `input`/`output` payloads on completed runs
  (143 MB across 32 rows, ~2.5 MB/row May-era pipeline runs; rows stay).
- `spans`: full purge (984 MB; regrows to its 7-day steady state —
  volume tuning is the retention plan's).
Phase B then vacuums each purged table; reclaimed bytes recorded.
Expected reclaim ≥ 4.5 GB of the ~8 GB hub.

**Acceptance:**
- 5.2.1 - Fresh backup manifest immediately precedes the purge run. behavior:
  "pre-purge backup manifest" in session transcript.
- 5.2.2 - Purge script + per-table pre/post ledger recorded; only enumerated
  categories touched. file: `scripts/hub_data_purge.sql`.
- 5.2.3 - Post-purge smoke: daemon boot, rule-eval writes, session create,
  pipeline run, admin stats endpoints. behavior: "post-purge smoke" in
  session transcript.
- 5.2.4 - Phase split honored (DML in transactions, VACUUM outside);
  epoch + quiescence + full-relation/WAL/margin preflight evidence;
  hub-resident completion receipts (`destructive_batches`) support rerun
  from any machine; size ledger recorded. behavior: "size ledger +
  preflight" in session transcript.
- 5.2.5 - `gobby hub-purge` owns the orchestration end-to-end as the
  purge campaign; it refuses a predicates file whose sha256 differs from
  the batch intent row. test: `tests/cli/` hub-purge focused run.

### 5.3 Probe removal + Qdrant/FalkorDB orphan reconciliation [category: code] (depends: 0.4, 0.7, 0.3)
`kind: deliverable`

Targets:
- `scripts/hub_vector_graph_reconcile.py`
- `crates/gcode/src/commands/status/invalidate.rs::*` — scope-reason: targeted-invalidation path exercised and extended for manifest-pinned deletion
- `crates/gcode/src/commands/status/drop_namespace.rs`
- `crates/gcode/src/commands/status.rs::*` — scope-reason: drop_namespace module wired into the status command tree
- `crates/gcode/src/cli.rs::*` — scope-reason: new subcommand added to the clap parser
- `crates/gcode/src/dispatch.rs::*` — scope-reason: new subcommand dispatched
- `src/gobby/cli/recall_maintenance.py`
- `src/gobby/cli/__init__.py::*` — scope-reason: recall-maintenance command registered at the CLI root
- `src/gobby/install/shared/skills/gcode/SKILL.md`
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: regenerated after the gcode CLI-surface change

The `/private/tmp/gobby-grok-edit-probe.olxVdb` probe project (91 files in
the shared index) is removed via the projection OWNER, not direct store
access (Codex review): `gcode invalidate --project-id <uuid> --force`
clears all three stores under the project maintenance lock — the
PostgreSQL code-index rows, the derived `code_symbols_<uuid>` Qdrant
collection, and the project-scoped FalkorDB nodes
(`commands/status/invalidate.rs:34-104`,
`index/indexer/lifecycle.rs:86-124`). Global `gcode prune` is explicitly
NOT used: its
staleness test treats a root path missing on the local filesystem as stale
(`crates/gcode/src/commands/status/projects.rs:37`; global sweep at
`prune.rs:385`), which on a shared hub would delete another machine's live
project (Codex review finding). The reconcile script inventories both
stores and classifies every entry in strict order (Codex review — the
canonical global stores are keyed by reserved names, not registry rows):
(1) reserved global allowlist, always kept — Qdrant `memories` +
`tool_embeddings`; FalkorDB `gobby_code`, `gobby_wiki`, `gobby_kg`,
`gwiki`; (2) registered projections, kept — `code_symbols_<uuid>`/
`gwiki_project_<uuid>`/`gwiki_topic_*` resolved to live project/topic
registry rows; (3) proven ephemeral harness namespaces → delete
candidates; (4) anything else → unknown, report-only, never auto-dropped.
The dry run emits a keep/delete ledger. Apply sequencing is
backup-after-manifest (review finding, blocker — a candidate created
after the initial backup would be deleted with no recoverable copy;
inventory hashing proves identity, not recoverability): stop the leak
producers (no benchmark/harness runs for the window) → produce the
candidate manifest → take a FRESH restore-verified 0.4 backup and
verify every exact candidate appears in its inventory (candidate-level
coverage) → re-inventory → apply. The deletion manifest binds the
backup-manifest sha256 alongside its own ledger hash. Owner commands
mutate sequentially, so a global unchanged-inventory recheck would fail
after the first deletion (review finding): verification runs once under
a reconcile lock inside the open maintenance epoch (0.7); durable
per-target completion receipts live in `destructive_batches` — hub
state, not a local file, so resume works from any machine (Codex F5) —
recording each finished target with 0.7's component-level
pending → applied → verified states: a crash between an external-store
deletion and its receipt leaves `pending`, and resume re-derives the
truth from the exact idempotent postcondition (collection/graph absent)
before completing the receipt (Codex round 7); resume re-verifies
against the original inventory minus completed targets and hard-fails
on any proposed deletion outside the manifest. Mutations run through owner
CLIs only — the Python script is inventory, classification, and
hash-pinned orchestration (Codex review): registered-project orphans via
`gcode invalidate --project-id <uuid> --force`; scratch `gwiki_topic_*`
collections via `gwiki purge --topic <name> --yes` (whole-collection
delete + scope rows; verified to resolve unregistered topic names —
`commands/purge.rs:67-117`, `scope.rs:123-129`); orphan `code_symbols_*` collections whose suffix resolves to no registry
row get an exact-namespace subcommand added to gcode
(`commands/status/drop_namespace.rs`; verified: gcode
has no delete-by-name surface anywhere; every destructive path derives
its target from a project id); recall/debug FalkorDB graphs
(`test_recall_benchmark_*`, `dbg*_*`, `probe_cluster_*`) are created by
the memory/recall harness via its own `FalkorClient`
(`tests/memory/test_recall_benchmark.py:492`,
`test_recall_benchmark_e2e.py:460` — verified) and stay OUT of gcode's
code-projection boundary (Codex review): they get a narrowly scoped
memory/recall-owned cleanup command (`src/gobby/cli/recall_maintenance.py`
— recall is Python-owned, so Python is the owner surface here). Every
owner command consumes the same hash-pinned manifest and deny-lists the
reserved allowlist. The reconcile script itself never opens a direct
Qdrant/FalkorDB/PostgreSQL mutation connection. Because 5.3 changes the
gcode CLI surface, it re-regenerates `skills/gcode/SKILL.md` and the
bundled-content manifest (2.16/2.24's regenerations are not final when
5.3 lands later — Codex review).
Observed today — Qdrant, 28 collections: the live set resolves to
`memories`, `tool_embeddings` (live per 2.2), `code_symbols_<project-uuid>`/
`gwiki_project_<uuid>` for registered projects, and `gwiki_topic_*` for
registered wiki topics; scratch candidates include
`code_symbols_graph-standalone-*`, `gwiki_topic_vverify`,
`gwiki_topic_refresh-test`, `gwiki_topic_vision-smoke`,
`gwiki_topic_gobby-17644-verify`, `gwiki_topic_track-b-bakeoff`. FalkorDB,
41 graphs: 4 live (`gobby_code`, `gobby_wiki`, `gobby_kg`, `gwiki`), 37
pid-suffixed leaks (`test_recall_benchmark_*`, `test_recall_benchmark_e2e_*`,
`dbg*_*`, `probe_cluster_*`). Recall caution: benchmark graphs are verified
against the recall harness first (confirm it creates per-run graphs and
never re-reads old ones); `recall_*` PostgreSQL research data untouched.
Root cause included: the harness leaking `test_recall_benchmark_*`/`dbg*`
graphs gets a teardown fix (leak site pinned at implementation in the recall
benchmark utilities).

**Acceptance:**
- 5.3.1 - Probe project absent from `code_indexed_projects` and both
  projections via `gcode invalidate --project-id --force` (owner CLI; no
  global prune, no direct store deletes). behavior: "targeted deletion
  evidence" in session transcript.
- 5.3.2 - Four-tier classification ledger recorded (reserved globals kept);
  orphans dropped only via the hash-pinned manifest, executed by owner
  surfaces (gcode for `code_symbols_*`, gwiki for topics, the recall
  cleanup command for recall/debug graphs); live surfaces green
  (gcode search, wiki search, memory recall smoke). behavior: "reconcile
  ledger + smoke" in session transcript.
- 5.3.3 - Benchmark/debug graph leak has a teardown fix; a fresh benchmark
  run leaves no new graph behind. test: recall benchmark harness focused run.
- 5.3.4 - gcode SKILL.md + bundled-content manifest regenerated after the
  CLI change. behavior: "regen evidence" in session transcript.
- 5.3.5 - Apply preceded by a fresh backup whose inventory covers every
  candidate (binding sha recorded); per-target receipts support rerun
  after partial completion. behavior: "coverage + receipt evidence" in
  session transcript.

### 5.4 BM25 index verification + reserved disposition slot [category: code] (depends: 2.12, 0.3)
`kind: deliverable`

Targets:
- `docs/evidence/bm25-verification.md`
- `src/gobby/storage/migrations/364_bm25_disposition.sql`

~705 MB of pg_search BM25 indexes (`code_content_search_bm25` 387 MB among
them) show `idx_scan=0`, while gcode search/search-text exercise BM25 daily —
ParadeDB custom scans plausibly bypass the counter. Verify with
`EXPLAIN (ANALYZE)` on the live query shapes (gcode `search-text`/
`search-content` SQL) that the custom scan touches each index; record a
per-index verdict. An index may be dropped only with plan-shape proof of
non-use (expected outcome: all stay; the counter is the artifact). This is
the ParadeDB counterpart of 2.3's btree evidence rule. Slot discipline
(review finding — a conditional drop with no slot would reshuffle the
pre-M0 sequence): slot 364 is reserved now as the BM25-disposition
migration. A proven-dead index ships the destructive-marked drop there;
an all-stay verdict ships 364 as a no-op verdict-record migration — the
chain stays contiguous either way and M0's range (365+) is fixed
regardless of outcome.

**Acceptance:**
- 5.4.1 - Per-index verdict with EXPLAIN evidence recorded. file:
  `docs/evidence/bm25-verification.md`.
- 5.4.2 - Slot 364 ships either way: destructive-marked drop with proof
  attached, or no-op verdict record on all-stay. file:
  `src/gobby/storage/migrations/364_bm25_disposition.sql`.

---

## P3: Schema flatten
`kind: framing`

**Goal**: One regenerated baseline reflecting post-P1 schema, reproducibly
built and triple-verified; filename-aware bookkeeping preventing 346-style
hijacks. **Gates**: 0.3 diff fully reconciled; 2.18/2.19 identity redesign
applied; M0 (#17488) machine-scoping migrations (authored UUID-native)
applied on the hub — encoded as gate leaf 3.0, because Gobby manifests
cannot express cross-epic dependencies (review finding).

### 3.0 M0-landed gate: cross-epic dependency installed and verified [category: config] (depends: 2.19)
`kind: deliverable`

Targets:
- `.gobby/plans/m0-shared-datastores-bridge.md`

Gobby manifests only encode dependencies between sections of the same
document — `_validate_manifest_invariants` rejects any `depends` value
without a manifest entry in the plan (`plans/manifest_parser.py:243-289`,
verified) — so "after M0's migrations" cannot be a manifest edge (review
finding, blocker). It becomes this local gate leaf. 3.0 closes only
when: the amended M0 artifact (2.19.5) is validated and registered; M0's
machine-scoping migration leaf is closed; an explicit cross-epic task
dependency from this epic's P3 tasks onto that M0 leaf is installed via
gobby-tasks; and the hub's `schema_migrations` shows every M0 slot
applied with filename/checksum rows recorded (354's columns make that
attestable live evidence, not prose).

**Acceptance:**
- 3.0.1 - Cross-epic dependency installed; M0 slots applied on the hub
  with filename/checksum bookkeeping recorded. behavior: "M0 gate
  evidence (dependency id + psql bookkeeping listing)" in session
  transcript.

### 3.1 Filename-aware migration bookkeeping: enforcement semantics [category: code] (depends: 0.3, 3.0)
`kind: deliverable`

Targets:
- `src/gobby/storage/migrations.py::*` — scope-reason: mismatch-fatal verification + at-rest contiguity audit across the runner
- `tests/storage/test_migration_contract.py::*` — scope-reason: hijack-scenario and contiguity contract tests

The bookkeeping columns landed in P0 — migration 354 (shipped by 0.7)
adds nullable
`filename`/`checksum` and the runner records both from then on (0.5,
pulled ahead of the destructive chain; review blocker). 3.1 completes
the enforcement semantics: application verifies a recorded version's
filename/checksum against the on-disk file and fails loudly on mismatch
— the 346 hijack class becomes detectable by construction. Make
non-matching migration filenames fatal instead of warn-and-skip
(`migrations.py:229-233`). Historical pre-354 rows are never
retroactively stamped — their applied content cannot be attested, and
slot 346's divergence is deliberate evidence (its reconciled-legacy
record is the 355 repair + 0.3's resolution ledger); checksum
verification applies only to rows with recorded values. The contiguity
check is baseline-relative — versions contiguous from
`BASELINE_VERSION`+1 through `MAX(version)`, 1:1 with on-disk files
(0.5's pending-gap guard is the apply-time half; this is the at-rest
audit); after 3.2's flatten reset the baseline row carries a
pseudo-filename (`baseline@<version>`) plus the generated baseline's
checksum, and post-flatten rows verify normally.
Ships WITH the flatten (3.2 consumes it). This machinery ports to gcore in P4
— keep it dependency-light.

**Acceptance:**
- 3.1.1 - Bookkeeping columns + verification in runner; hijack scenario test.
  symbol: `MigrationRunner.apply_pending`.
- 3.1.2 - Typo'd filename fails hard. test: `tests/storage/test_migration_contract.py`.
- 3.1.3 - Historical rows keep NULL bookkeeping (no retroactive stamping);
  contiguity is baseline-relative; baseline row carries pseudo-filename +
  checksum post-flatten. test: `tests/storage/test_migration_contract.py`.

### 3.2 Flatten migrations into regenerated baseline [category: code] (depends: 3.1, 3.0, 0.4, 5.4, 0.7)
`kind: deliverable`

Targets:
- `scripts/flatten_schema.py`
- `src/gobby/storage/postgres_baseline_schema.sql::*` — scope-reason: baseline regenerated wholesale from the migrated-fresh schema
- `src/gobby/storage/migrations.py::*` — scope-reason: BASELINE_VERSION reset + baseline-row bookkeeping semantics
- `tests/storage/test_migration_contract.py::*` — scope-reason: pinned ranges updated; cutover crash/skew tests

Reproducible flatten script: build fresh-from-migrations schema, emit the new
baseline (preserving seed INSERTs and the gcode/gwiki standalone-DDL adoption
seam — `_classify_baseline_state` states `gcore_code_index`/`gwiki_standalone`
must survive), set `BASELINE_VERSION` to the post-M0 max slot, persist
the canonical pre-flatten reference — the migrated-fresh normalized DDL
plus the seed-row manifest, each sha256-pinned — as checked-in evidence
under `docs/evidence/pre-flatten/` (the reproducible migrated-fresh
baseline must survive deletion of its inputs — Codex review), delete the
folded migration files, reset live-hub `schema_migrations` bookkeeping to the
new baseline row (bookkeeping-only change — no data touched). Update the
contract tests' pinned ranges. Verify by triple diff (via 0.3's harness):
flattened-fresh vs migrated-fresh — identical normalized DDL and exact seed
manifest; vs live — identical normalized DDL, seeds in 0.3's invariant mode
(installed registry rows are authoritative live state, never reset to
template values). Pre-0.5.0: no
compatibility path for pre-flatten DBs beyond the existing
`MigrationUnsupportedError` seam.
Live-hub cutover is a protocol, not a bookkeeping UPDATE (review
finding, blocker — a reset racing another runner, or a crash between
delete and insert, strands `_classify_baseline_state` in
`corrupt_partial`, `storage/hub/postgres.py:485-506`, verified): all
schema clients stopped — the cutover runs inside an open maintenance
epoch (0.7) with a `pg_stat_activity` preflight, as 2.18; the migration
advisory lock held across the
cutover; pre-flatten assets verified first via a two-tier gate (Codex
F13, blocker — pre-354 rows carry NULL bookkeeping by design, so exact
per-row agreement is provable only from 354 onward): applied rows ≥ 354
verify exactly (recorded filename/checksum matches disk), while pre-354
history verifies against the pinned migrated-fresh DDL + seed manifest
(this section's `docs/evidence/pre-flatten/` assets) together with 0.3's
explicit divergence-resolution ledger — 346 and any accepted drift are
named exceptions, never silent passes; the bookkeeping replacement —
delete
old rows, insert the new baseline row — runs in ONE transaction; the
matching runner/baseline deploy is part of the same cutover step, and
`BASELINE_VERSION` is explicitly the post-M0 max applied slot. Tests:
crash between delete and insert rolls back atomically (never
`corrupt_partial`); old-runner-vs-flattened-DB and
new-runner-vs-unflattened-DB skew both fail loudly.

**Acceptance:**
- 3.2.1 - Flatten script reproducible; run recorded. file: `scripts/flatten_schema.py`.
- 3.2.2 - Triple diff clean. behavior: "three-way identical schemas" in session
  transcript.
- 3.2.3 - Fresh install + pytest fixture build from new baseline; focused
  storage suites green. test: `tests/storage/hub/test_postgres_baseline_application.py`.
- 3.2.4 - M0-landed gate recorded (hub `schema_migrations` includes M0 slots
  pre-flatten). behavior: "gate evidence" in session transcript.
- 3.2.5 - Pre-flatten DDL + seed manifest persisted with hashes before
  migration deletion. file: `docs/evidence/pre-flatten/`.
- 3.2.6 - Single-transaction bookkeeping cutover under quiescence + lock;
  crash and runner-skew tests green. test: `tests/storage/`
  flatten-cutover focused run.

---

## P4: gcore schema authority
`kind: framing`

**Goal**: gcore owns baseline + migrations + application machinery; `gdaemon`
applies; Python consumes by shelling out and retains zero persistent runtime
DDL (4.4). The whole phase runs after P3 — encoded as 4.1's `depends: P3`
edge, which every later P4 deliverable inherits transitively.

### 4.1 Embed schema assets + migration runner in gcore [category: code] (depends: 3.2)
`kind: deliverable`

Targets:
- `crates/gcore/assets/schema/baseline.sql`
- `crates/gcore/assets/schema/catalog.manifest.json`
- `crates/gcore/src/schema/mod.rs`
- `crates/gcore/src/schema/runner.rs`
- `crates/gcore/src/schema/assets.rs`
- `crates/gcore/src/schema/identity.rs`
- `crates/gcore/src/schema/verify.rs`
- `crates/gcore/src/schema/gate.rs`
- `crates/gcore/Cargo.toml`
- `crates/gcore/tests/public_boundary.rs::*` — scope-reason: manifest string-assertions follow the new schema module
- `crates/gcore/tests/fixtures/hub_backup_manifest/v2_roundtrip.json`
- `crates/gcore/tests/catalog_manifest_freshness.rs`
- `.github/workflows/rust-ci.yml::*` — scope-reason: catalog-manifest staleness gate wired into CI

The runner is born as a module tree, never a monolith (Codex F14 — the
responsibilities below would breach the 1,000-line ceiling in one
`schema.rs`): `runner.rs` (application machinery), `assets.rs` (embedded
assets + root hash), `identity.rs` (schema identity + handshake),
`verify.rs` (read-only verify contract), `gate.rs` (destructive gate +
backup-manifest consumption), `mod.rs` (public surface).

Move the flattened baseline + post-flatten migrations into
`crates/gcore/assets/schema/` (follows the existing `include_str!` provisioning
pattern, `provisioning/mod.rs:43-51`). Implement the runner in gcore behind the
`postgres` feature (sync `postgres = "0.19"`, matching workspace architecture —
no sqlx/tokio): schema-scoped advisory-lock application (parity with
`hashtext('postgres_migrations_apply'), hashtext(current_schema())`,
`migrations.py:87` — pytest-xdist schemas must not deadlock one another),
interrupted-`CONCURRENTLY` invalid-index detection + repair (parity with
`_INVALID_CONCURRENT_INDEX_SQL`), `-- gobby:non-transactional`
directive, dollar-quote-aware statement splitting, filename+checksum
bookkeeping (port P3's 3.1 semantics), baseline-state classification incl. the
gcode/gwiki standalone adoption seam, seed-row verification against the
canonical seed manifest (0.3), the M0 lockstep guard
(`MAX(version) > latest_known_version()` → fatal), the
`-- gobby:destructive` gate ported from 0.5 (destructive slots never
auto-apply; the gated path consumes 0.4's named `gobby-hub-backup-manifest`
v2 contract, pinned by shared cross-language fixtures so the Python
producer and this Rust consumer cannot drift — Codex F12), and a
machine-readable schema identity — runner protocol version, baseline
version/checksum, latest asset version/checksum, and an ordered
schema-asset manifest ROOT HASH covering the baseline, every migration
filename/checksum, the canonical seed manifest, AND the normalized
catalog manifest (review findings:
endpoint checksums alone leave intermediate assets and seed data
unattested) — exposed for the
bidirectional lockstep handshake (Codex review: one-directional
`MAX(version)` cannot catch a NEW Python invoking an OLD gdaemon against
an empty or older DB). `schema verify` gets a defined read-only contract
(review finding — an unpinned verify could pass on bookkeeping alone
after catalog or seed drift): it checks asset checksums against
bookkeeping rows, the LIVE CATALOG against a normalized catalog
manifest — generated from a scratch apply of the embedded assets
(reusing 0.3's normalizer), embedded in gcore as
`assets/schema/catalog.manifest.json`, and covered by the root hash
(Codex F11, blocker: SQL hashes and seed manifests cannot prove live
columns, constraints, indexes, functions, or triggers; the generated
catalog manifest is the complete oracle). The generator is named and
gated (round 7): `crates/gcore/tests/catalog_manifest_freshness.rs`
performs the scratch apply, regenerates the manifest, and fails when the
checked-in copy is stale; `rust-ci.yml` runs it, so the manifest cannot
drift from the assets it attests — seed invariants
against
the canonical manifest, the newer-DB guard, and named-schema isolation —
mutating nothing. Update public_boundary
assertions. Ownership contract from the audit Part 3 map (5 Rust-owned, 10
shared, ~90 Python-only) recorded as a doc comment / contract doc — schema DDL
is gcore's; CRUD stays put.

**Acceptance:**
- 4.1.1 - Assets embedded; runner applies fresh + idempotent re-apply. symbol:
  `SchemaRunner`.
- 4.1.4 - Concurrent named-schema applies don't deadlock; an interrupted
  non-transactional migration (invalid concurrent index) recovers on
  re-apply. behavior: "lock + recovery tests" in cargo test output.
- 4.1.2 - Lockstep guard fails an older binary against a newer DB. behavior:
  "guard test" in cargo test output.
- 4.1.3 - Feature gating keeps ghook/build.rs free of postgres. behavior:
  "cargo tree -p gobby-hooks" in session transcript.
- 4.1.5 - Destructive-gate parity: a destructive-marked migration halts
  default apply in the gcore runner; the gated path applies. behavior:
  "gate tests" in cargo test output.
- 4.1.6 - `schema verify` detects catalog drift (dropped column/index/
  constraint), seed drift, and bookkeeping drift in a scratch schema.
  behavior: "verify contract tests" in cargo test output.

### 4.2 Create gobby-daemon crate with gdaemon schema CLI [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `crates/gdaemon/Cargo.toml`
- `crates/gdaemon/src/main.rs`
- `Cargo.toml`
- `Cargo.lock`
- `.github/workflows/release-gdaemon.yml`
- `crates/CLAUDE.md`
- `docs/guides/release-guide.md`

New workspace member `crates/gdaemon` (package `gobby-daemon` → binary
`gdaemon`) — the early gobby-daemon: no daemon behavior yet, named for forward
planning; future hosted backend owns schema application from day one.
Subcommands: `gdaemon schema apply [--schema <name>] [--destructive]`,
`gdaemon schema verify`, `gdaemon schema version [--json]` — thin CLI
over gcore's runner. `schema version --json` emits the machine-readable
identity `{runner_protocol, baseline_version, baseline_checksum,
latest_version, latest_checksum, assets_root_hash}`; the install
receipt/version sidecar records the OBSERVED tuple at install time —
observed provenance only, never the trust anchor (Codex review: an old
binary paired with its own old sidecar must not self-attest); the
EXPECTED identity ships with the Python distribution (4.3). Extends the
existing floor-based native-binary freshness machinery with schema
identity. Verification is same-process (review finding, blocker —
verifying one gdaemon process and then launching another for apply
leaves a binary-swap window): `gdaemon schema apply` accepts the
caller's expected identity via child-only environment or stdin and
compares it against its own embedded identity BEFORE opening any DB
connection; mismatch exits without touching the database. The expected
identity is a named, deterministically generated contract artifact
(review finding — without a named artifact, generator, and packaging
integration, source/wheel/editable paths drift): 4.3's
`scripts/generate_schema_expected_identity.py` generates
`src/gobby/storage/schema_expected_identity.json` from `gdaemon schema
version --json`; compared on all six fields; regeneration enforced with
a staleness gate across wheel/sdist/editable/CI flows. Recorded
correction (round 7 asked for Homebrew artifacts): no Homebrew
packaging exists in this repo — verified zero references — so there is
no Homebrew flow to cover; binary distribution rides the existing
per-crate release workflows plus the new `release-gdaemon.yml`.
Process boundary hardened (Codex review): DSN resolution via gcore
bootstrap (`database_url`) with `GOBBY_DATABASE_URL` env override — no
`--dsn` argv flag, credentials never appear in `ps` output, and every
gdaemon error/log line redacts DSN credentials; `--schema` sets
search_path for pytest isolation, accepts only `^[a-z_][a-z0-9_]{0,62}$`,
and is applied as a quoted identifier — never string-spliced SQL — with
malicious-identifier tests. Install to `~/.gobby/bin/gdaemon`; update
crate map + release guide. Mark `docs/plans/rust-migration-epic.md` stale (its `gobbyd`
port-60890 sidecar reservation is superseded by this naming; Josh:
`docs/plans/` is reference-only — `.gobby/plans/` is authoritative).

**Acceptance:**
- 4.2.1 - `gdaemon schema apply` builds a fresh schema identical to the
  baseline. behavior: "apply + diff vs baseline" in session transcript.
- 4.2.2 - `--schema` applies into a named schema. behavior: "scratch-schema
  apply" in session transcript.
- 4.2.3 - Binary installed; version sidecar written. file: `docs/guides/release-guide.md`.
- 4.2.4 - Malicious `--schema` identifiers rejected; no DSN on argv;
  credentials redacted in errors/logs. behavior: "hardening tests" in cargo
  test output.
- 4.2.5 - `schema version --json` emits the six-field identity incl.
  `assets_root_hash`; the install receipt records observed provenance
  only. behavior: "identity output + receipt" in session transcript.
- 4.2.6 - `schema apply` with a mismatched expected identity refuses
  before connecting; binary replacement between a preflight check and
  apply is caught by the same-process comparison. behavior:
  "identity-enforcement tests" in cargo test output.

### 4.3 Switch Python to gdaemon; delete Python migration machinery [category: code] (depends: 4.2)
`kind: deliverable`

Targets:
- `src/gobby/storage/hub/postgres.py::*` — scope-reason: apply_migrations becomes the gdaemon shell-out with DSN pinning
- `src/gobby/storage/hub/runtime.py::*` — scope-reason: all runtime entry points inherit the shell-out
- `src/gobby/storage/migrations.py::*` — scope-reason: whole file deleted
- `src/gobby/cli/schema.py`
- `src/gobby/cli/install_setup.py::*` — scope-reason: gdaemon provisioning moves before DB init; failures become fatal
- `src/gobby/utils/native_bin.py::*` — scope-reason: gdaemon joins the managed-binary resolution and floor set
- `src/gobby/storage/postgres_baseline_schema.sql::*` — scope-reason: whole file deleted
- `scripts/generate_schema_expected_identity.py`
- `src/gobby/storage/schema_expected_identity.json`
- `pyproject.toml`
- `.github/workflows/ci.yml::*` — scope-reason: gdaemon built before the pytest job; identity staleness gate added
- `scripts/schema_diff.py`
- `tests/fixtures/postgres.py::*` — scope-reason: fixtures build schemas through gdaemon
- `tests/storage/test_migration_contract.py::*` — scope-reason: retired; successor contract tests land in test_schema_contract.py
- `tests/storage/test_schema_contract.py`

`PostgresHubDatabase.apply_migrations` becomes a shell-out to `gdaemon
schema apply`, resolved via `resolve_native_bin("gdaemon")`
(`utils/native_bin.py:47` — managed `~/.gobby/bin` first, PATH fallback;
Codex review) with a clear failure message when the binary is
missing/stale. DSN pinning (review finding, blocker — a
`PostgresHubDatabase` holding explicit conninfo while gdaemon
independently resolves bootstrap defaults can migrate a DIFFERENT
database): Python sets child-only `GOBBY_DATABASE_URL` from the exact
database object's resolved DSN — never argv, never left to the child's
own bootstrap resolution; tested with default bootstrap aimed at a
decoy DB.
The version handshake is bidirectional (Codex review): Python pins the
EXPECTED identity — runner protocol, latest version/checksum,
`assets_root_hash` — from release metadata bundled with the Python
distribution (generated at build time; the installed sidecar is observed
provenance, never the trust anchor) and refuses to apply on any
mismatch — a newer Python invoking an old gdaemon against an empty or
older DB fails closed instead of provisioning a stale schema.
Enforcement is same-process (review finding, blocker): the expected
identity passes to `gdaemon schema apply` via child-only env/stdin and
the binary self-compares before connecting (4.2) — a separate
version-check invocation is advisory only, because the binary can be
replaced between processes. Tests cover that exact scenario, a mutated
intermediate migration asset (root-hash mismatch),
old-binary-plus-old-sidecar skew, and binary replacement between
preflight and apply. All ~25
`runtime_hub_database` entry points inherit it. Fresh-install ordering
(Codex review): gdaemon provisioning + version verification move BEFORE DB
initialization — `run_daemon_setup` (`cli/install_setup.py:327`) currently
initializes the database during install before managed native binaries
land; covered surfaces: source/editable install, wheel/sdist, CI, upgrade,
sidecar refresh, uninstall, and binary resolution (Homebrew dropped
from the list — recorded correction, no such packaging exists in-repo).
The expected-identity artifact is packaged concretely (round 7):
`scripts/generate_schema_expected_identity.py` writes
`src/gobby/storage/schema_expected_identity.json`, shipped via
package-data in `pyproject.toml`, with the CI staleness gate rebuilding
and comparing it. Install failures escalate
(review finding — today native-binary and DB-init failures degrade to
warnings, so reordering alone still permits a "successful" install with
no usable schema authority): gdaemon installation, identity
verification, and the initial schema apply become FATAL for full
install/upgrade paths, with source-install and upgrade failure paths
tested.
The `gobby schema` CLI created in 0.5 rebases onto gdaemon here — same
UX and gate semantics, now an identity-enforcing wrapper — so deleting
the Python runner orphans no command registration or diagnostic
(review finding). pytest
`postgres_canonical_seed`/`postgres_db` fixtures shell out with `--schema
<worker schema>`; CI builds gdaemon before the pytest job. Retarget
`scripts/schema_diff.py`'s fresh-build path from
`PostgresHubDatabase.apply_migrations` to `gdaemon schema apply --schema
<scratch>` so 0.3's harness survives P4 (Codex review). Delete
`src/gobby/storage/migrations.py`, `postgres_baseline_schema.sql`, and the
migrations directory; retire the string-match contract tests superseded by
gcore's runner tests (successor: `tests/storage/test_schema_contract.py`,
keeping the shell-out contract + failure-mode assertions). Hosted-path note recorded: this shell-out is the temporary
client-DSN seam; remote-API backend replaces it later.

**Acceptance:**
- 4.3.1 - Daemon startup applies schema via gdaemon; boot green. behavior:
  "daemon start log showing gdaemon apply" in session transcript.
- 4.3.2 - Python migration machinery deleted; no `apply_migrations` SQL left in
  Python. file: `src/gobby/storage/migrations.py`.
- 4.3.3 - Focused pytest builds schemas through gdaemon. test:
  `tests/storage/hub/test_postgres_baseline_application.py` (successor).
- 4.3.4 - Missing-binary failure mode is actionable. test:
  `tests/storage/` shell-out contract test.
- 4.3.5 - Fresh install on a clean home provisions gdaemon before the first
  migration apply. behavior: "fresh-install ordering check" in session
  transcript.
- 4.3.6 - Handshake fails closed: new-Python + old-gdaemon against an
  empty DB; mutated intermediate migration (root-hash mismatch);
  old-binary + old-sidecar skew. test: `tests/storage/` handshake focused
  run.
- 4.3.7 - `schema_diff.py` builds its fresh reference through gdaemon.
  behavior: "diff harness run post-cutover" in session transcript.
- 4.3.8 - Decoy-DB test: explicit-conninfo database object + divergent
  bootstrap default → gdaemon applies to the object's DB only. test:
  `tests/storage/` DSN-pinning focused run.
- 4.3.9 - `gobby schema` survives the runner deletion as a gdaemon
  wrapper with unchanged gate semantics. test: `tests/cli/` schema
  focused run.
- 4.3.10 - Full install/upgrade fails (not warns) when gdaemon cannot
  be installed, verified, or complete the initial apply. test:
  `tests/cli/installers/` focused run.

### 4.4 Retire runtime ensure-DDL from production Python [category: code] (depends: 4.3, 2.4, 0.8)
`kind: deliverable`

Targets:
- `crates/gcore/src/schema/sweep.rs`
- `crates/gdaemon/src/main.rs`
- `src/gobby/runner_maintenance/storage_hygiene.py`
- `src/gobby/memory/dream/storage_schema.py::*` — scope-reason: whole file deleted
- `src/gobby/memory/dream/storage.py::*` — scope-reason: ensure_dream_schema call sites removed
- `src/gobby/memory/dream/service.py::*` — scope-reason: memoized ensure calls removed
- `src/gobby/storage/bin_update_state.py::*` — scope-reason: dead schema constant removed
- `src/gobby/storage/tasks/_dispatch_mutex.py::*` — scope-reason: test-only ensure_table removed
- `src/gobby/storage/tasks/_stage_state_schema.py::*` — scope-reason: whole file deleted
- `src/gobby/storage/tasks/_validation_backoff.py::*` — scope-reason: test-only ensure_table removed
- `src/gobby/storage/tasks/_stage_registry.py::*` — scope-reason: per-construction column probe removed
- `tests/storage/test_schema_contract.py`

Codex Critical, verified 2026-07-31 with a full-tree sweep: six runtime-DDL
sites exist; only two execute in production. `ensure_dream_schema`
(`memory/dream/storage_schema.py:8-158`) fires at boot (reconcile via
`orchestration.py:630`), memoized per service (`service.py:272-276`), and
unconditionally on every dream start (`service.py:337`).
`StageRegistryManager._ensure_phase2_columns` (`_stage_registry.py:326-355`)
fires from `__init__:75` on every construction — a catalog query plus an
open transaction each time even when no ALTER runs (constructed per
dispatch-context build and per `gobby stages` invocation). The other four
are dead or test-only: `BIN_UPDATE_STATE_SCHEMA` (`bin_update_state.py:33-50`,
never executed, drifted from baseline — TEXT vs TIMESTAMPTZ);
`TaskDispatchMutexManager.ensure_table` + `TaskValidationBackoffStore.ensure_table`
(test-only by docstring, 8 test callers, TEXT-vs-UUID drift);
`StageStateSchema.ensure_phase2_columns`/`rebuild_stage_states_table`
(`_stage_state_schema.py:16-153`, zero callers anywhere — includes a
DROP-and-recreate of a live table). Remove all six paths: delete the
ensure/repair functions, dead constants, and `_stage_state_schema.py`
outright; drop the per-construction probe from `StageRegistryManager`;
delete `storage_schema.py` (2.12's CHECK mirror lives there until this
lands) and update the `MEMORY_DREAM_RUNTIME_NORMALIZERS` contract test
accordingly; test fixtures that called `ensure_table` build schema through
gdaemon (4.3) instead; contract assertions land in
`tests/storage/test_schema_contract.py` (4.3's successor —
`test_migration_contract.py` retires with the Python runner; Codex
review). `memory_dream_truth_state` — the one object whose
ONLY source is runtime DDL (read/written at `storage_runs.py:200,217`;
absent from baseline and every migration) — is adopted into the migration
chain by 0.3's reconcile migration so it survives the flatten before this
deletion lands.
Seventh site (review finding — 2.4's startup sweep adds production
`DROP SCHEMA` from Python after the original six-site audit, and a
`TABLE|INDEX` sweep regex would miss it): the `gobby_test_*` sweep
re-homes behind `gdaemon schema sweep-test-schemas` (gcore-owned; same
aged-schema + 6-part name-contract semantics; schema-scoped locking);
Python maintenance invokes the command. The DDL audit itself expands
beyond `CREATE/ALTER/DROP TABLE|INDEX` to schemas, types, functions,
triggers, sequences, constraints, extensions, and views.
Kept-adjacent (not schema authority): TEMP-table staging in
`code_index/_storage/files.py:76-95` (same-transaction scratch); `REINDEX`
repair of damaged BM25 indexes (`bm25_health.py:72` — rebuild-only, creates
nothing); FalkorDB Cypher index creation (`falkor_client.py:294` — not
PostgreSQL); `CREATE EXTENSION` in one-shot install tooling
(`cli/installers/postgres.py:112`) and container `initdb.d` scripts; the
Python migration runner's own bookkeeping DDL (deleted with 4.3).

**Acceptance:**
- 4.4.1 - All six sites removed and the 2.4 sweep re-homed to gdaemon;
  daemon boot, a dream run, and stage-registry paths run without issuing
  DDL. behavior: "boot + dream run post-removal" in session transcript.
- 4.4.2 - Audit shows zero persistent DDL of ANY object kind (tables,
  indexes, schemas, types, functions, triggers, sequences, constraints,
  extensions, views) against PostgreSQL in production Python;
  kept-adjacent ledger recorded. behavior: "DDL sweep output" in session
  transcript.
- 4.4.3 - `memory_dream_truth_state` present in the gcore baseline; dream
  truth-state reads/writes work post-removal. test: `tests/memory/` focused
  run.
- 4.4.4 - Former `ensure_table` test callers build via gdaemon fixtures.
  test: `tests/dispatch/` + `tests/storage/tasks/` focused runs.

### 4.5 Re-home gcode/gwiki standalone DDL onto gcore [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `crates/gcore/src/schema/external.rs`
- `crates/gcode/src/setup/ddl.rs::*` — scope-reason: independent DDL strings replaced by gcore-exported definitions
- `crates/gwiki/src/setup.rs::*` — scope-reason: independent DDL strings replaced by gcore-exported definitions

"gcore is authoritative" is incomplete while gcode (`setup/ddl.rs`, 341
lines) and gwiki (`setup.rs`, 497 lines) each carry independent
PostgreSQL DDL for the shared external tables (Codex F12, blocker —
verified both files exist with self-defined DDL). Both crates already
depend on `gobby-core`, so the least-mechanism fix is direct
consumption, not a generated sync contract: the canonical definitions
for the shared gcode/gwiki table shapes move into
`gcore/src/schema/external.rs` (exported constants/builders), and the
two setup paths emit exactly what gcore exports. The baseline's
standalone-adoption seam (`gcore_code_index`/`gwiki_standalone`
classification states, preserved by 3.2) is unchanged — what changes is
that the DDL those standalone paths execute now has one owner, so
baseline adoption can verify live standalone tables against the same
gcore definitions the baseline embeds. Rebuild + reinstall gcode/gwiki.

**Acceptance:**

- 4.5.1 - Canonical shared-table definitions live in gcore; gcode/gwiki
  setup paths source them from gcore with zero independent DDL strings
  remaining. file: `crates/gcore/src/schema/external.rs`.
- 4.5.2 - Standalone gcode/gwiki setup against a fresh database still
  works; emitted DDL is byte-identical to gcore's export. behavior:
  "cargo test -p gobby-code + -p gobby-wiki setup suites" in session
  transcript.
- 4.5.3 - Baseline adoption seam still classifies and adopts standalone
  tables. test: `tests/storage/hub/test_postgres_baseline_application.py`.

### 4.6 Close-out: file the follow-up epic for every deferral [category: config] (depends: 4.4, 4.5, 5.2, 5.3, 2.7, 2.9, 2.11, 2.13, 2.14, 2.15, 2.17, 2.20, 2.22, 2.23, 2.24, 2.25)
`kind: deliverable`

Targets:
- `.gobby/plans/gcore-schema-authority.md`

Decision 12 requires a follow-up epic materializing every Out of Scope
deferral, but no deliverable owned its creation — nothing gated root
closure on it (Codex F16, blocker). This final leaf owns it: create the
follow-up epic via gobby-tasks, one leaf task per deferred obligation
below, link already-tracked items by their existing ids instead of
duplicating, and record the epic + task ids in this plan's Task Mapping
before the root epic closes. Because 4.6 is a leaf of this epic, the
root cannot close without it — the gate is structural. Fleet management
(`machines.owner_user_id` real FK + enrollment, M3) is explicitly
excluded per Decision 12.

**Acceptance:**

- 4.6.1 - Follow-up epic exists; retention-policy plan-drafting task
  filed (TTL/cadence for metrics_events, token_events, loop_progress,
  step_executions, spans volume, recall_* growth). behavior: "task id in
  close-out ledger" in session transcript.
- 4.6.2 - Hub-PC datastore-move plan-drafting task filed. behavior:
  "task id in close-out ledger" in session transcript.
- 4.6.3 - Shared-daemon / feature-crate build-out plan-drafting task
  filed (two-daemon-hub roadmap). behavior: "task id in close-out
  ledger" in session transcript.
- 4.6.4 - Hot-path perf task filed (projection-cleanup DELETEs +
  vector-branch enqueue bug, triage polling cadence,
  `gh_triage_build_dispatches.task_id` FK index). behavior: "task id in
  close-out ledger" in session transcript.
- 4.6.5 - `indexing.extra_excludes` follow-up task filed. behavior:
  "task id in close-out ledger" in session transcript.
- 4.6.6 - Machine-attribution deferral task filed (attachments blob
  attribution, prune machine-scoping, `code_index_prune_dirty_projects`
  scoping) with #17435/#17437 linked, not duplicated. behavior: "task id
  in close-out ledger" in session transcript.
- 4.6.7 - Git-history scrub decision task filed (tasks.jsonl /
  memories.jsonl before repo publication). behavior: "task id in
  close-out ledger" in session transcript.
- 4.6.8 - Rust inline-test file-size-linter task filed; keep-records (29
  `*-steps` rows) confirmed task-free; already-filed links recorded
  (#19365, #19366, #19367-closed). behavior: "close-out ledger complete"
  in session transcript.

---

## V1: Verification
`kind: verification`

Per-deliverable: focused pytest with `GOBBY_TEST_PROTECT=1`, `uv run ruff check
src/`, `uv run mypy src/`, `cargo check`/`cargo clippy -p <crate>` +
`cargo test -p <crate>` per touched crate, `npm run build` per web change,
rebuild + reinstall `~/.gobby/bin` binaries after every crate change. P0:
schema-mirror-check + pre-commit green; 346 repair verified by live psql;
destructive-gate halt + gated apply demonstrated; backup manifest reaches
`restore_verified` for all three stores. P1:
per-item evidence + kept-adjacent ledgers in session transcripts; daemon boot +
dream run + pipeline webhook delivery smoke checks. Identity (2.18/2.19):
live-hub remap evidence (machines row, sessions FK integrity, rewritten
`~/.gobby/machine_id`), fresh-home boot registering a uuid4 machine, web-chat
+ pipeline session smoke. 2.20: fresh-clone contributor walkthrough + push
dry-run. P2 (hygiene): backup manifest + restore-verification evidence;
per-table pre/post row-count and `pg_total_relation_size` ledger;
Qdrant/FalkorDB keep/delete inventory diff; post-purge daemon boot +
rule-eval + gcode search + wiki search + memory recall smoke. P3: triple
diff clean; single-transaction bookkeeping cutover demonstrated;
fresh-install and pytest paths green on the flattened baseline.
P4: gdaemon
apply/verify/version round-trip; two consumers proven (daemon boot + pytest);
lockstep guard demonstrated; gcode/gwiki setup DDL sourced from gcore
(4.5); close-out epic filed with per-deferral tasks (4.6). End-to-end:
`uv run gobby restart` + task CRUD,
memory recall, gcode search, wiki search smoke pass on the migrated hub.

## Out of Scope (recorded so nothing is lost)
`kind: framing`

**Follow-up-epic requirement (Decision 12):** each deferred-work item below
becomes a leaf task in a follow-up epic filed at this plan's close-out — not
created now. Deliverable 4.6 owns the filing, with one acceptance item
per deferred obligation, and gates root-epic closure structurally
(Codex F16). That epic also carries plan-drafting tasks fleshing out the
drafted #17488-adjacent plans (future retention plan; hub-PC datastore move;
shared daemon with machine-local execution / feature-crate build-out per the
`two-daemon-hub.md` roadmap). Items already tracked link their existing ids
(#19365/#19366/#19367, #17435/#17437, M0 #17488). Keep-records (the 29
`*-steps` rows) generate no tasks. Fleet management is excluded — see its
entry below.

- **Ongoing retention-policy machinery → future retention plan:** TTL/cadence
  policies for `metrics_events`, `token_events`, `loop_progress`,
  `step_executions`, spans volume tuning, and `recall_*` growth. The one-time
  purges, writer-side fixes, `token_events` index drops, BM25 verification,
  and probe/orphan cleanup formerly deferred here moved into scope (0.4,
  2.3, P2 — Decision 11).
- **Hub-PC migration itself** (moving the datastores to the new machine) —
  consumes P2's clean state; not a deliverable here.
- Already filed: #19365 comms_routing_rules; #19366 delivery-campaign bypass;
  #19367 external issue sync (closed 2026-07-30 — 2.23's uuid-seed
  removal keeps only its hub-query evidence gate).
- Hot-path perf on live tables (projection-cleanup DELETEs + vector-branch
  enqueue bug, triage polling cadence, `gh_triage_build_dispatches.task_id` FK
  index) — not attached to dropped tables.
- `indexing.extra_excludes` config key — future single-key follow-up if a
  concrete repo needs non-gitignored excludes.
- Machine attribution beyond identity: M0's worktrees/clones/agent_runs/
  cron_runs stamping (UUID-native, after 2.19); per-machine project paths +
  `code_indexed_projects.root_path` split (#17435/#17437);
  `chat_attachments`/`comms_attachments` machine attribution or shared blob
  storage (machine-local files, zero attribution, unplanned anywhere —
  recorded here so it isn't lost); `code_index_prune_dirty_projects`
  machine scoping; global `gcode prune`'s root_path-staleness heuristic
  must become machine-scoped before it is safe on a shared hub (Codex
  review — see 5.3's targeted-deletion workaround); `machines.owner_user_id` real FK + enrollment (M3 —
  fleet management: excluded from the follow-up epic, stays put pre-0.5.0).
- Git-history scrub of previously committed `tasks.jsonl`/`memories.jsonl`
  before the repo goes public (filter-repo decision).
- File-size-ceiling linter for Rust inline tests (policy applied via 2.17;
  enforcement tooling later).
- Feature-crate build-out (`gobby` multicall CLI, real gdaemon supervisor,
  session/spawn split); M0 epic itself beyond the P3 gate.
- 29 disabled `*-steps` workflow rows (per-session activation semantics).

## Execution notes
`kind: framing`

- Post-approval sequence per the Full Gobby workflow: write
  `.gobby/plans/gcore-schema-authority.md` (canonical artifact), `uv run gobby
  plans validate`, THEN — before registering or expanding either plan —
  execute 0.6: amend `.gobby/plans/m0-shared-datastores-bridge.md` to the
  UUID-native spec, validate and re-register it (Codex round 7: the M0
  contract must be amended while still in the planning session, not as an
  execution leaf), then register this plan, then the separately-approved
  enhancement + adversarial review gates, then optional `gobby build`
  handoff.
- Task tracking: new root epic; every deliverable maps 1:1 to an expansion
  leaf via the adversary-written M1 manifest.
- Close-out requirement: 4.6 files the follow-up epic materializing Out
  of Scope per Decision 12 before this plan's root epic closes.
- Migration allocation manifest (review-driven; disk head 353 at drafting —
  re-verified against disk + live `MAX(version)` at expansion; the root
  epic owns the range under one serialized allocator; independent "next
  free" probing is prohibited): 354 migration-bookkeeping columns +
  `maintenance_epochs`/`destructive_batches` ledger tables
  (precursor — auto-applies; 0.7), 355 reconcile-346 + tmux drops
  [destructive], 356 live-drift reconcile v2 incl.
  `memory_dream_truth_state` adoption, 357 drop dead tables [destructive],
  358 drop dead columns + `token_events` indexes [destructive], 359
  `_pgaudit_probe` drop [destructive], 360 identity-cutover per-identity
  journal + deny-all fence + sessions NOT NULL drop + retired-identity
  tombstones (precursor — auto-applies), 361 machines UUID PK
  [destructive], 362 sessions machine FK [destructive], 363 dream CHECK
  tighten + run-level snapshot purge [destructive], 364 BM25 disposition
  (reserved
  — destructive drop or no-op verdict record; 5.4). M0's UUID-native
  machine-scoping slots allocate next (365+, count fixed by 0.6's
  M0-artifact amendment). Destructive-marked slots never auto-apply
  (0.5), and every destructive batch runs inside an open maintenance
  epoch (0.7); slot order is enforced by 0.5's contiguous-range guard
  plus the formal slot-chain edges below.
- P1 deliverables are broadly parallelizable; hard edges (all encoded as
  DELIVERABLE-heading `depends:` — prose ordering has no executable
  effect, and phase-heading annotations are equally inert for manifest
  emission, Codex F2 + round 7, which is why the former `P2 (depends:
  P0)` heading annotation is replaced by explicit 0.3 edges on 5.1, 5.3,
  and 5.4): 0.1→0.4→0.7→0.5 before 0.2 and before every destructive
  migration (runner- and epoch-enforced, not transcript-enforced),
  0.8 before 2.4, 5.1, and 4.4 (runner-maintenance decomposition
  precedes every leaf touching that module), 2.1→2.2,
  0.3→2.2 (drift reconciled before the first dead-table drop), 2.8→2.9
  (removed-key machinery's last
  run precedes its retirement), 2.5→2.10 (dead-file deletions land
  before duplicate consolidation — pipelines_runtime.py overlap),
  2.10→2.11, 2.16→2.24 (manifest regen
  after template removals), 2.21→2.18→2.19 (Fernet retired before the
  identity remap; sessions FK after the machines PK), slot-chain edges
  0.2→0.3→2.2→2.3→2.6→2.18→2.19→2.12→5.4 (migration files land in slot
  order; 0.5's contiguity guard backstops), 0.6 executed in the
  planning session (post-approval, pre-registration) with 2.18/2.19
  carrying the verification edge, every epoch consumer (0.5 gated
  apply, 5.2 purge, 5.3 reconcile, 2.18 cutover, 3.2 flatten) running
  as a `gobby hub-maintenance` campaign (0.7), P3 gated
  by 3.0 (cross-epic M0 dependency installed via gobby-tasks — manifest
  edges are same-document only, `plans/manifest_parser.py:243`),
  3.2 after 5.4 (slot 364 ships either way before the flatten), 5.1→5.2
  (writers stopped before the purge)
  with a fresh 0.4 snapshot inside 5.2's epoch, 5.3 strictly
  sequenced producers-stopped → manifest → fresh covering backup →
  apply, P2 (hygiene) after P0 via the explicit 0.3 edges, independent
  of M0/P3 — sequence it before the hub
  moves to the new PC, all of P3 (flatten) after 0.3 + 3.0, P4 after P3
  (4.1's `depends: 3.2` edge), 4.4 after 2.4 (the sweep it re-homes must
  exist), 4.6 last — its `depends:` list names the terminal leaf of
  every phase chain, so the follow-up epic files before root closure by
  construction.

## Task Mapping
`kind: framing`

<!-- Updated after task creation: root epic, phase sub-epics, leaves, and
4.6's follow-up-epic ids land here at close-out. -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Fix repo config breakage
  category: config
  task_type: feature
  depends_on: []
  validation_criteria: '0.1.1: Mirror byte-identical to ghook copy; schema-mirror-check
    passes. file: `schemas/diagnose-output.v2.schema.json`.

    0.1.2: `.gitleaks.toml` absent; pre-commit gitleaks hook runs with default rules
    and passes. behavior: "gitleaks default ruleset active" in pre-commit run.

    0.1.3: `.github/coderabbit.yaml` deleted. file: `.github/coderabbit.yaml`.

    0.1.4: Per-fix evidence + the kept-adjacent ledger recorded. behavior: "0.1 evidence
    ledger" in session transcript.'
  labels:
  - covers:gcore-schema-authority:0.1:0.1.1
  - covers:gcore-schema-authority:0.1:0.1.2
  - covers:gcore-schema-authority:0.1:0.1.3
  - covers:gcore-schema-authority:0.1:0.1.4
  tdd: true
  source_section: '0.1'
  assigned_agent: backend-developer
- title: Repair hijacked migration slot 346 + drop orphan tmux tables
  category: code
  task_type: feature
  depends_on:
  - '0.4'
  - '0.5'
  validation_criteria: '0.2.1: Migration 355 exists, is dual-shape safe (fresh + live),
    and carries the destructive marker. file: `src/gobby/storage/migrations/355_reconcile_346_cron_display_name.sql`.

    0.2.2: Live hub has `cron_jobs.display_name`; tmux tables gone. behavior: "column
    present, orphan tables absent" via psql check in session transcript.

    0.2.3: Contract test covers 355. test: `tests/storage/test_migration_contract.py`.'
  labels:
  - covers:gcore-schema-authority:0.2:0.2.1
  - covers:gcore-schema-authority:0.2:0.2.2
  - covers:gcore-schema-authority:0.2:0.2.3
  tdd: true
  source_section: '0.2'
  implementation_domain: backend
- title: Build fresh-vs-live schema diff harness
  category: code
  task_type: feature
  depends_on:
  - '0.2'
  validation_criteria: '0.3.1: Diff script exists, reproducible, documented usage.
    file: `scripts/schema_diff.py`.

    0.3.2: Zero unexplained divergences between fresh and live; resolutions recorded.
    behavior: "clean diff output" in session transcript.

    0.3.3: Reconcile migration ships if needed (else explicitly recorded as not-needed).
    file: `src/gobby/storage/migrations/356_reconcile_live_hub_schema_drift_v2.sql`.'
  labels:
  - covers:gcore-schema-authority:0.3:0.3.1
  - covers:gcore-schema-authority:0.3:0.3.2
  - covers:gcore-schema-authority:0.3:0.3.3
  tdd: true
  source_section: '0.3'
  implementation_domain: backend
- title: Hub backup command + verified-restore manifest
  category: code
  task_type: feature
  depends_on:
  - '0.1'
  validation_criteria: '0.4.1: `gobby hub-backup` covers PG (`-Fc` + `pg_dumpall --globals-only`)
    + Qdrant + FalkorDB + volume tarballs; daemon stopped first and restarted last;
    services restart even on failure; v2 manifest with distinct `archive_verified`/`restore_verified`
    states, fingerprint, checksums, row-count probes; allow-audit log files enter
    the inventory once 5.1 lands. file: `src/gobby/cli/hub_backup/cli.py`.

    0.4.2: Initial full backup completed with `restore_verified` earned for all three
    stores via scratch restores, incl. a globals replay with role/ACL verification.
    behavior: "backup manifest + restore checks" in session transcript.

    0.4.3: Freshness/identity machine-checked: the gate refuses a manifest older than
    max age, lacking `restore_verified`, or fingerprint-mismatched. test: `tests/cli/`
    hub-backup focused run.'
  labels:
  - covers:gcore-schema-authority:0.4:0.4.1
  - covers:gcore-schema-authority:0.4:0.4.2
  - covers:gcore-schema-authority:0.4:0.4.3
  tdd: true
  source_section: '0.4'
  implementation_domain: backend
- title: Destructive-migration gate in the runner
  category: code
  task_type: feature
  depends_on:
  - '0.4'
  - '0.7'
  validation_criteria: "0.5.1: Boot/CLI halts before a pending destructive migration;\
    \ fresh- schema and pytest paths unaffected. test: `tests/storage/test_migration_contract.py`.\n\
    0.5.2: Gated apply verifies `restore_verified` + freshness + stable identity +\
    \ epoch binding (`manifest.epoch_id` = open epoch) + exact pre-batch schema-head\
    \ match, and refuses each failure mode individually. test: `tests/storage/` gate\
    \ focused run.\n0.5.3: Marker audit: destructive SQL without the marker fails\
    \ the contract suite, incl. TRUNCATE/DROP CONSTRAINT/DO-block cases. test: `tests/storage/test_migration_contract.py`.\n\
    0.5.4: Interrupted destructive batch resumes from DB-attested bookkeeping after\
    \ every committed migration \u2014 including a crash after commit but before any\
    \ receipt write, and including resume from a different machine reading only hub\
    \ state; a prefix mismatch or a different-bytes-same-version runner refuses loudly.\
    \ test: `tests/storage/` batch-resume focused run.\n0.5.5: The contiguity guard\
    \ halts on a gapped pending chain; the gated apply refuses to run without an open\
    \ maintenance epoch. test: `tests/storage/test_migration_contract.py`."
  labels:
  - covers:gcore-schema-authority:0.5:0.5.1
  - covers:gcore-schema-authority:0.5:0.5.2
  - covers:gcore-schema-authority:0.5:0.5.3
  - covers:gcore-schema-authority:0.5:0.5.4
  - covers:gcore-schema-authority:0.5:0.5.5
  tdd: true
  source_section: '0.5'
  implementation_domain: backend
- title: Amend the M0 artifact to UUID-native machine scoping
  category: config
  task_type: feature
  depends_on: []
  validation_criteria: '0.6.1: M0 artifact specifies UUID-native machine scoping with
    post-364 slots; no `machine_id TEXT` remains in its spec. file: `.gobby/plans/m0-shared-datastores-bridge.md`.

    0.6.2: Amended artifact validates and re-registers. behavior: "validate + register
    output" in session transcript.'
  labels:
  - covers:gcore-schema-authority:0.6:0.6.1
  - covers:gcore-schema-authority:0.6:0.6.2
  tdd: true
  source_section: '0.6'
  assigned_agent: backend-developer
- title: 'Shared maintenance epoch: DB-enforced fence, orchestrator, ledgers'
  category: code
  task_type: feature
  depends_on:
  - '0.4'
  validation_criteria: "0.7.1: Migration 354 ships bookkeeping columns + both ledger\
    \ tables + the login-fence trigger; auto-applies as a non-destructive precursor.\
    \ file: `src/gobby/storage/migrations/354_migration_bookkeeping.sql`.\n0.7.2:\
    \ With an epoch open, a tokenless login is rejected BY THE DATABASE \u2014 pinned\
    \ for a Python client, a Rust (gcode/gwiki-style) connection, and a bare psycopg\
    \ connection simulating a pre-protocol daemon; with no epoch open, logins are\
    \ unaffected; Python entry points surface the courtesy diagnostic. test: `tests/storage/test_maintenance_epoch.py`.\n\
    0.7.3: Epoch open terminates pre-existing foreign connections and verifies clean\
    \ `pg_stat_activity`; release happens only via the owning orchestrator run or\
    \ evidence-gated abort. test: `tests/storage/test_maintenance_epoch.py`.\n0.7.4:\
    \ `gobby hub-maintenance` run/status/resume/abort lifecycle works end-to-end;\
    \ resume re-enters an interrupted campaign from hub state alone (different-machine\
    \ simulation); destructive commands refuse to run outside an orchestrator-owned\
    \ epoch. test: `tests/cli/` hub-maintenance focused run.\n0.7.5: Receipt state\
    \ machine: a crash injected between an external-store deletion and its receipt\
    \ resumes via the component's idempotent postcondition; a pending receipt whose\
    \ postcondition does not hold re-runs its component. test: `tests/storage/test_maintenance_epoch.py`."
  labels:
  - covers:gcore-schema-authority:0.7:0.7.1
  - covers:gcore-schema-authority:0.7:0.7.2
  - covers:gcore-schema-authority:0.7:0.7.3
  - covers:gcore-schema-authority:0.7:0.7.4
  - covers:gcore-schema-authority:0.7:0.7.5
  tdd: true
  source_section: '0.7'
  implementation_domain: fullstack
- title: Decompose runner_maintenance.py into a package
  category: refactor
  task_type: feature
  depends_on: []
  validation_criteria: '0.8.1: Package replaces the module; every file < 1,000 lines;
    import paths preserved via `__init__` re-exports; no behavior change. behavior:
    "line counts + import check" in session transcript.

    0.8.2: Daemon boots and maintenance loops register unchanged. test: `tests/` runner-maintenance
    focused run.'
  labels:
  - covers:gcore-schema-authority:0.8:0.8.1
  - covers:gcore-schema-authority:0.8:0.8.2
  tdd: false
  source_section: '0.8'
  assigned_agent: backend-developer
- title: Rewrite claims reader off workflow_states
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '2.1.1: Claims resolve from tasks table; no workflow_states
    reference remains in claims path. symbol: `get_claimed_task_owners`.

    2.1.2: CLI listing still marks claimed tasks. test: `tests/cli/tasks/` focused
    run.'
  labels:
  - covers:gcore-schema-authority:2.1:2.1.1
  - covers:gcore-schema-authority:2.1:2.1.2
  tdd: true
  source_section: '2.1'
  implementation_domain: backend
- title: Drop dead tables
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  - '0.3'
  - '0.4'
  - '0.5'
  validation_criteria: '2.2.1: Migration 357 drops all five tables + FKs; dual-shape
    safe. file: `src/gobby/storage/migrations/357_drop_dead_tables.sql`.

    2.2.2: Rule-eval pipeline has no override probe. symbol: `WorkflowEngine.evaluate`.

    2.2.3: Row-count/sequence evidence for each table recorded pre-drop. behavior:
    "evidence block per table" in session transcript.

    2.2.4: Focused suites green. test: `tests/workflows/test_rule_engine.py`.'
  labels:
  - covers:gcore-schema-authority:2.2:2.2.1
  - covers:gcore-schema-authority:2.2:2.2.2
  - covers:gcore-schema-authority:2.2:2.2.3
  - covers:gcore-schema-authority:2.2:2.2.4
  tdd: true
  source_section: '2.2'
  implementation_domain: backend
- title: Drop dead columns + dead indexes
  category: code
  task_type: feature
  depends_on:
  - '2.2'
  - '0.4'
  - '0.5'
  validation_criteria: '2.3.1: Migration 358 ships with per-column and per-index verification
    evidence. file: `src/gobby/storage/migrations/358_drop_dead_columns.sql`.

    2.3.2: Contract tests updated. test: `tests/storage/test_migration_contract.py`.'
  labels:
  - covers:gcore-schema-authority:2.3:2.3.1
  - covers:gcore-schema-authority:2.3:2.3.2
  tdd: true
  source_section: '2.3'
  implementation_domain: backend
- title: 'gobby_test_* schema hygiene: leaked-schema drops + leased startup sweep'
  category: code
  task_type: feature
  depends_on:
  - '0.8'
  validation_criteria: '2.4.1: Startup sweep registered in daemon maintenance; drops
    aged `gobby_test_*` schemas only under an acquired lease + recheck; a held lease
    (live test) blocks the drop, pinned by test. file: `src/gobby/runner_maintenance/storage_hygiene.py`.

    2.4.2: Creation-time validation rejects labels breaking the 6-part contract; fixtures
    acquire the lease at creation. test: `tests/fixtures/test_postgres_safety.py`.

    2.4.3: Hub has zero leaked schemas post-cleanup. behavior: "psql schema list clean"
    in session transcript.'
  labels:
  - covers:gcore-schema-authority:2.4:2.4.1
  - covers:gcore-schema-authority:2.4:2.4.2
  - covers:gcore-schema-authority:2.4:2.4.3
  tdd: true
  source_section: '2.4'
  implementation_domain: backend
- title: Delete dead Python files
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '2.5.1: 9 files + shim tests deleted; import graph clean (`uv
    run mypy src/` + daemon boot). behavior: "daemon starts clean" in session transcript.

    2.5.2: stages.py endpoints still serve. test: `tests/servers/routes/test_stage_routes.py`.'
  labels:
  - covers:gcore-schema-authority:2.5:2.5.1
  - covers:gcore-schema-authority:2.5:2.5.2
  tdd: true
  source_section: '2.5'
  implementation_domain: backend
- title: Remove postgres-activate ritual + SQLite residue
  category: code
  task_type: feature
  depends_on:
  - '0.4'
  - '0.5'
  - '2.3'
  validation_criteria: '2.6.1: `gobby postgres activate` gone; `_pgaudit_probe` dropped.
    file: `src/gobby/cli/postgres.py`.

    2.6.2: bootstrap.yaml schema has neither `database_path` nor `hub_backend`; Rust
    reads `database_url` directly. symbol: `parse_hub_database_bootstrap`.

    2.6.3: gcode DSN resolution works against the trimmed bootstrap. behavior: "gcode
    search succeeds post-reinstall" in session transcript.

    2.6.4: Focused pytest + cargo check green. test: `tests/config/` focused run.'
  labels:
  - covers:gcore-schema-authority:2.6:2.6.1
  - covers:gcore-schema-authority:2.6:2.6.2
  - covers:gcore-schema-authority:2.6:2.6.3
  - covers:gcore-schema-authority:2.6:2.6.4
  tdd: true
  source_section: '2.6'
  implementation_domain: backend
- title: Remove no-op CLI/API surfaces
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '2.7.1: All six surfaces removed or corrected; CLI/API help
    output clean. file: `src/gobby/cli/build.py`.

    2.7.2: Focused route/CLI tests green. test: `tests/cli/` focused run.'
  labels:
  - covers:gcore-schema-authority:2.7:2.7.1
  - covers:gcore-schema-authority:2.7:2.7.2
  tdd: true
  source_section: '2.7'
  implementation_domain: backend
- title: Remove dead config fields incl. CodeIndexConfig disposition
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '2.8.1: Enumerated sets removed; config loads; flatten_config
    emits no orphan keys. symbol: `DaemonConfig`.

    2.8.2: Six dead CodeIndexConfig fields removed; web settings form updated in lockstep;
    kept-field consumer trace recorded. behavior: "per-field disposition" in session
    transcript.

    2.8.3: Focused config tests green. test: `tests/config/` focused run.'
  labels:
  - covers:gcore-schema-authority:2.8:2.8.1
  - covers:gcore-schema-authority:2.8:2.8.2
  - covers:gcore-schema-authority:2.8:2.8.3
  tdd: true
  source_section: '2.8'
  implementation_domain: backend
- title: Remove legacy config-chain migration paths
  category: code
  task_type: feature
  depends_on:
  - '2.8'
  validation_criteria: '2.9.1: 9 config-chain paths removed with per-item hub-state
    evidence. behavior: "per-item evidence ledger" in session transcript.

    2.9.2: Config loads; daemon boots clean. test: `tests/config/` focused run.

    2.9.3: Kept-adjacent ledger recorded (load chain, wiki section, live default seeding).
    behavior: "2.9 kept ledger" in session transcript.'
  labels:
  - covers:gcore-schema-authority:2.9:2.9.1
  - covers:gcore-schema-authority:2.9:2.9.2
  - covers:gcore-schema-authority:2.9:2.9.3
  tdd: true
  source_section: '2.9'
  implementation_domain: backend
- title: Consolidate duplicate utilities, _sanitize_url first
  category: code
  task_type: feature
  depends_on:
  - '2.5'
  validation_criteria: '2.10.1: Single `sanitize_url` with query/fragment stripping
    + IPv6 re-bracketing; both call sites migrated. symbol: `sanitize_url`.

    2.10.2: All four named families each resolve to one canonical implementation with
    duplicates deleted; per-family site evidence + the kept-adjacent corrections recorded.
    behavior: "duplicate sweep results" in session transcript.

    2.10.3: Focused tests green. test: `tests/clones/` focused run.'
  labels:
  - covers:gcore-schema-authority:2.10:2.10.1
  - covers:gcore-schema-authority:2.10:2.10.2
  - covers:gcore-schema-authority:2.10:2.10.3
  tdd: true
  source_section: '2.10'
  implementation_domain: backend
- title: Consolidate webhook stacks onto httpx; drop aiohttp
  category: code
  task_type: feature
  depends_on:
  - '2.10'
  validation_criteria: '2.11.1: Pipeline webhooks deliver via the shared transport
    with unchanged payload behavior; SSRF-blocking + pinning tests green. test: `tests/workflows/`
    webhook-focused run.

    2.11.2: `aiohttp` absent from pyproject and `uv.lock`; zero imports remain. file:
    `pyproject.toml`.

    2.11.3: Dispatcher rides the same transport: fail-closed blocking semantics preserved,
    backoff capped, private addresses still allowed for local endpoints. test: `tests/hooks/`
    webhook-focused run.

    2.11.4: Transport edge semantics pinned by tests (Codex review): TLS SNI + certificate
    hostname verification against the ORIGINAL hostname when connecting via pinned
    IP; multi-address DNS answers all validated and pinned; response cap enforced
    mid-stream on chunked bodies; retries limited to idempotent methods (a non-idempotent
    request retries only when provably never sent); hostile `HTTP_PROXY`/`HTTPS_PROXY`/
    `ALL_PROXY` environment variables never route a pinned request through a proxy
    (trust_env=False pinned by test). test: `tests/utils/` webhook-transport focused
    run.'
  labels:
  - covers:gcore-schema-authority:2.11:2.11.1
  - covers:gcore-schema-authority:2.11:2.11.2
  - covers:gcore-schema-authority:2.11:2.11.3
  - covers:gcore-schema-authority:2.11:2.11.4
  tdd: true
  source_section: '2.11'
  implementation_domain: backend
- title: Remove memory-dream merge arm + _legacy fallbacks
  category: code
  task_type: feature
  depends_on:
  - '0.4'
  - '0.5'
  - '2.19'
  validation_criteria: '2.12.1: Manifest symbols removed; live pipeline untouched;
    dream run completes post-change. behavior: "successful dream run" in daemon logs
    or `dream_runs` row in session transcript.

    2.12.2: Dream suites green without `_legacy` fakes. test: `tests/memory/test_dream.py`.

    2.12.3: CHECK constraints match the live action set; affected runs purged whole
    with row-count evidence and stamped `revert_forfeited`; `revert_dream_run` fails
    closed on forfeited runs and replays untouched runs normally. test: `tests/memory/`
    revert-focused run.

    2.12.4: `revert_forfeited` is in `RUN_TERMINAL_STATUSES`; admission, display,
    pruning, and repeated-revert paths handle it; exhaustiveness tests cover the full
    vocabulary. symbol: `RUN_TERMINAL_STATUSES`.'
  labels:
  - covers:gcore-schema-authority:2.12:2.12.1
  - covers:gcore-schema-authority:2.12:2.12.2
  - covers:gcore-schema-authority:2.12:2.12.3
  - covers:gcore-schema-authority:2.12:2.12.4
  tdd: true
  source_section: '2.12'
  implementation_domain: backend
- title: 'TTS: remove voice=[] extra; pin no-torch-at-import'
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '2.13.1: `voice = []` extra gone; chatterbox-tts + pins present.
    file: `pyproject.toml`.

    2.13.2: No-torch-at-import regression test. test: `tests/voice/test_lazy_import.py`.'
  labels:
  - covers:gcore-schema-authority:2.13:2.13.1
  - covers:gcore-schema-authority:2.13:2.13.2
  tdd: true
  source_section: '2.13'
  implementation_domain: backend
- title: Remove dead Rust mediawiki/wayback + dead deps
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '2.14.1: Files + deps gone; workspace builds; clippy clean
    per crate. behavior: "cargo check + clippy per crate" in session transcript.

    2.14.2: public_boundary contract test updated and green. test: `crates/gcore/tests/public_boundary.rs`.

    2.14.3: gwiki ingest suites green. behavior: "cargo test -p gobby-wiki" in session
    transcript.'
  labels:
  - covers:gcore-schema-authority:2.14:2.14.1
  - covers:gcore-schema-authority:2.14:2.14.2
  - covers:gcore-schema-authority:2.14:2.14.3
  tdd: true
  source_section: '2.14'
  implementation_domain: backend
- title: Remove dead web modules + dead deps; Python dead deps
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '2.15.1: Web builds clean with modules + deps removed. behavior:
    "npm run build" in session transcript.

    2.15.2: Python deps trimmed; `uv sync` + daemon boot green. file: `pyproject.toml`.'
  labels:
  - covers:gcore-schema-authority:2.15:2.15.1
  - covers:gcore-schema-authority:2.15:2.15.2
  tdd: true
  source_section: '2.15'
  implementation_domain: backend
- title: Retire template-registry chaff, DB registry verified
  category: config
  task_type: feature
  depends_on: []
  validation_criteria: '2.16.1: Registry rows retired with pre-verification evidence
    per row. behavior: "DB registry verification ledger" in session transcript.

    2.16.2: rules CLAUDE.md accurate against DB. file: `src/gobby/install/shared/workflows/rules/CLAUDE.md`.

    2.16.3: gcode SKILL.md regenerated. file: `src/gobby/install/shared/skills/gcode/SKILL.md`.'
  labels:
  - covers:gcore-schema-authority:2.16:2.16.1
  - covers:gcore-schema-authority:2.16:2.16.2
  - covers:gcore-schema-authority:2.16:2.16.3
  tdd: true
  source_section: '2.16'
  assigned_agent: backend-developer
- title: Decompose the 3 oversized gcode files
  category: refactor
  task_type: feature
  depends_on: []
  validation_criteria: '2.17.1: All three files < 1,000 lines post-split; no new clippy
    warnings. behavior: "line counts + clippy" in session transcript.

    2.17.2: Out-of-line test convention documented. file: `crates/CLAUDE.md`.

    2.17.3: gcode suites green. behavior: "cargo test -p gobby-code" in session transcript.'
  labels:
  - covers:gcore-schema-authority:2.17:2.17.1
  - covers:gcore-schema-authority:2.17:2.17.2
  - covers:gcore-schema-authority:2.17:2.17.3
  tdd: false
  source_section: '2.17'
  assigned_agent: backend-developer
- title: 'Machine identity: machines.id UUID PK'
  category: code
  task_type: feature
  depends_on:
  - '2.21'
  - '2.6'
  - '0.4'
  - '0.5'
  - '0.7'
  - '0.6'
  validation_criteria: "2.18.1: Generator is uuid4-only; `machineid` import and dep\
    \ gone. symbol: `_generate_machine_id`.\n2.18.2: Migration 361 ships `machines.id\
    \ UUID PK` (TEXT column gone) + `bin_update_state` re-key; dual-shape safe; destructive-marked.\
    \ file: `src/gobby/storage/migrations/361_machines_uuid_identity.sql`.\n2.18.3:\
    \ Staged cutover verified: inventory ledger, live-machine remap (machines row\
    \ + rewritten local file agree), non-live rows retired with sessions NULLed and\
    \ one `retired_machine_identities` tombstone per retired id, zero-unmapped gate\
    \ passes. behavior: \"cutover ledger + gate evidence\" in session transcript.\n\
    2.18.4: Fresh-home boot generates uuid4 and registers it in `machines` at daemon\
    \ startup. behavior: \"fresh-home boot check\" in session transcript.\n2.18.5:\
    \ Unpack skips identity by default; `--restore-identity` restores it. behavior:\
    \ \"pack/unpack round-trip\" in session transcript.\n2.18.6: Cutover journal resumes\
    \ from every phase AND per identity (a partially retired inventory resumes where\
    \ it stopped); fault-injection at each DB/file boundary green; 361's guard refuses\
    \ a half-remapped machine; the 360 \u2192 cutover-NULLs \u2192 361 \u2192 362\
    \ intermediate sequence is exercised end-to-end; a tombstoned identity file re-keys\
    \ fresh at boot. test: `tests/storage/` identity-cutover focused run.\n2.18.7:\
    \ Preflight fails on foreign `pg_stat_activity` connections; 360's deny-all fence\
    \ (activated under table locks before preflight) rejects an old writer reconnecting\
    \ mid-window AND a uuid-shaped legacy writer (tests); re-inventory runs after\
    \ activation; identity file flocked; identity-file replacement fsyncs file + parent\
    \ dir and readback-verifies before the journal advances; identity file + checksum\
    \ present in the cutover backup manifest; restore-based rollback documented. behavior:\
    \ \"quiescence + fence + rollback evidence\" in session transcript."
  labels:
  - covers:gcore-schema-authority:2.18:2.18.1
  - covers:gcore-schema-authority:2.18:2.18.2
  - covers:gcore-schema-authority:2.18:2.18.3
  - covers:gcore-schema-authority:2.18:2.18.4
  - covers:gcore-schema-authority:2.18:2.18.5
  - covers:gcore-schema-authority:2.18:2.18.6
  - covers:gcore-schema-authority:2.18:2.18.7
  tdd: true
  source_section: '2.18'
  implementation_domain: backend
- title: 'Sessions machine attribution: UUID FK + sentinel policy'
  category: code
  task_type: feature
  depends_on:
  - '2.18'
  - '0.6'
  - '0.4'
  - '0.5'
  validation_criteria: '2.19.1: Migration 362 converts the column, rebuilds `idx_sessions_unique`
    NULLS NOT DISTINCT, maps sentinels to NULL, adds the FK; destructive- marked.
    file: `src/gobby/storage/migrations/362_sessions_machine_uuid_fk.sql`.

    2.19.2: Registration idempotency holds for machine-attributed and NULL-machine
    sessions. test: `tests/storage/` sessions-focused run.

    2.19.3: Web-chat session create and pipeline session lookup work post-change.
    test: `web/src/hooks/__tests__/` + `tests/workflows/` focused runs.

    2.19.4: identity-model.md reflects the UUID contract. file: `docs/contracts/identity-model.md`.

    2.19.5: M0 prerequisite verified: the registered M0 artifact is 0.6''s amended
    UUID-native version (post-364 slots, no TEXT machine_id); collision preflight
    ledger recorded (zero expected). behavior: "M0 amendment verification + collision
    ledger" in session transcript.

    2.19.6: Child-table policy exercised: a synthetic duplicate group with one-row-per-session
    children (session_variables), multi-row children, and a parent_session_id self-reference
    merges per the recorded order without constraint violations; the FK/uniqueness
    inventory is emitted from pg_constraint. test: `tests/storage/` survivor-merge
    focused run.'
  labels:
  - covers:gcore-schema-authority:2.19:2.19.1
  - covers:gcore-schema-authority:2.19:2.19.2
  - covers:gcore-schema-authority:2.19:2.19.3
  - covers:gcore-schema-authority:2.19:2.19.4
  - covers:gcore-schema-authority:2.19:2.19.5
  - covers:gcore-schema-authority:2.19:2.19.6
  tdd: true
  source_section: '2.19'
  implementation_domain: backend
- title: project.json contributor flow + untrack state JSONLs
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '2.20.1: Fresh-clone simulation: gcode works, `gobby init`
    registers the row and indexes, working tree stays clean. behavior: "fresh-clone
    walkthrough" in session transcript.

    2.20.2: State JSONLs untracked; pre-push makes no auto-commit; local backup written
    at push. behavior: "push dry-run" in session transcript.

    2.20.3: `ensure_exists` targeted; name collision fails loudly. test: `tests/storage/`
    projects-focused run.

    2.20.4: Non-portable keys never re-emitted; file mode preserved. test: `tests/utils/`
    project_init-focused run.'
  labels:
  - covers:gcore-schema-authority:2.20:2.20.1
  - covers:gcore-schema-authority:2.20:2.20.2
  - covers:gcore-schema-authority:2.20:2.20.3
  - covers:gcore-schema-authority:2.20:2.20.4
  tdd: true
  source_section: '2.20'
  implementation_domain: backend
- title: Remove legacy secrets/auth migration paths
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '2.21.1: Fernet apparatus + migrate CLI + auth/webhook migrators
    removed with hub-state evidence. behavior: "per-item evidence" in session transcript.

    2.21.2: Secrets store works post-Fernet-removal. test: `tests/storage/test_secrets.py`.

    2.21.3: Kept-adjacent ledger recorded (live crypto path, CRUD CLI, inbound verifiers,
    auth verification). behavior: "2.21 kept ledger" in session transcript.'
  labels:
  - covers:gcore-schema-authority:2.21:2.21.1
  - covers:gcore-schema-authority:2.21:2.21.2
  - covers:gcore-schema-authority:2.21:2.21.3
  tdd: true
  source_section: '2.21'
  implementation_domain: backend
- title: Remove legacy installer/hook migration paths
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '2.22.1: 6 paths removed; keeps recorded. behavior: "evidence
    ledger" in session transcript.

    2.22.2: Install/uninstall flows green. test: `tests/cli/installers/` focused run.'
  labels:
  - covers:gcore-schema-authority:2.22:2.22.1
  - covers:gcore-schema-authority:2.22:2.22.2
  tdd: true
  source_section: '2.22'
  implementation_domain: backend
- title: Remove legacy data-shape paths + gated github uuid seeds
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '2.23.1: Data-shape paths removed with hub-state evidence;
    gate decision recorded either way. behavior: "evidence + gate record" in session
    transcript.

    2.23.2: Wiki cron + build-profile suites green. test: `tests/wiki/` + `tests/storage/`
    focused runs.

    2.23.3: Kept-adjacent ledger recorded (live wiki jobs, current row-hash path,
    github sync machinery). behavior: "2.23 kept ledger" in session transcript.'
  labels:
  - covers:gcore-schema-authority:2.23:2.23.1
  - covers:gcore-schema-authority:2.23:2.23.2
  - covers:gcore-schema-authority:2.23:2.23.3
  tdd: true
  source_section: '2.23'
  implementation_domain: backend
- title: 'Detection profiles: agy rename + grok.toml + manifest regen'
  category: config
  task_type: feature
  depends_on:
  - '2.16'
  validation_criteria: '2.24.1: `agy.toml` + `grok.toml` resolve at runtime with 14/14
    rule ids; grok rules verified against a live pane capture; bundled-content manifest
    regenerated. behavior: "manifest resolution check" in session transcript.'
  labels:
  - covers:gcore-schema-authority:2.24:2.24.1
  tdd: true
  source_section: '2.24'
  assigned_agent: backend-developer
- title: Delete scratch files + doc fixes
  category: config
  task_type: feature
  depends_on: []
  validation_criteria: '2.25.1: Scratch files gone; doc fixes applied. file: `docs/guides/release-guide.md`.

    2.25.2: All 5 stubs + 31 dumps deleted; the named kept files remain; kept ledger
    recorded. behavior: "2.25 kept/deleted ledger" in session transcript.'
  labels:
  - covers:gcore-schema-authority:2.25:2.25.1
  - covers:gcore-schema-authority:2.25:2.25.2
  tdd: true
  source_section: '2.25'
  assigned_agent: backend-developer
- title: Redirect rule_eval telemetry; session-variable expiry
  category: code
  task_type: feature
  depends_on:
  - '0.3'
  - '0.8'
  validation_criteria: '5.1.1: Allow-outcome rule evals write no `metrics_events`
    rows; they appear in the rotating surface log and as Prometheus-exposed counters/histograms;
    block rows unchanged. symbol: `_run_rule_loop`.

    5.1.2: `SESSION_REVIVAL_HORIZON_HOURS` enforced in every revival path; expiry
    sweep clears variables past that identical horizon only; abandoned `pending_interactions`
    rows reach a terminal status. test: `tests/storage/` session-lifecycle focused
    run.

    5.1.3: `cleanup_old_metrics` atomic; `reset_metrics` filtered. test: `tests/mcp_proxy/`
    metrics focused run.

    5.1.4: Allow-audit surface retention sized in days with recorded math; delivery
    probe shows allow lines in the new log; write-failure path degrades to counter
    + warning without blocking evals; queue overflow drops newest with a counted metric
    and the shutdown deadline is enforced (both pinned by test); the log files appear
    in the hub-backup manifest with checksums (Codex review). test: `tests/workflows/`
    telemetry-focused run.'
  labels:
  - covers:gcore-schema-authority:5.1:5.1.1
  - covers:gcore-schema-authority:5.1:5.1.2
  - covers:gcore-schema-authority:5.1:5.1.3
  - covers:gcore-schema-authority:5.1:5.1.4
  tdd: true
  source_section: '5.1'
  implementation_domain: backend
- title: One-time purge + space reclaim, fresh-backup gated
  category: code
  task_type: feature
  depends_on:
  - '5.1'
  - '0.4'
  - '0.7'
  validation_criteria: '5.2.1: Fresh backup manifest immediately precedes the purge
    run. behavior: "pre-purge backup manifest" in session transcript.

    5.2.2: Purge script + per-table pre/post ledger recorded; only enumerated categories
    touched. file: `scripts/hub_data_purge.sql`.

    5.2.3: Post-purge smoke: daemon boot, rule-eval writes, session create, pipeline
    run, admin stats endpoints. behavior: "post-purge smoke" in session transcript.

    5.2.4: Phase split honored (DML in transactions, VACUUM outside); epoch + quiescence
    + full-relation/WAL/margin preflight evidence; hub-resident completion receipts
    (`destructive_batches`) support rerun from any machine; size ledger recorded.
    behavior: "size ledger + preflight" in session transcript.

    5.2.5: `gobby hub-purge` owns the orchestration end-to-end as the purge campaign;
    it refuses a predicates file whose sha256 differs from the batch intent row. test:
    `tests/cli/` hub-purge focused run.'
  labels:
  - covers:gcore-schema-authority:5.2:5.2.1
  - covers:gcore-schema-authority:5.2:5.2.2
  - covers:gcore-schema-authority:5.2:5.2.3
  - covers:gcore-schema-authority:5.2:5.2.4
  - covers:gcore-schema-authority:5.2:5.2.5
  tdd: true
  source_section: '5.2'
  implementation_domain: backend
- title: Probe removal + Qdrant/FalkorDB orphan reconciliation
  category: code
  task_type: feature
  depends_on:
  - '0.4'
  - '0.7'
  - '0.3'
  validation_criteria: '5.3.1: Probe project absent from `code_indexed_projects` and
    both projections via `gcode invalidate --project-id --force` (owner CLI; no global
    prune, no direct store deletes). behavior: "targeted deletion evidence" in session
    transcript.

    5.3.2: Four-tier classification ledger recorded (reserved globals kept); orphans
    dropped only via the hash-pinned manifest, executed by owner surfaces (gcode for
    `code_symbols_*`, gwiki for topics, the recall cleanup command for recall/debug
    graphs); live surfaces green (gcode search, wiki search, memory recall smoke).
    behavior: "reconcile ledger + smoke" in session transcript.

    5.3.3: Benchmark/debug graph leak has a teardown fix; a fresh benchmark run leaves
    no new graph behind. test: recall benchmark harness focused run.

    5.3.4: gcode SKILL.md + bundled-content manifest regenerated after the CLI change.
    behavior: "regen evidence" in session transcript.

    5.3.5: Apply preceded by a fresh backup whose inventory covers every candidate
    (binding sha recorded); per-target receipts support rerun after partial completion.
    behavior: "coverage + receipt evidence" in session transcript.'
  labels:
  - covers:gcore-schema-authority:5.3:5.3.1
  - covers:gcore-schema-authority:5.3:5.3.2
  - covers:gcore-schema-authority:5.3:5.3.3
  - covers:gcore-schema-authority:5.3:5.3.4
  - covers:gcore-schema-authority:5.3:5.3.5
  tdd: true
  source_section: '5.3'
  implementation_domain: backend
- title: BM25 index verification + reserved disposition slot
  category: code
  task_type: feature
  depends_on:
  - '2.12'
  - '0.3'
  validation_criteria: '5.4.1: Per-index verdict with EXPLAIN evidence recorded. file:
    `docs/evidence/bm25-verification.md`.

    5.4.2: Slot 364 ships either way: destructive-marked drop with proof attached,
    or no-op verdict record on all-stay. file: `src/gobby/storage/migrations/364_bm25_disposition.sql`.'
  labels:
  - covers:gcore-schema-authority:5.4:5.4.1
  - covers:gcore-schema-authority:5.4:5.4.2
  tdd: true
  source_section: '5.4'
  implementation_domain: backend
- title: 'M0-landed gate: cross-epic dependency installed and verified'
  category: config
  task_type: feature
  depends_on:
  - '2.19'
  validation_criteria: '3.0.1: Cross-epic dependency installed; M0 slots applied on
    the hub with filename/checksum bookkeeping recorded. behavior: "M0 gate evidence
    (dependency id + psql bookkeeping listing)" in session transcript.'
  labels:
  - covers:gcore-schema-authority:3.0:3.0.1
  tdd: true
  source_section: '3.0'
  assigned_agent: backend-developer
- title: 'Filename-aware migration bookkeeping: enforcement semantics'
  category: code
  task_type: feature
  depends_on:
  - '0.3'
  - '3.0'
  validation_criteria: '3.1.1: Bookkeeping columns + verification in runner; hijack
    scenario test. symbol: `MigrationRunner.apply_pending`.

    3.1.2: Typo''d filename fails hard. test: `tests/storage/test_migration_contract.py`.

    3.1.3: Historical rows keep NULL bookkeeping (no retroactive stamping); contiguity
    is baseline-relative; baseline row carries pseudo-filename + checksum post-flatten.
    test: `tests/storage/test_migration_contract.py`.'
  labels:
  - covers:gcore-schema-authority:3.1:3.1.1
  - covers:gcore-schema-authority:3.1:3.1.2
  - covers:gcore-schema-authority:3.1:3.1.3
  tdd: true
  source_section: '3.1'
  implementation_domain: backend
- title: Flatten migrations into regenerated baseline
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  - '3.0'
  - '0.4'
  - '5.4'
  - '0.7'
  validation_criteria: '3.2.1: Flatten script reproducible; run recorded. file: `scripts/flatten_schema.py`.

    3.2.2: Triple diff clean. behavior: "three-way identical schemas" in session transcript.

    3.2.3: Fresh install + pytest fixture build from new baseline; focused storage
    suites green. test: `tests/storage/hub/test_postgres_baseline_application.py`.

    3.2.4: M0-landed gate recorded (hub `schema_migrations` includes M0 slots pre-flatten).
    behavior: "gate evidence" in session transcript.

    3.2.5: Pre-flatten DDL + seed manifest persisted with hashes before migration
    deletion. file: `docs/evidence/pre-flatten/`.

    3.2.6: Single-transaction bookkeeping cutover under quiescence + lock; crash and
    runner-skew tests green. test: `tests/storage/` flatten-cutover focused run.'
  labels:
  - covers:gcore-schema-authority:3.2:3.2.1
  - covers:gcore-schema-authority:3.2:3.2.2
  - covers:gcore-schema-authority:3.2:3.2.3
  - covers:gcore-schema-authority:3.2:3.2.4
  - covers:gcore-schema-authority:3.2:3.2.5
  - covers:gcore-schema-authority:3.2:3.2.6
  tdd: true
  source_section: '3.2'
  implementation_domain: backend
- title: Embed schema assets + migration runner in gcore
  category: code
  task_type: feature
  depends_on:
  - '3.2'
  validation_criteria: '4.1.1: Assets embedded; runner applies fresh + idempotent
    re-apply. symbol: `SchemaRunner`.

    4.1.4: Concurrent named-schema applies don''t deadlock; an interrupted non-transactional
    migration (invalid concurrent index) recovers on re-apply. behavior: "lock + recovery
    tests" in cargo test output.

    4.1.2: Lockstep guard fails an older binary against a newer DB. behavior: "guard
    test" in cargo test output.

    4.1.3: Feature gating keeps ghook/build.rs free of postgres. behavior: "cargo
    tree -p gobby-hooks" in session transcript.

    4.1.5: Destructive-gate parity: a destructive-marked migration halts default apply
    in the gcore runner; the gated path applies. behavior: "gate tests" in cargo test
    output.

    4.1.6: `schema verify` detects catalog drift (dropped column/index/ constraint),
    seed drift, and bookkeeping drift in a scratch schema. behavior: "verify contract
    tests" in cargo test output.'
  labels:
  - covers:gcore-schema-authority:4.1:4.1.1
  - covers:gcore-schema-authority:4.1:4.1.4
  - covers:gcore-schema-authority:4.1:4.1.2
  - covers:gcore-schema-authority:4.1:4.1.3
  - covers:gcore-schema-authority:4.1:4.1.5
  - covers:gcore-schema-authority:4.1:4.1.6
  tdd: true
  source_section: '4.1'
  implementation_domain: backend
- title: Create gobby-daemon crate with gdaemon schema CLI
  category: code
  task_type: feature
  depends_on:
  - '4.1'
  validation_criteria: '4.2.1: `gdaemon schema apply` builds a fresh schema identical
    to the baseline. behavior: "apply + diff vs baseline" in session transcript.

    4.2.2: `--schema` applies into a named schema. behavior: "scratch-schema apply"
    in session transcript.

    4.2.3: Binary installed; version sidecar written. file: `docs/guides/release-guide.md`.

    4.2.4: Malicious `--schema` identifiers rejected; no DSN on argv; credentials
    redacted in errors/logs. behavior: "hardening tests" in cargo test output.

    4.2.5: `schema version --json` emits the six-field identity incl. `assets_root_hash`;
    the install receipt records observed provenance only. behavior: "identity output
    + receipt" in session transcript.

    4.2.6: `schema apply` with a mismatched expected identity refuses before connecting;
    binary replacement between a preflight check and apply is caught by the same-process
    comparison. behavior: "identity-enforcement tests" in cargo test output.'
  labels:
  - covers:gcore-schema-authority:4.2:4.2.1
  - covers:gcore-schema-authority:4.2:4.2.2
  - covers:gcore-schema-authority:4.2:4.2.3
  - covers:gcore-schema-authority:4.2:4.2.4
  - covers:gcore-schema-authority:4.2:4.2.5
  - covers:gcore-schema-authority:4.2:4.2.6
  tdd: true
  source_section: '4.2'
  implementation_domain: backend
- title: Switch Python to gdaemon; delete Python migration machinery
  category: code
  task_type: feature
  depends_on:
  - '4.2'
  validation_criteria: "4.3.1: Daemon startup applies schema via gdaemon; boot green.\
    \ behavior: \"daemon start log showing gdaemon apply\" in session transcript.\n\
    4.3.2: Python migration machinery deleted; no `apply_migrations` SQL left in Python.\
    \ file: `src/gobby/storage/migrations.py`.\n4.3.3: Focused pytest builds schemas\
    \ through gdaemon. test: `tests/storage/hub/test_postgres_baseline_application.py`\
    \ (successor).\n4.3.4: Missing-binary failure mode is actionable. test: `tests/storage/`\
    \ shell-out contract test.\n4.3.5: Fresh install on a clean home provisions gdaemon\
    \ before the first migration apply. behavior: \"fresh-install ordering check\"\
    \ in session transcript.\n4.3.6: Handshake fails closed: new-Python + old-gdaemon\
    \ against an empty DB; mutated intermediate migration (root-hash mismatch); old-binary\
    \ + old-sidecar skew. test: `tests/storage/` handshake focused run.\n4.3.7: `schema_diff.py`\
    \ builds its fresh reference through gdaemon. behavior: \"diff harness run post-cutover\"\
    \ in session transcript.\n4.3.8: Decoy-DB test: explicit-conninfo database object\
    \ + divergent bootstrap default \u2192 gdaemon applies to the object's DB only.\
    \ test: `tests/storage/` DSN-pinning focused run.\n4.3.9: `gobby schema` survives\
    \ the runner deletion as a gdaemon wrapper with unchanged gate semantics. test:\
    \ `tests/cli/` schema focused run.\n4.3.10: Full install/upgrade fails (not warns)\
    \ when gdaemon cannot be installed, verified, or complete the initial apply. test:\
    \ `tests/cli/installers/` focused run."
  labels:
  - covers:gcore-schema-authority:4.3:4.3.1
  - covers:gcore-schema-authority:4.3:4.3.2
  - covers:gcore-schema-authority:4.3:4.3.3
  - covers:gcore-schema-authority:4.3:4.3.4
  - covers:gcore-schema-authority:4.3:4.3.5
  - covers:gcore-schema-authority:4.3:4.3.6
  - covers:gcore-schema-authority:4.3:4.3.7
  - covers:gcore-schema-authority:4.3:4.3.8
  - covers:gcore-schema-authority:4.3:4.3.9
  - covers:gcore-schema-authority:4.3:4.3.10
  tdd: true
  source_section: '4.3'
  implementation_domain: backend
- title: Retire runtime ensure-DDL from production Python
  category: code
  task_type: feature
  depends_on:
  - '4.3'
  - '2.4'
  - '0.8'
  validation_criteria: '4.4.1: All six sites removed and the 2.4 sweep re-homed to
    gdaemon; daemon boot, a dream run, and stage-registry paths run without issuing
    DDL. behavior: "boot + dream run post-removal" in session transcript.

    4.4.2: Audit shows zero persistent DDL of ANY object kind (tables, indexes, schemas,
    types, functions, triggers, sequences, constraints, extensions, views) against
    PostgreSQL in production Python; kept-adjacent ledger recorded. behavior: "DDL
    sweep output" in session transcript.

    4.4.3: `memory_dream_truth_state` present in the gcore baseline; dream truth-state
    reads/writes work post-removal. test: `tests/memory/` focused run.

    4.4.4: Former `ensure_table` test callers build via gdaemon fixtures. test: `tests/dispatch/`
    + `tests/storage/tasks/` focused runs.'
  labels:
  - covers:gcore-schema-authority:4.4:4.4.1
  - covers:gcore-schema-authority:4.4:4.4.2
  - covers:gcore-schema-authority:4.4:4.4.3
  - covers:gcore-schema-authority:4.4:4.4.4
  tdd: true
  source_section: '4.4'
  implementation_domain: backend
- title: Re-home gcode/gwiki standalone DDL onto gcore
  category: code
  task_type: feature
  depends_on:
  - '4.1'
  validation_criteria: '4.5.1: Canonical shared-table definitions live in gcore; gcode/gwiki
    setup paths source them from gcore with zero independent DDL strings remaining.
    file: `crates/gcore/src/schema/external.rs`.

    4.5.2: Standalone gcode/gwiki setup against a fresh database still works; emitted
    DDL is byte-identical to gcore''s export. behavior: "cargo test -p gobby-code
    + -p gobby-wiki setup suites" in session transcript.

    4.5.3: Baseline adoption seam still classifies and adopts standalone tables. test:
    `tests/storage/hub/test_postgres_baseline_application.py`.'
  labels:
  - covers:gcore-schema-authority:4.5:4.5.1
  - covers:gcore-schema-authority:4.5:4.5.2
  - covers:gcore-schema-authority:4.5:4.5.3
  tdd: true
  source_section: '4.5'
  implementation_domain: backend
- title: 'Close-out: file the follow-up epic for every deferral'
  category: config
  task_type: feature
  depends_on:
  - '4.4'
  - '4.5'
  - '5.2'
  - '5.3'
  - '2.7'
  - '2.9'
  - '2.11'
  - '2.13'
  - '2.14'
  - '2.15'
  - '2.17'
  - '2.20'
  - '2.22'
  - '2.23'
  - '2.24'
  - '2.25'
  validation_criteria: '4.6.1: Follow-up epic exists; retention-policy plan-drafting
    task filed (TTL/cadence for metrics_events, token_events, loop_progress, step_executions,
    spans volume, recall_* growth). behavior: "task id in close-out ledger" in session
    transcript.

    4.6.2: Hub-PC datastore-move plan-drafting task filed. behavior: "task id in close-out
    ledger" in session transcript.

    4.6.3: Shared-daemon / feature-crate build-out plan-drafting task filed (two-daemon-hub
    roadmap). behavior: "task id in close-out ledger" in session transcript.

    4.6.4: Hot-path perf task filed (projection-cleanup DELETEs + vector-branch enqueue
    bug, triage polling cadence, `gh_triage_build_dispatches.task_id` FK index). behavior:
    "task id in close-out ledger" in session transcript.

    4.6.5: `indexing.extra_excludes` follow-up task filed. behavior: "task id in close-out
    ledger" in session transcript.

    4.6.6: Machine-attribution deferral task filed (attachments blob attribution,
    prune machine-scoping, `code_index_prune_dirty_projects` scoping) with #17435/#17437
    linked, not duplicated. behavior: "task id in close-out ledger" in session transcript.

    4.6.7: Git-history scrub decision task filed (tasks.jsonl / memories.jsonl before
    repo publication). behavior: "task id in close-out ledger" in session transcript.

    4.6.8: Rust inline-test file-size-linter task filed; keep-records (29 `*-steps`
    rows) confirmed task-free; already-filed links recorded (#19365, #19366, #19367-closed).
    behavior: "close-out ledger complete" in session transcript.'
  labels:
  - covers:gcore-schema-authority:4.6:4.6.1
  - covers:gcore-schema-authority:4.6:4.6.2
  - covers:gcore-schema-authority:4.6:4.6.3
  - covers:gcore-schema-authority:4.6:4.6.4
  - covers:gcore-schema-authority:4.6:4.6.5
  - covers:gcore-schema-authority:4.6:4.6.6
  - covers:gcore-schema-authority:4.6:4.6.7
  - covers:gcore-schema-authority:4.6:4.6.8
  tdd: true
  source_section: '4.6'
  assigned_agent: backend-developer
```
