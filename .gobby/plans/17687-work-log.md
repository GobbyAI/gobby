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

## 2026-07-10 19:35 UTC — #17533 — Relocate lane_b_dump debug artifacts out of the codewiki output tree — CLOSED

- Changes: Lane B / nav-plan failure dumps relocated off the page surface.
  `run()` now resolves the dump directory once
  (`resolve_lane_b_dump_dir`: `GOBBY_CODEWIKI_LANE_B_DUMP_DIR` scratch-dir
  override, else the live output's `_meta/lane_b/`) and threads it as
  `GenerateDocsOptions.lane_b_dump_dir` through
  `build_curated_navigation_docs` → `render_curated_navigation_docs` →
  `curated_page_body`; `maybe_dump_lane_b_failure` and
  `maybe_dump_nav_failure` take `Option<&Path>` instead of reading the env
  var at the write site. Diagnostics are captured by default on hard-fail
  now (previously lost unless the env var was set); `_meta/` is never
  visited by the `code/`-scoped walker, so dumps are excluded from
  page-count/lint/orphan-GC/ingest surfaces by construction. `None`
  (tests/library callers) disables dumping. No CLI/contract changes.
- Commits: bfe6e38fb
- Validation: `cargo test -p gobby-code` fully green — 996 lib + 109
  integration tests, 0 failed; `cargo clippy -p gobby-code --tests` clean
  (one documented `too_many_arguments` allow on the dump fn, params are the
  dump-artifact sections); `cargo fmt -p gobby-code --check` clean;
  `gobby test-quality audit` on 3 touched test files: 14 tests scanned,
  0 issues, 0 new vs baseline. New tests: resolver default/override/
  blank-override, dump write + `None` no-op, exhausted-nav-plan dump
  end-to-end through the options plumbing, walker exclusion of
  `_meta/lane_b` artifacts.
- Deferred: none. Binary NOT reinstalled (bakeoff freeze); deploys with the
  post-arm-G rebuild. No daemon restarts this task.
- Note: the curated-page Lane B hard-fail dump path is currently
  unreachable in production (curated bodies are Lane A one-shot per
  gobby-cli #1001; `tool_loop` is not forwarded to `curated_page_body`), so
  it is covered by unit tests; the nav-plan dump fires on real Lane A parse
  exhaustion and is covered end-to-end.

## 2026-07-10 19:20 UTC — #17531 bakeoff — arm S end, daemon-auth deploy, freeze superseded

- Arm S (sonnet@xhigh) ran 2026-07-10T00:00:27Z → 18:05:38Z (ARM_S_END
  auto-stamped by the wrapper). It ended with 23/2882 pages `degraded: true`
  and a Lane B repo-overview hard-fail (`model-unavailable`, transport
  errors against `http://localhost:60887/api/llm/generate`), so the
  aggregates after the abort are missing.
- Root cause of the tail failures: the daemon-auth workstream deployed
  mid-window — daemon restarted with auth enabled (up since ~18:17 UTC),
  `~/.gobby/bin/{gcode,ghook}` reinstalled 18:38 UTC and `gwiki` 18:53 UTC
  (not by this session). Arm S's transport died in that restart window.
- Freeze disposition: the gcode binary freeze (no reinstalls until arm G)
  was violated by that deploy, but is also moot — an unauthenticated
  pre-auth binary can no longer reach the auth-enabled daemon, so the
  remaining bakeoff work must run on the current binary. Comparability is
  verified empirically instead: arm-sonnet backed up to
  `arm-sonnet.pre-heal-backup/` before healing; arms O/G must show
  `code/files/**` byte-identical to arm S per the plan's own gate. The
  binary transition (old binary for arm S's surviving aggregates, current
  binary for healed + O/G aggregates) will be disclosed in the evidence
  doc.
- Action: healing arm S by rerunning the exact arm command (reuse
  regenerates only the 23 degraded pages + missing aggregates), then arm O
  → arm G per `/Users/josh/.claude/plans/i-had-the-absolute-modular-pizza.md`.

## 2026-07-11 13:40 UTC — #17823 — Fix codewiki publish link validation flagging wikilinks inside code spans — CLOSED

- Changes: `code_wikilinks` (crates/gcode/src/commands/codewiki/
  publication.rs) now masks fenced code blocks (backtick/tilde fences,
  CommonMark char + length matching) and inline code spans (equal-length
  backtick runs; unmatched runs stay literal) before extracting `[[...]]`
  targets. Quoted wikilink examples inside code no longer fail publish;
  real links still resolve and real broken links still fail closed. Known
  scoped gap: 4-space-indented code blocks are not masked (generated pages
  use fences/inline code; documented here rather than speculatively
  handled).
- Found by: #17531 arm S heal attempt=4 died at publish (exit=1) because
  the regenerated modules.rs.md correctly QUOTED
  `Module: [[code/modules/<file.module>]]` in backticks and the validator
  treated it as a broken generated link, stranding the whole stage.
- Commits: 65288f7ba
- Validation: `cargo test -p gobby-code` fully green — 998 lib tests + all
  integration suites, 0 failed (includes 2 new publish-level tests:
  quoted-links-in-code-spans/fences publish cleanly with no placeholder
  leakage; real broken link after an unmatched backtick still fails).
  `cargo clippy -p gobby-code --tests` clean; `cargo fmt -p gobby-code
  --check` clean; `gobby test-quality audit` on tests/publication.rs:
  0 new issues vs baseline.
- Deferred: none.
- Daemon-restart log (for #17531 evidence doc): cli_restart shutdowns at
  2026-07-11 04:31Z, 07:03Z, 09:28Z, 10:20Z (sender pids 2688/52294/
  90029/78680, none announced); each degraded in-flight heal pages
  (staged degraded peaked 33). Also disabled misconfigured cron
  `gobby:monitor:17731-17806-hourly` (agent_spawn parent = project UUID,
  11 consecutive failures).
- NOTE: binary reinstall required for the fix to reach the heal (freeze
  superseded 2026-07-10); reinstall logged in #17531 evidence narrative.

## 2026-07-11 22:30 UTC — #17848 — Pin canonical spine titles in narrative plan normalization — CLOSED

- Changes: `normalize_narrative_pages` (crates/gcode/src/commands/codewiki/
  build_parts/concepts/plan.rs) pins spine-chapter titles to
  `DEFAULT_CHAPTERS` canon when a model page merges into the spine. On-disk
  slugs derive from titles (`ordinal_narrative_slug`), and the guided tour
  (`default_chapter_links()` via `render_repo_doc` / `append_guided_tour`)
  independently derives slugs from canonical titles — a model re-wording
  ("Overview — Gobby's build pipeline and the gcode tool") moved the page
  to `01-overview-gobby-s-...` while repo.md linked `01-overview`, and
  publish failed closed on the dangling link. Extras keep model titles and
  readable ordinal slugs (existing behavior, still tested).
- Found by: #17531 arm S heal attempt=5 died at publish (exit=1):
  "generated link in code/repo.md has no staged target
  code/narrative/01-overview.md". sonnet@xhigh re-worded spine titles;
  gpt-5.5 default kept them, which is why production never hit it.
- Commits: 835b9c62b
- Validation: `cargo test -p gobby-code` fully green — 1108 tests total
  across lib + integration, 0 failed (new unit test
  `re_titled_spine_chapter_keeps_the_canonical_slug_and_title` asserts the
  normalized spine slugs equal `default_chapter_links()` exactly);
  `cargo clippy -p gobby-code --tests` clean; `cargo fmt --check` clean;
  `gobby test-quality audit` on plan.rs: 0 new issues.
- Deferred: none.
- Also this window: 18:16Z cli_restart attributed via process trap to the
  codex --yolo session (uv run gobby restart, pid 91960 ← codex pid 15045,
  now exited) — 5th uncoordinated restart; urgent P2P sent with evidence.
  Staged degraded 9→19 in that window; heal attempt=6 will converge.

## 2026-07-12 — #18005 CLOSED: ownership page dangling module links (gcode bug #3 found live)

- Arm S heal attempt=6 PUBLISHED (exit=0 at 07:36:36Z, 9h46m; files=2597,
  modules=270, symbols=33302, skipped=1552; degraded=11 from the restart
  saga + a machine-sleep gap). Aggregates verified: repo.md,
  _architecture.md, concepts/ x12, narrative 01..09 canonical short slugs
  (+10/11 deep-dives) — #17823 and #17848 fixes both held.
- Attempt=7 convergence rerun failed at publish (13:35:36Z exit=1):
  "generated link in code/_ownership.md has no staged target
  code/modules/crates/gwiki/src/source_file.md". Root cause: write_modules
  linked raw file_modules cluster names; clustering divergence vs the
  emitted module-page set (synthetic cluster crates/gwiki/src/source_file —
  never a git path — emitted by an earlier run's clustering, dropped by
  this run's) dangles and publish fails closed.
- Fix (#18005, commit e7ee1049d): build_ownership_doc takes the emitted
  module set from module_docs; absent clusters remap to nearest emitted
  ancestor, else repo overview. Tests: 2 new in ownership/tests.rs; 1001
  gobby-code lib tests green; clippy/fmt clean; test-quality audit 0 new.
- Binary transition: gcode rebuilt (release) + reinstalled to
  ~/.gobby/bin/gcode after this commit (4th transition; disclose in
  evidence doc). Heal attempt=8 launched on the fixed binary.
- Daemon restarts this window (all logged for disclosure): 2026-07-12
  00:59:39Z sender_pid=63145 unattributed; 02:11:13Z session #8155
  (claude --dangerously-skip-permissions, caught by ancestry trap, P2P
  sent); 03:27:40Z #8155 again but COORDINATED (gobby-#17908 fix
  activation, single restart, agreed via P2P — I gave go-ahead since a
  converging rerun was already required). Trap b0y4a3sxt stays armed.

## 2026-07-12 ~17:35Z — attempt=8 ABORTED: daemon text-gen outage (quota)

- Heal attempt=8 (post-#18005 binary, START 15:56:54Z) killed at 17:33:33Z
  (SIGTERM, exit=143 logged) after the daemon lost BOTH text-gen
  candidates:
  - claude: subscription weekly limit — "You've hit your weekly limit ·
    resets Jul 13 at 12pm (America/Chicago)" (429, window=seven_day);
    circuit `claude:sonnet` open with ~84.6k-s retry (= the reset).
    First observed 09:37:54 local in tasks.validation calls.
  - codex: OpenAI usage limit — "try again at 1:37 PM" (local, today).
  - ~699 circuit/500 errors in ~25 min of gobby.log (rotated 12:29 local;
    prior log now gobby.log.1 — restart history intact there).
- Why abort instead of letting it run: with claude open until Jul 13
  12:00 CDT, every regenerated page degrades to AST-only fallback (1
  transport-failure fallback already logged in attempt=8), and after
  codex recovers at 13:37 CDT, catch-up file pages would be authored by
  gpt-5.5 inside the SONNET arm — authorship contamination. Live vault
  untouched (staging model): still attempt-6 published state, degraded=11.
- Plan: relaunch as attempt=9 (same command) after the claude weekly
  reset (2026-07-13 12:00 CDT). Bakeoff timeline slips ~23h unless Josh
  provides alternate claude capacity (e.g. API-key billing for daemon
  text-gen) — his call, flagged in session.
- No new daemon restarts (latest remains 22:27:40 local 07-11, #8155
  coordinated). ARM_S_HEAL_ABORT annotation appended to arm-sonnet.log.

## 2026-07-12 ~21:25Z — circuits stale, not quota: restart + attempt=9

- Josh challenged the quota diagnosis ("we have plenty of usage on both").
  He was right. Verification: claude CLI account josh@gamegoblins.com (same
  subscription this session used all day without limits); after a daemon
  restart cleared the in-memory circuit breakers, direct probes through
  /api/llm/generate succeeded for BOTH claude/sonnet and codex/gpt-5.5
  ("OK" responses). The morning 429s ("weekly limit resets Jul 13 12pm")
  were transient/stale — actual upstream cause unconfirmed, but current
  capacity is fine. Correction to the previous entry's assessment.
- DAEMON RESTART (disclose): 2026-07-12 ~21:21Z, sender=this session
  (#7921), purpose: clear open claude:sonnet/codex circuits (retry timers
  ~23.5h) that outlived whatever transient tripped them. Own restart-trap
  caught it as designed.
- Heal attempt=9 launched ~21:25Z ("post-circuit-clear" stamp), same
  command (sonnet@xhigh aggregate candidate), caffeinate armed, trap
  re-armed. Timeline slip from the false quota block: ~4h, not ~23h.
