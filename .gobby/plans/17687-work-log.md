# #17687 Work Log — defect & functionality-gap burn-down (wiki + agent infra)

Append-only log of task work under epic #17713/#17687 and the freeze-safe side
backlog. One entry per task closure, written **before** `close_task`.

Entry format:

```markdown
## YYYY-MM-DD HH:MM UTC — #NNNN — <title> — CLOSED
- Changes: <what changed, key files>
- Commits: <sha(s)>
- Validation: <tests run, results, lint/type checks>
- Deferred: <follow-up tasks filed, or "none">
```

---

## 2026-07-10 16:40 UTC — baseline at log creation

- Bakeoff #17531: arm S (sonnet@xhigh) running since 00:00:27Z, pid 26649,
  ~2,735/2,884 docs, module phase, ETA ~17:30–18:00 UTC. gcode binary FROZEN
  (no reinstalls, no daemon restarts) until arm G completes.
- Closed earlier today: #17780 (6590bfe31), #17781 (68c78de88), #17782 (23a61c9ee).
- #17731: fix committed (1ce443cff), validation blocked on post-arm-G gcode
  rebuild + vault recovery run. Claimed by session #7921.
- #17802 (proxy call_tool type-checking): being wrapped up in another session.
- #17804 (topic-compile overwrite guard): moved under #17687; claimed by
  session f2aabc4c at 16:33 UTC.
- #17532/#17533/#17534: sequencing deps on #17531 (and #17529 for #17532)
  removed at 16:37 UTC — all three ready. Source-only work is freeze-safe;
  binaries deploy at the post-arm-G rebuild.

## 2026-07-10 17:25 UTC — #17534 — Collapse generate_hierarchical_docs_with_* wrapper chain — CLOSED

- Changes: replaced the seven `generate_hierarchical_docs_with_*` overloads +
  `generate_hierarchical_docs_core` in
  `crates/gcode/src/commands/codewiki/generation.rs` with one
  `generate_hierarchical_docs(input, GenerateDocsOptions, emit)` entry point
  (options struct with manual `Default` mirroring the old AI-off/test path).
  `run.rs` production call converted to a struct literal;
  `#[cfg(test)] collect_docs`/`collect_doc_pairs` helpers added in `tests.rs`;
  ~110 test call sites across 14 files rewritten (scripted + hand-converted
  `_core` sites). mod.rs exports reduced to one `#[cfg(test)] pub(crate) use`.
- Commits: 01557fd15
- Validation: `cargo test -p gobby-code` 997 passed / 1 failed —
  the single failure (`features::every_entry_resolves_to_a_handler_file...`,
  gwiki `pages` handler) is caused by session f2aabc4c's uncommitted
  `gwiki.contract.json` diff adding the new `pages` command mid-flight; it is
  independent of this refactor (catalog resolution code untouched).
  `cargo clippy -p gobby-code --tests` clean; `cargo fmt -p gobby-code --check`
  clean; `gobby test-quality audit` on touched test paths: 145 tests scanned,
  0 issues, 0 new vs baseline.
- Deferred: none. NOTE: binary NOT reinstalled (bakeoff freeze); deploys with
  the post-arm-G rebuild.
- Follow-up fix in same close (553a70bc2): #17752 had added the gwiki `pages`
  command to the contract without extending gcode's `resolve_gwiki_handler`
  map, breaking the feature-catalog handler test at HEAD. Mapped
  `pages -> crates/gwiki/src/commands/pages.rs :: commands::pages::execute`.
  Final validation: `cargo test -p gobby-code` fully green — 998 lib tests +
  all integration suites, 0 failed; clippy + fmt clean.

## 2026-07-10 18:50 UTC — #17532 — Bounded generation concurrency: --max-workers, default 1 — CLOSED

- Changes: `gcode codewiki --max-workers <N>` (default 1, `positive_usize`).
  1 maps to `file_workers: None` — the exact pre-change sequential path. N>1
  fans per-file doc builds out to N `std::thread::scope` workers via new
  `GenerateDocsOptions::file_workers` (`FileGenerationWorkers`). Resolved
  generator/verifier closures became `Fn + Send + Sync`
  (`SyncTextGenerator`/`SyncTextVerifier`, warn-once flags → `AtomicBool`);
  serial sites adapt via local `FnMut` wrappers. ReusePlan decisions hoisted
  to `resolve_file_reuse`, resolved serially in file order in both modes;
  workers funnel progress/docs over mpsc and the main thread emits strictly
  in file order (DocSink writes stay serialized + deterministic). Module/
  aggregate/curated generation untouched. In-flight LLM calls stay capped by
  `ai.max_concurrency` transport permits. CLI/dispatch/contract wired;
  pinned `gcode.contract.json` regenerated (+`--max-workers`, purge conflict).
- Commits: c92e52613
- Validation: `cargo test -p gobby-code --lib` 992 passed / 0 failed;
  all 7 integration suites green (46 tests); `cargo clippy -p gobby-code
  --tests` clean; `cargo fmt -p gobby-code --check` clean;
  `gobby test-quality audit` on touched test paths: 0 failing new issues
  (1 medium SLEEP_IN_TEST in `pool_bounds_concurrent_generation_calls` —
  intentional: the sleep creates the overlap window the bounding assertion
  measures; the `peak <= 3` bound holds independent of timing). New tests:
  pooled-vs-serial byte equality incl. emit order, pool-of-1 equivalence,
  in-flight bounding, zero-call reuse through the pool, CLI parse coverage.
- Deferred: none. Binary NOT reinstalled (bakeoff freeze); deploys with the
  post-arm-G rebuild.
- Cross-session note: mid-validation, session #8116's uncommitted #17753
  gwiki contract diff (`page write`/`page delete`, v13) broke the
  feature-catalog handler test in the shared tree — same miss class as
  #17752. Coordinated via P2P message; #8116 took the `resolve_gwiki_handler`
  fix into their commit (their features.rs edit; not in c92e52613). They
  also own a pre-existing gobby-wiki `upkeep_near_duplicate_hit...` failure
  they flagged back.
