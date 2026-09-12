# Ask snapshot preparation measurements

Measured on macOS 26.6.2, ARM64, on 2026-09-10 under coordinator #22021.
These are local snapshot preparation measurements. Native Ask runtime security
acceptance and the 14-question cohort remain unrun or unvalidated separately.

The seventh contained native probe failed during source preparation because its
private database lacked the canonical parent index required by a linked worktree.
That harness provisioning defect is tracked under #22018. Independent measurement
also found substantial time spent launching `git cat-file` for individual blobs.

## Fixed source and results

Every measurement used source commit `1d104abbec44a15a5a7ea980a95ea30f8467ce6c`,
tree `ec8b0b733008835076ead4b3cd46e48548c03b30`, and project
`d45545c5-ded5-4335-b115-0245752edacf`.
The complete inventory contained 8,511 entries, with 7,988 eligible files totaling
91,614,922 materialized bytes.

| Binary | Inspect | Materialize, including inspection and verification |
| --- | ---: | ---: |
| Original debug | 113.384 s | 290.943 s |
| Original release | 112.598 s | Not measured |
| Batched debug | 5.118 s | 43.714 s |

These are individual wall-clock observations, not a statistical benchmark.
A two-second macOS process sample during original materialization showed most
samples in Git subprocess spawning or waiting below `Snapshot::read_blob`.
The optimized original binary's inspection time corroborates the subprocess cost.

All four original/batched inspect/materialize JSON outputs were byte-identical:
SHA-256 `1fb5c34cd502c779865513d46be6549ed52015a51d6b3f1746c4a40bb23c11d2`.
Their inventory digest was
`3a0b84f122c6e5e0904bd5a0d0c49fddfa6e91d23320158ed3d725275bec2fd9`.
Materialization retained content verification, exclusions, path checks, and file
syncs. No deadline, inventory, or evidence admission rule was relaxed.

The original release binary SHA-256 was
`325da8b38295dbcf06f1c25704797a09811be5a63f4d2ad4388d12a3f8eac9cc`.
The measured batched debug binary SHA-256 was
`02a985470604661a694759771489742ffb3e56da37a1037d0ffe4b8a57b4f4f3`.
These binaries were built locally; no installed binary or shared daemon changed.

## Method and regression evidence

The benchmark invoked the real `gcode` binary with `--quiet --format json
--project <integration-checkout> evidence --snapshot-json <request>`.
Requests specified `schema_version: 1`, the fixed project/commit above, and
`action: "inspect"` or `action: "materialize"`. Each materialize request named a
fresh empty temporary `target_root`. Python's `time.monotonic()` measured each
subprocess with a 420-second diagnostic timeout; this did not change Ask's
600-second execution deadline.

The integration regression
`snapshot_blob_reads_use_bounded_git_processes` uses actual Git repositories,
duplicate blobs, and a 150 KB blob to exercise pipe-sized output. Before the fix,
the named test failed with 69 blob processes instead of two. After batching,
the same test passed with identical bindings, inventories, and materialized bytes.
The focused evidence library and CLI tests cover exclusions, replacements,
ambient Git settings, missing objects, canonical inventory, and evidence behavior.

Exact commands:

```text
cargo nextest run -p gobby-code --lib -E 'test(snapshot_blob_reads_use_bounded_git_processes)'
cargo nextest run -p gobby-code --lib -E 'test(evidence::tests::)'
cargo nextest run -p gobby-code --test evidence
cargo clippy -p gobby-code --lib -- -D warnings
```

Local raw outputs, timing records, the process sample, and their hash inventory
remain in `/private/var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/gobby-ask-materialize-benchmark-12261-kmhljs52`.
Both temporary materialized trees were removed after verification; `cleanup.json`
records their matching file counts and byte totals. Original probe attempts remain
separate failed evidence and were not overwritten by these measurements.

## Managed sandbox correction

The first batched binary above failed inside the managed SRT sandbox when Ask's
sanitized subprocess environment omitted a writable temporary directory. Capturing
Git stderr in an anonymous temporary file had added a filesystem requirement to
read-only snapshot inspection. The old per-blob binary passed the same isolated
topology regression; the batched binary returned `Operation not permitted`.

A CLI regression with `TMPDIR`, `TMP`, and `TEMP` pointing to a nonexistent directory
reproduced the dependency as `cat-file --batch failed: No such file or directory`.
The correction drains piped stderr concurrently, retains at most 4,096 diagnostic
bytes, and discards the remainder. Both success and failure paths reap the child
and join the reader. It adds no filesystem or sandbox grant.

The 14 focused evidence tests pass with the correction, including the unavailable
temporary-directory case and a wrapper that emits 2 MiB of stderr before serving
each batch. Clippy and the test-quality audit pass. The corrected private debug
binary SHA-256 is
`5e0ae71ef4288bf25437f9965870b5e7b2f0acb6b3215ffa8d69f24f0f9fa831`.
The timings above remain measurements of the first batched binary. Inside the
actual managed SRT worker, the corrected binary passed
`tests/ask/test_native_integration.py::test_real_managed_snapshot_queries_branch_native_gcode`
in 12.30 seconds. The regression exercises private parent indexing, normal overlay
admission, exact committed evidence queries, credential rotation, and recovery.
This validates the corrected snapshot/index path; provider-agent security proof
and the complete cohort remain separate acceptance work.

## Full private index preparation

Contained attempt 9 used integration source
`123c8e86075cf1a0acaf7acf787a5d96f76d6d97` and the corrected binary above. It reached
the original 600-second controller deadline while building the private parent
index, before any provider agent started. Cleanup reported no errors, dropped
the owned schema, removed the runtime, and left no probe process running.

A two-second macOS process sample taken during indexing recorded 679 of 1,381
main-thread samples under `upsert_calls` / `insert_call` and PostgreSQL execution.
The implementation issued one database insertion per call relationship. This is
evidence of a separate indexing bottleneck, not a snapshot batching regression.
The sample and failed attempt remain in
`/tmp/gobby-ask-native-probe-12261-ninth`; its raw export SHA-256 is
`0b94f0f207bc5bcb979088b3e029b442bcc98bb882548c6cee64578e73644485`.

This timeout also exposed a receipt-capture race: the controller checked for the
private index receipt before stopping the worker, which could write its final
receipt during shutdown. The existing attempt cannot recover that missing receipt.
The harness now captures it after process cleanup and before raw export or state
removal. A focused regression verifies that a late failure receipt reaches both
the raw export and retained runtime identity before deletion.

## Integrated call-write fix and release preparation

Commit `95d34054d2ab4d21d8430657873c7adaf8a9128b` replaces per-call inserts
with 500-row `UNNEST` batches. The delete scope, nine stored fields, NULL/sentinel
behavior, unique constraint, inserted-row count, and caller-owned transaction
remain intact. Parent review requested and verified a duplicate across the batch
boundary, empty replacement, and rollback after a late invalid UUID.

The worker reported a same-process, isolated PostgreSQL transaction benchmark
of 10,000 unique calls: 3,900.83 ms for the former loop and 130.42 ms for batching
(29.91 times faster). The temporary comparison helper was removed before the
commit; these are worker diagnostic measurements, not an end-to-end Ask result.
Parent reran the five focused database regressions successfully on integration
`71476b2f79032cb20fcdfa3a8566331884cd2926`.

That integration also includes stage authority commit
`c4daecb0840c212a0b71c239ae4704f41c3d1aba` and main through `4b5262b8b4`.
Parent validation passed 431 focused Python tests and three real managed
snapshot/query/recovery tests using the optimized release `gcode` SHA-256
`7a2f3556b67e919ace7a87003f8474830fc75e04d16a9d3a9e4fea240a212b9d`.
Main through workspace-path fix `323753556f` and OAuth fix `6f21e72c73` was
subsequently merged at `2164568b944d5ca564ef04df862cb8d973cdc960`; 72 focused
spawn/Ask/resume/OAuth checks passed on the combined source.

Fresh `gterm` release preparation exposed two independent toolchain failures.
Zig 0.15.2's SIMD-enabled libc++ build fails with the active macOS 27 SDK;
selecting the installed Xcode macOS 26.5 SDK through `DEVELOPER_DIR` produced
a successful release build. Disabling SIMD exposed an unnormalized static
archive whose `compiler_rt.o` was not eight-byte aligned. The local vendor patch
now routes native Darwin static archives through the existing `ranlib`/`libtool`
combination step even without SIMD. A real build/link regression failed before
the patch and passed afterward while verifying that the runtime member remains.
The development guide records the SDK selection and regression command.

These checks do not establish native provider security or cohort accuracy.
Attempts 1–9 remain unchanged; no shared binary installation has occurred.

## Contained attempt 10

Attempt 10 ran from clean commit `549db0456b405dbf89a99f73b3d64d78c21274a8`
after the other coordinator cleared the isolated run. Both release binaries
were rebuilt with the installed Xcode SDK selected:

- `gcode`: `a95b97258ff1fa2c81674bbbe6da1aad3a1e5b6e16ce32ce8a4d6c46686e4383`
- `gterm`: `a910dd61e2f379ac91362ef492c667fb69b7adf3b9cc95959839ad11d18b7326`

The private parent index completed for 6,722 files. Snapshot materialization took
39,602.931 ms and indexing took 260,994.578 ms. All six bootstrap commands exited
zero, the checkout was restored, and the bootstrap credential was revoked. The
original controller/run deadline remained unchanged. A two-second process sample
during the later index phase was entirely under local-import resolution; it is a
phase observation, not a whole-run performance attribution.

Ask run `266c6783-849b-43e0-b3b9-54a9f51afb5e` completed `prepare`, then failed
at `seed` with `malformed: malformed grant: grant machine does not match local
machine`. No provider agent or launch receipt was created, so this attempt proves
neither native security nor resume behavior. Investigation is tracing the direct
Ask evidence environment against the managed wrapper's `GOBBY_HOME` identity.

The original evidence is retained at `/tmp/gobby-ask-native-probe-12261-tenth`.
Raw export SHA-256:
`ecfe65f7719b2798a0ccdd9e0c7e88fa862f0c2b191c0cf18e5c28534d61b18a`.
Cleanup reported `errors=[]`, complete raw export, private schema dropped,
runtime removed, and worker/host absent. The parent verified that no probe or
pinned `gcode` process remained. This failure remains an owned implementation
finding; the native proof and all 14 cohort questions are still pending.

## Attempt 10 identity correction

The direct evidence process retained only `PATH`, `SYSTEMROOT`, and the prepared
runtime environment. That environment carried the managed grant and
`GOBBY_CODE_INDEX_RUNTIME_HOME`, but omitted `GOBBY_HOME`. The usual shell wrapper
exports the latter; Ask bypasses that wrapper to invoke its pinned native binary.
Consequently, gcore read the host machine identity instead of the contained one.

The existing native integration fixture used the host machine ID in its temporary
home, masking the fallback. Giving it a separate private home and distinct machine
UUID reproduced the exact native error in 2.15 seconds. Snapshot preparation now
requires the managed runtime home and includes it explicitly in the persisted
evidence environment. It does not inherit an ambient home or weaken native grant
validation. An additional real-native check changes the private on-disk identity
and verifies rejection before restoring it and exercising recovery.

Validation used the isolated test hub on port 60892, `GOBBY_TEST_PROTECT=1`,
`UV_PROJECT_ENVIRONMENT=/Users/josh/Projects/gobby/.venv`, `PYTHONPATH=src`,
`uv run --no-sync`, and the unchanged pinned `target/ask-probe-549db04` binaries:

- `pytest tests/ask/test_native_integration.py::test_real_managed_snapshot_queries_branch_native_gcode -q --tb=short`:
  RED with the exact grant/local-machine mismatch.
- `pytest tests/ask/test_native_integration.py tests/ask/test_snapshots.py tests/ask/test_evidence.py -q --tb=short`:
  16 passed in 17.61 seconds after the product fix.
- `pytest tests/ask/test_native_integration.py -q --tb=short`:
  3 passed in 4.51 seconds including the added real-native identity rejection,
  fresh retrieval, recovered retrieval, and rejection of the stale runtime.
- Focused Ruff lint/format and mypy passed. Test quality and test types audits
  reported no issues; the suppression ratchet reported 218 baseline, zero new.

This small regression establishes the identity correction. A new contained probe,
provider security/resume acceptance, installed-service checks, and all 14 cohort
answers remain required; attempt 10 is unchanged.

## Shared environment audit correction

During the foreign coordinator's restart, the shared editable install was found
pointing at this worktree instead of main. Explicit parent Python commands used
`uv run --no-sync`, but `test-types audit` independently launched `uv run mypy`
without that option. Its availability probe already used `--no-sync`; its actual
checker invocation did not. The inherited `UV_PROJECT_ENVIRONMENT` therefore
allowed a nested command to reinstall the worktree into the shared environment.
The user also clarified that another reinstall may have been explicitly requested;
the source defect is confirmed, but attribution of the earlier install is uncertain.

The resolved uv command now includes `--no-sync` for both invocations. A focused
regression exercises `run_mypy` through command resolution and checks both child
processes. It failed on the actual checker invocation before the fix. Protected
`uv run --no-sync pytest tests/test_types/test_audit.py tests/test_types/test_mypy_parser.py -q --tb=short`
then passed 23 tests in 0.17 seconds. Focused Ruff lint/format, mypy, test quality
and test types audits passed.

After the foreign owner restored main's editable install, the fixed real
`gobby test-types audit tests/test_types/test_audit.py --baseline .gobby/test-types-baseline.json --fail-on-new`
reported zero errors. This run used the shared environment and outer `--no-sync`,
without `UV_NO_SYNC`, to verify the corrected nested invocation. Both editable
file hashes stayed identical before and after:

- `__editable__.gobby-0.5.0.pth`:
  `643e4428b9bb063e37c9635792cbd5a89cc55e8e4c1901b7a58175d6b9001d28`
- `gobby-0.5.0.dist-info/direct_url.json`:
  `1b42b219c1dd1733ee9d6ecea8b883055a1ed098bf44c2a19cf135baec0fcb43`

A fresh main-checkout import resolved to
`/Users/josh/Projects/gobby/src/gobby/__init__.py`. Subsequent coordinator commands
also export `UV_NO_SYNC=1` so child tools inherit the intended environment policy.

## Contained attempt 11

Independent reviewer run `7731c838-7692-48b8-ad63-e7adc26208a5` reported
NO FINDINGS for both `a34d76491c` and `c1063381df`; its completion was consumed.
Integration `a08066e83093204e631fe9982ede9d3b85b4ced0` then merged main through
`1189323508`, preserving the startup-monitor and tmux-maintenance repairs.
All eight changed production/test files matched main's Git blobs. Combined
regressions passed 77 tests in 7.59 seconds; real native integration passed
three tests in 7.80 seconds. The release build passed, and fresh binaries were
pinned under `target/ask-probe-a08066e8` with the same hashes as attempt 10.

After an explicit quiet-window release, attempt 11 ran from that clean commit
with `UV_NO_SYNC=1`, isolated schema/home/ports, and the original 600-second
controller budget. Private parent indexing completed for 6,723 files from an
8,546-entry inventory, which also includes excluded entries. Materialization
took 42,020.969 ms and indexing took 292,164.304 ms. All six bootstrap commands
exited zero and the checkout was restored. A two-second sample recorded all
1,650 main-thread samples under local-import resolution; this remains a phase
observation, not an end-to-end performance attribution.

Ask run `04fb130b-2458-44cb-a1fe-dc9a7d9218a6` completed prepare at
`2026-09-10T23:46:35.930911Z`. Seed then failed at
`2026-09-10T23:46:36.540392Z` with
`index_incomplete: eligible snapshot path is not indexed: .claude-plugin/plugin.json`.
The earlier machine-identity error was resolved. No provider agents or launch
receipts were created, so native security and resume acceptance remain unproved.

Evidence is retained at `/tmp/gobby-ask-native-probe-12261-eleventh` with raw SHA-256
`faf8c0b4e29466ddf6842dc65971561234f80f3bdffc85d200e13ad2e02302fa`.
Cleanup recorded `errors=[]`, complete raw export, schema
`gobby_test_askprobe_bb259f1bad054fbc884511a359e754ee` dropped, runtime removed,
worker 56920 exited, and host 57069 absent. Parent process inspection found no
remaining harness or pinned gcode process. A diagnostic read raced cleanup and
found the private control directory already removed; the retained raw export
provided the index and failure records.

The expanded small native regression is intentionally RED pending the complete
inventory/index-policy correction. Adding a committed `.metadata/project.json`
reproduced the same error in 4.31 seconds. The fixture now also includes a tracked
gitignored source, committed text under `target/`, and empty source. Its current
first failure is the eligible `.gitignore` omitted by normal discovery (2.94 seconds).
The production fix must index the verified eligible inventory while preserving
security exclusions and ordinary navigation policy; it must not waive completeness
or allowlist only the first failing path. No new full probe is justified until
this real-native fixture passes fresh and recovered retrieval.

## Complete snapshot inventory indexing

The parent reproduced the remaining `.gitignore` failure on integration
`cdce43626b` with a freshly rebuilt private `gcode`: one failure in 3.07 seconds.
The correction adds `gcode index --snapshot-commit <oid>` and passes the run's
immutable commit through both fresh and recovered Ask snapshot preparation.
This command verifies the complete materialization, indexes verified Git blob
bytes through the existing parser and fact sinks, then verifies it again.
It indexes every eligible file, including hidden, ignored, excluded-directory,
and empty committed text. Snapshot overlays receive their own file selectors
and tombstones for parent-only paths. Ordinary discovery policy is unchanged.
The command rejects partial ordinary scan options. Sensitive, symlink, and
untracked exclusions remain enforced by the snapshot inventory and verifier.

The first native GREEN was one pass in 6.66 seconds, using the same command as
RED, with `GOBBY_NATIVE_BIN_DIR` set to the integration worktree's private
`target/release` directory:

```sh
UV_NO_SYNC=1 UV_PROJECT_ENVIRONMENT=/Users/josh/Projects/gobby/.venv PYTHONPATH=src \
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test \
GOBBY_TEST_PROTECT=1 GOBBY_NATIVE_BIN_DIR=/Users/josh/.gobby/worktrees/gobby/epic-22010-native-ask/target/release \
uv run --no-sync pytest tests/ask/test_native_integration.py::test_real_managed_snapshot_queries_branch_native_gcode -q
```

That test exercises real managed credentials, native indexing/evidence, fresh
and recovered generations, historical merge and root commit metadata, secret
exclusion, and rejection of a stale runtime identity. It launches no provider
agent. The companion snapshot/isolation files passed 83 tests; the focused Rust
CLI/parser run passed 149 tests. Exact final validation and subsequent review
are recorded under coordinator task `#22021`.

This resolves the small inventory regression, not the full native security or
resume acceptance. Attempts 1–11 remain unchanged, attempt 12 must retain its
original deadline and separate evidence directory, and the all-14 cohort has
not yet run. No shared binary installation or daemon restart was performed for
this correction.

## Independent review corrections

Review found that one-time overlay tombstones did not hide files added to the
parent index after snapshot capture. Ask now writes the pinned commit into the
isolation sidecar. Native snapshot scope reads only its own complete index,
retains isolated write authority, and routes refreshes through the same pinned
commit instead of ordinary checkout discovery. The native integration test
refreshes the parent through a separate managed session after capture, proves
the new parent row exists, and verifies snapshot evidence still excludes it.
The snapshot-marker unit test failed before this change and passed afterward.

The blob capture API now uses one verified `git cat-file --batch` process for
the complete eligible inventory. The process-count regression failed with the
empty API stub, then passed with exactly three blob processes across prepare,
materialize, and capture, independent of file count. This correction is committed
as `aa3c8fda0d` under coordinator `#22021`.

Focused validation passed 52 Rust tests and 85 Python tests, including native
snapshot/evidence and recovery. The 62 wait/resume and completion-registry tests
also passed in the worker's clean checkpoint. Mutable import-context and header
classification inputs remain under correction; optional clangd reads require a
separate hermetic boundary and cannot establish captured-source provenance.
Full native provider/security acceptance and all 14 cohort answers remain
unvalidated. These focused results do not replace attempts 1–11 or count as a
successful attempt 12.

The native integration test also passes with no initial parent index. Future
contained probes therefore omit the obsolete full parent-index preparation;
Ask prepares its own complete pinned index within the original deadline. The
helper and receipt export remain available for historical evidence and focused
parent-mutation tests. No previous attempt artifacts were changed.

## Recovery sealing and captured language inputs

Coordinator commit `fdac191ebb` restores the isolation marker during recovery
of an existing source tree and validates it against the persisted run binding.
Snapshot indexing rejects Single, ordinary Overlay, and mismatched commit scopes
before Git or database access. Its API also rejects all flags incompatible with
complete snapshot indexing. Four damaged-marker cases failed before the fix and
passed afterward. The rebuilt native CLI rejects deleted and ordinary-overlay
markers; recovery succeeds and preserves the parent's newer index row.
The focused snapshot/native selection passed 16 Python tests.

Managed merge `0880dd560f` integrates the captured parser/import inputs and the
wait/resume correction. The parent independently passed 201 focused Rust tests,
then resolved the indexer conflict by retaining the strict snapshot scope and
captured-source parser. The semantic-resolver regression now uses a bound
Snapshot context; it and both scope/option tests pass together.

Inventory language metadata uses verified eligible bytes and the full captured
regular-file path inventory, including content-excluded sibling paths. Excluded
blobs receive deterministic path-only language metadata. Indexing consumes that
same inventory classification. A regression failed for a committed header's
Objective-C sibling, then passed both normally and in a child process with
misleading ambient headers and sibling files. The combined focused Rust selection
passed 216 tests; clippy and the test-quality audit passed.

Worker correction `836f693bf6` restores extension filtering before live scanner
reads. Its bounded FIFO regression observed seven irrelevant reads before the fix
and zero afterward. The parent independently passed all 18 context-loading tests
and integrated it through managed merge `c832eb879d`. The parent also extended
safe Cargo prefix normalization to repeated leading `./` components after the
corresponding regression failed. Parent/absolute/backslash escape rejection stays
in place. Full native attempt 12 and all 14 cohort questions remain unrun; these
results are focused regression evidence.

## September 11 integration and executable-launch blocker

Managed merge `86287d573035b50d30c4b9f63ee0687baacfcd3d` incorporates main
through `a833a68b25`, including the settings-modal and session/activity fixes.
Main was verified to be an ancestor of the clean integration HEAD. The four
focused session naming/registration Python files passed 149 tests in 15.26 seconds.
The merged correction worktree was deleted through the managed tool, and its
merge commit was linked to coordinator #22021.

The final combined Rust selection did not reach test execution. Cargo launched
`target/debug/build/gobby-code-1145fe66d7eb26a9/build-script-build`, but a process
sample showed only `_dyld_start`, with zero CPU time after several minutes.
Its code signature verified successfully. Replacing that generated executable
with a new inode reproduced the stall. Independently compiling `crates/gcode/build.rs`
to `/tmp/gobby-12261-build-script-diagnostic` reproduced the same pre-main stall;
a separate ad-hoc re-signed copy also stalled. All owned stalled processes were
terminated, and their command handles returned definitive nonzero exits.

Samples are retained in `/tmp/gobby-12261-build-script-sample.txt` and
`/tmp/gobby-12261-fresh-build-script-sample.txt`. During the same window, macOS
`syspolicyd` repeatedly logged `Unable to initialize qtn_proc: 3` and
`dispatch_mig_server returned 268435459`. These observations implicate executable
admission but do not establish the underlying cause. Sampling that system process
was denied for lack of administrator privileges. Active project sessions were
notified; authorization for a coordinated system-service recovery was requested.
No system security settings, shared binaries, or daemon were changed.

The release rebuild, immutable binary pin, native attempt 12, installed acceptance,
and all 14 cohort questions remain pending. Earlier successful focused tests and
attempts 1–11 remain unchanged; the stalled build is not a native probe attempt.

The user then authorized a system-service restart. After a machine-wide coordination
notice, administrator-authenticated `launchctl kickstart` was refused by System
Integrity Protection. Administrator-authenticated `SIGTERM` to the verified
`syspolicyd` PID 494 succeeded; launchd started replacement PID 7443. The same
previously stalled `/tmp/gobby-12261-build-script-diagnostic` then completed with
exit 0 in 0.35 seconds. The quiet window was released. System Integrity Protection
remained enabled; no Gobby restart or shared binary replacement occurred. This
establishes recovery of the observed launch failure, not its underlying cause.
The Ask pipeline, permission, and recovery Python selection also passed 44 tests
in 10.19 seconds.
After service recovery, the same combined Rust selection compiled in 8.38 seconds
and passed all 217 selected tests in 2.196 seconds (873 unrelated tests skipped).

## Contained attempt 12

The release build completed in 58.93 seconds. Fresh immutable binaries were pinned
under `target/ask-probe-b1a2b014`: gcode SHA-256
`276167dbdbe0cd45a79d17076e040911b55ac8adc12017c0ef76a71c112b4384` and gterm SHA-256
`a910dd61e2f379ac91362ef492c667fb69b7adf3b9cc95959839ad11d18b7326`.
The 16 snapshot/native integration tests passed with that exact gcode binary in
22.52 seconds. Source HEAD was clean at `b1a2b014f150c491ccaedee2fbece959889726da`,
including all then-current main commits.

Attempt 12 ran with its original 600-second controller deadline. Ask run
`3e98fddf-996c-4a07-acba-8100c7e1b91f` completed prepare between
`2026-09-11T05:33:27.392758Z` and `2026-09-11T05:39:09.988719Z` (342.596 seconds).
Seed then failed with `stale_range`: the content search requested lines `1..8`
for `tests/ask/fixtures/native_ask_probe_hostile.txt`, whose committed blob has
seven lines. Seed supplies a content query, not a hard-coded fixture line range.
The canonical producer/consumer range disagreement remains an owned correction;
the fixture and stale-range rejection are preserved.

No provider agents or launch receipts were created. Raw evidence is retained at
`/tmp/gobby-ask-native-probe-12261-twelfth/raw-probe.json`, SHA-256
`e9cfec430586a6b66241ef5885e87506113ac0caaa8753b53aa542b8e44523e8`.
Cleanup reported `errors=[]`, complete raw export, private schema dropped, runtime
removed, worker exited, and terminal host absent. A parent sample request raced
the indexer's normal exit and produced no sample. Attempts 1–12 remain immutable;
native security/resume proof, installed acceptance, and the all14 cohort remain open.

An isolated #22018 correction worker is reproducing the line-range failure.
A separate read-only reviewer is checking sealing/admission requirements and
whether repeating full-repository preparation for the second native run can fit
the unchanged controller budget. The recurrent MCP Git-status timeout also
reappeared after macOS recovery while direct Git status took 0.04 seconds; its
cause remains unresolved. No further full probe is scheduled before the required
correction and independent review.

## Separate Ask and controller deadlines

The implementation plan specifies a configurable default of 600 seconds for each
Ask run, including preparation, agents, review, repair, and publication. It does
not require two distinct runs and controller setup to share that same budget.
The harness incorrectly used the remaining controller time as each new Ask
request's timeout, coupling independent runs and making its 600-second controller
too short for two observed full-repository preparations.

The controller now defaults to 1,500 seconds, while each new Ask request uses the
public 600-second default. Recovery continues to attach to the existing durable
run and keeps its original deadline. The full integration repository remains the
probe source; reducing the repository merely to fit the former controller limit
is unnecessary. The frozen cohort still uses 600 seconds per question, and
attempts 1–12 retain their original commands and outcomes.

The parser and independent-request budget regressions failed before the change
and passed afterward. All 100 focused harness, cleanup, and provenance tests
passed in 5.23 seconds. Ruff, test quality, test types, and the suppression ratchet
passed. This corrects the controller contract; another full native probe still
depends on the product line-range correction and parent review.

## Range corrections reviewed; native launch recovery pending

The range producer correction `e447449f5ed9b8ea5ca0ba5bb625d4c00e8ed3ca`
removes the synthetic segment after a terminal LF. Parent DB-backed validation
then exposed `index_incomplete` for `fixtures/empty.txt`. The prescribed Rust
test database, `gobby_gcode_test` on loopback port 60892, worked; the earlier
worker's Python-test-database and fresh-image failures were not prerequisites
for this validation.

Correction `4361440167e6463a7530e9ecc47053666b412ac8` admits an absent ordinary
index row only for a verified empty or valid UTF-8 whitespace-only snapshot
blob. Missing nonblank facts and mismatched indexed hashes remain errors. Tests
cover empty and Unicode-whitespace admission, missing nonblank facts, terminal
LF/CRLF, unterminated content, and real blank lines at an overlap boundary. The
integration metadata assertions now cover the exact seven-path fixture commit.
Parent independently reviewed both diffs and ran:

```sh
env -u DEVELOPER_DIR -u DATABASE_URL GCODE_POSTGRES_TEST_DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_gcode_test GOBBY_TEST_PROTECT=1 cargo test -p gobby-code --test evidence -- --nocapture
```

All four tests passed in 8.43 seconds. Both commits were merged through the
managed worktree tools into the integration branch at `ba3aca4d1842c64171fc5261c9b73f04aa268bcf`;
the correction worktree was deleted through the same tools. Main was then merged
through `07255f624fb2ccc2f827cc3e8d8f3d48fd29acaa`, preserving committed main
changes through `2a0164ad1f` without touching foreign main dirt.

The release gcode build completed in 62 seconds. Immutable probe binaries at
`target/ask-probe-07255f62` have these SHA-256 digests:

- gcode: `bd676d06d9933251fe1e6f6ee363c3f39c58281fee03f95a3e889ed1072641d0`
- gterm: `a910dd61e2f379ac91362ef492c667fb69b7adf3b9cc95959839ad11d18b7326`

The preliminary `tests/ask/test_native_integration.py` run with that pin stalled
at native launch. A sample of gcode PID 52926 showed only `_dyld_start`, with a
112 KB footprint; `codesign --verify --strict` passed. The sample is preserved at
`/tmp/gobby-12261-probe-07255f62-sample.txt`. Concurrent syspolicyd PID 7443 logs
again showed `Unable to initialize qtn_proc: 3` and
`dispatch_mig_server returned 268435459`. The parent terminated the two owned
stalled gcode children so test cleanup could finish: two tests passed and the two
native cases failed after termination (413.91 seconds total). This run is not
passing native acceptance and is separate from numbered full probe attempts.

The user-authorized graceful syspolicyd restart was requested through macOS
administrator authentication and was still awaiting authentication at this
checkpoint. No Gobby daemon restart or shared binary install occurred. Ordinary
coordination sends, a read-only diagnostic worker launch, and session listing
were blocked by the recurrent MCP Git-status preflight failure; even the optional
preflight setting did not unblock a coordination message. No diagnostic worker
was created. Direct integration Git status completed in 0.09 seconds. Both the
OS launch recovery and the Git-status failure remain owned, unresolved work.
Attempt 13 and the all14 cohort have not run; attempts 1–12 remain unchanged.

## Attempt 13: snapshot preparation passed; credential admission rejected seed

The same pinned gcode subsequently executed `--version` successfully without a
service restart. A complete rerun of `tests/ask/test_native_integration.py`
passed all four tests in 10.32 seconds. The pending administrator-authentication
request was cancelled; syspolicyd remained PID 7443. The earlier stalled run is
retained above. These observations establish recovery, not that the version
invocation caused it. A read-only worker is investigating the recurring native
launch and Git-status failures.

Attempt 13 ran from clean source `13e9597cc1f1b6fa2ddf9bb2c36bf154d2bb9129`
with the unchanged immutable gcode/gterm pin described above:

```sh
UV_NO_SYNC=1 UV_PROJECT_ENVIRONMENT=/Users/josh/Projects/gobby/.venv PYTHONPATH=src DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 GOBBY_NATIVE_BIN_DIR=/Users/josh/.gobby/worktrees/gobby/epic-22010-native-ask/target/ask-probe-07255f62 uv run --no-sync python tests/ask/native_probe_harness.py contained-drive --project-root /Users/josh/.gobby/worktrees/gobby/epic-22010-native-ask --output-dir /tmp/gobby-ask-native-probe-12261-thirteenth --timeout-seconds 1500
```

Fresh Ask run `0f3dd685-f7bf-4637-9f49-4cb474d6d182` retained its own
600-second budget, with deadline `2026-09-11T09:04:12.250681Z`. Preparation
completed between `2026-09-11T08:54:12.533543Z` and
`2026-09-11T08:59:49.198114Z` (336.665 seconds). Seed failed at
`2026-09-11T09:00:01.710662Z` with `invalid gcode evidence response: response
contains credential-bearing content`. No provider agents or launch receipts
were created. The resumed phase did not run.

The raw export at `/tmp/gobby-ask-native-probe-12261-thirteenth/raw-probe.json`
has SHA-256 `b851f5cfacb5aca19b659c63458ce444fa5aa272a7bd51c0f99dc3c356a69ca0`.
It reports complete capture with no capture errors or missing agent IDs.
Cleanup reports `errors=[]`, the owned schema dropped, runtime removed,
worker exited, terminal host absent, and empty owned process groups. This is a
failed primary attempt; it does not satisfy native acceptance.

Parent diagnosis reproduced two false positives in Python's admission check:
an ordinary home-directory path and `token = runtime_token_reference` both
return true from `_contains_credential`, as does a real credential-bearing URI.
The check compares terminal-output redaction against serialized JSON; that
redactor also rewrites the operator home and bare source references. Rust's
snapshot classifier already distinguishes source references from literal
assignments. An exact committed-blob scan found 262 Python-rejected blobs among
8,027 Rust-eligible entries, including 41 with the operator home path. This is
classifier disagreement, not a finding that all 262 are harmless. The path-only
diagnostic report is `/tmp/gobby-12261-credential-parity-candidates.json`; native
inventory is `/tmp/gobby-12261-attempt13-inventory.json`.

A scoped #22018 worker is correcting admission semantics while preserving real
secret rejection, audit redaction, sensitive-path exclusions, response identity,
and citation bytes. Attempts 1–13 remain unchanged. Attempt 14 and the all14
cohort remain unrun; full native and installed acceptance remain unproved.

## Attempt 14: credential correction reviewed; native symbol range rejected seed

Credential correction `7946f7bb18bf29ee8a5e20117e7b0810df18aa60` separates
structured credential admission from terminal privacy redaction. It preserves
validated response bytes and citation hashes, rejects actual credential shapes,
and uses snapshot language context for source references. The worker's exact
262-candidate parity scan reported no remaining Python rejections. Parent
review covered both changed files without blocking findings.

Parent also repaired an obsolete `_seed_parent_index` import and call in
`tests/ask/test_validation_native.py`, committed as `b5165ad199`. The collection
failure was reproduced before the repair. The current snapshot manager prepares
the test's index itself. Parent validation with the existing immutable pin:

```sh
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 UV_NO_SYNC=1 UV_PROJECT_ENVIRONMENT=/Users/josh/Projects/gobby/.venv PYTHONPATH=src GOBBY_NATIVE_BIN_DIR=/Users/josh/.gobby/worktrees/gobby/epic-22010-native-ask/target/ask-probe-07255f62 uv run --no-sync pytest tests/ask/test_evidence.py tests/ask/test_validation.py tests/ask/test_native_integration.py tests/ask/test_validation_native.py -q
```

All 23 tests passed in 17.54 seconds. Ruff checks and the Python suppression
ratchet passed. Managed main synchronization at
`6e258e6b1ddd6188aa4fab6b2e7ec59921ac5bd1` included the latest main commit
`def724b78b`; foreign main changes were untouched.

Attempt 14 used that clean synchronized source, the unchanged binary hashes
above, the full integration repository and the unchanged probe question:

```sh
UV_NO_SYNC=1 UV_PROJECT_ENVIRONMENT=/Users/josh/Projects/gobby/.venv PYTHONPATH=src DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 GOBBY_NATIVE_BIN_DIR=/Users/josh/.gobby/worktrees/gobby/epic-22010-native-ask/target/ask-probe-07255f62 uv run --no-sync python tests/ask/native_probe_harness.py contained-drive --project-root /Users/josh/.gobby/worktrees/gobby/epic-22010-native-ask --output-dir /tmp/gobby-ask-native-probe-12261-fourteenth --timeout-seconds 1500
```

Fresh Ask `1a5e9916-06b8-49e2-8bd7-d1c6810e3a81` retained deadline
`2026-09-11T10:41:19.589384Z`. Preparation completed between
`2026-09-11T10:31:19.852647Z` and `2026-09-11T10:36:27.353188Z`
(307.501 seconds). Seed then failed at `2026-09-11T10:36:47.008951Z`:

```text
stale_range: stale range for src/gobby/install/shared/workflows/rules/task-enforcement/track-task-claim.yaml: symbol lines 13..43 resolve to 13..42
```

No agents or receipts were created; the resumed phase did not run. The complete
raw export at `/tmp/gobby-ask-native-probe-12261-fourteenth/raw-probe.json` has
SHA-256 `03c24979cda9cf9f1e8643f26d133e26ab652e82cd44b5c7d3dc71c68a8e7053`.
Capture errors and missing agent IDs are empty. Cleanup reports no errors,
schema `gobby_test_askprobe_ded176a9a0b742a3816bf4312ee16685` dropped,
runtime removed, fresh worker 76706 exited, and terminal host 76917 absent.
The next correction must address source symbol range generation and related
parser boundaries without weakening stale-range admission.

Separately, read-only diagnosis confirmed eager Git-status preflight amplifies
intermittent process stalls into unavailable query tools. A patch at
`e7695d95c5e16a1876d64a99f30bcca3210904c1` remains isolated in
`task-22021-query-git-preflight` for independent review. Parent reran
`tests/utils/test_daemon_git.py tests/workflows/test_workflow_hooks.py`: 61 passed
and 10 failed in 204.59 seconds. The failing fake executables timed out before
expected output or first marker writes, including a 60-second case. These
failures remain unresolved; they are not a passing validation result. The
independent reviewer ended with zero activity and no review, so review remains
outstanding. Git and gcode sharing a Gatekeeper root cause is still unproved.

Attempts 1–14 remain immutable failures. Attempt 15 and the all14 cohort are
unrun. No parent service restart or shared binary installation occurred here.

## Parser correction QA and release-build launch failure

Correction `ef2d583374ec494416fd97c3e31dcd1f27b8c977` converts tree-sitter's
exclusive definition end position to an inclusive source line. A node ending
at column zero immediately after LF uses the preceding line; byte bounds,
content hashes, and symbol-ID inputs remain unchanged. The independent reviewer
covered all three changed files and reported no material findings. The worker's
two broader corpus audits timed out, so no full-corpus result is claimed.

Parent independently ran the parser and evidence tests in the correction tree:

```sh
env -u DEVELOPER_DIR -u DATABASE_URL GCODE_POSTGRES_TEST_DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_gcode_test GOBBY_TEST_PROTECT=1 cargo nextest run -p gobby-code -E 'test(index::parser::tests) | test(evidence)'
```

All 145 selected tests passed in 8.177 seconds after compilation. Managed merge
was then blocked by the live Git-status preflight. A release build from the
reviewed correction tree, using the integration target directory, stalled in
`gobby-code`'s build-script executable before `main`. Parent sampled owned PID
27408: only `_dyld_start`, with a 96 KB footprint. Evidence is retained at
`/tmp/gobby-12261-parser-release-build-sample.txt`. Parent terminated that owned
child with SIGTERM and consumed Cargo's exit 101. No new release pin was created.

The separate direct fake-Git reproduction also stalled before `/bin/sh` entered
`main`; `/tmp/gobby-12261-direct-fake-git-sample.txt` records `_dyld_start` and a
96 KB footprint. The user-authorized graceful syspolicyd restart request is
still awaiting macOS administrator authentication at this checkpoint. No service
restart has completed. The reviewed parser commit remains isolated for managed
landing after recovery, and attempt 15 remains unrun.

## Attempt 15: preparation and seed pass; launch receipt capture fails

The pending administrator dialog ended with `User canceled (-128)`; parent did
not restart syspolicyd. After another session's announced main-checkout daemon
restart, a fresh shell executable launched in 0.344 seconds. This establishes
that the launch probe recovered, not the cause of the earlier OS stalls.

The reviewed parser correction was merged at `575e3fc734`; the corrected Git
preflight/callback work was merged at `3f8410b49c`. Both correction worktrees
were deleted through managed worktree tools. Parent validation on integration:

```sh
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 UV_NO_SYNC=1 UV_PROJECT_ENVIRONMENT=/Users/josh/Projects/gobby/.venv PYTHONPATH=src uv run --no-sync pytest tests/utils/test_daemon_git.py tests/workflows/test_workflow_hooks.py -q
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 UV_NO_SYNC=1 UV_PROJECT_ENVIRONMENT=/Users/josh/Projects/gobby/.venv PYTHONPATH=src uv run --no-sync pytest tests/ask/test_pipeline.py tests/ask/test_permissions.py tests/ask/test_recovery.py -q
```

The full Git/workflow pair passed all 78 tests in 5.09 seconds; the Ask pairings
passed all 44 tests in 10.08 seconds. These results resolve the earlier focused
test failures but do not establish native runtime acceptance.

The `gcode` release build passed in 57.99 seconds. `gterm` first required its
`vt-engine` feature, then reproduced the documented Zig/libc++ `INFINITY`
failure with the default SDK. Selecting
`DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer` made the release
build pass in 14.88 seconds. Fresh binaries were pinned under
`target/ask-probe-3f8410b4`:

- `gcode`: `a00bd2df47691e24e2e1ef15fbf6b03569033785f27826ae60a23b6e9e3632d0`
- `gterm`: `dd0ee8a926db25b0b534b54803fd92e059785007dece62198e226dd289a94204`

Attempt 15 used clean source `3f8410b49cc3a82dcc89116bdad4ab17f0511947`:

```sh
UV_NO_SYNC=1 UV_PROJECT_ENVIRONMENT=/Users/josh/Projects/gobby/.venv PYTHONPATH=src DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 GOBBY_NATIVE_BIN_DIR=/Users/josh/.gobby/worktrees/gobby/epic-22010-native-ask/target/ask-probe-3f8410b4 uv run --no-sync python tests/ask/native_probe_harness.py contained-drive --project-root /Users/josh/.gobby/worktrees/gobby/epic-22010-native-ask --output-dir /tmp/gobby-ask-native-probe-12261-fifteenth --timeout-seconds 1500
```

Ask run `c2513b1f-0883-4a07-9b09-069031b36ef0` retained its original deadline
`2026-09-11T13:02:08.415358Z`. Preparation completed in 322.616 seconds and seed
in 31.659 seconds. Investigator launch then failed with
`launch terminal is not a JSON object`. Agent run
`10cc2f00-a908-4a1c-9d39-b77f25fbc9ec` had no recorded PID, child session, or
terminal and was subsequently cancelled. No provider launch receipt, boundary
probe, or resumed-runtime acceptance was obtained.

The primary raw export remains at
`/tmp/gobby-ask-native-probe-12261-fifteenth/raw-probe.json`, SHA-256
`e7123c339c2c0ef08dfc4f49e7d235968b940492be03992bd34f080fbf7a77ab`.
It explicitly reports `complete=false`, a missing agent session export, and
the missing agent run above. Cleanup verified the fresh worker and terminal
host absent, but could not prove the agent's process state. It
therefore retained runtime `/private/tmp/gobby-ap-xl2j8dul` and isolated schema
`gobby_test_askprobe_8d9334776a0b4faf9e3acdc2083e4485`. This is an owned receipt
and cleanup finding, not successful cleanup. The correction must preserve
asynchronous launch identity and the original deadline without weakening
runtime evidence checks.

Attempts 1–15 remain immutable failures. Attempt 16 and all14 remain unrun.
Parent has not installed shared binaries or landed the epic on `0.5.0`.

## Attempt 16: launch identity retained; SRT policy validation fails

The launch receipt correction was merged at `8bb14b6fca` after parent review
and 52 focused harness tests passed in 3.64 seconds. The correction awaits the
registered managed spawn task and owns receipt capture through cancellation;
an incomplete launch retains its agent identity in the raw export.

Attempt 16 used clean source `8bb14b6fca2b4ad10ea3d2988b9de6afe74b2dbe`
and the unchanged `target/ask-probe-3f8410b4` binary pins listed above:

```sh
UV_NO_SYNC=1 UV_PROJECT_ENVIRONMENT=/Users/josh/Projects/gobby/.venv PYTHONPATH=src DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 GOBBY_NATIVE_BIN_DIR=/Users/josh/.gobby/worktrees/gobby/epic-22010-native-ask/target/ask-probe-3f8410b4 uv run --no-sync python tests/ask/native_probe_harness.py contained-drive --project-root /Users/josh/.gobby/worktrees/gobby/epic-22010-native-ask --output-dir /tmp/gobby-ask-native-probe-12261-sixteenth --timeout-seconds 1500
```

Ask run `0bd2999f-4ee0-4069-94ba-e53fa9ad5f04` completed preparation from
`2026-09-11T14:30:18.736984Z` to `14:35:30.240879Z`, then seed from
`14:35:30.245416Z` to `14:36:00.510184Z`. Investigation failed at
`14:36:01.116395Z`. Agent `5ee53929-f8ec-4fca-8317-790278dabb50` recorded
`Ask SRT policy semantics changed after validation` and was cancelled with
no PID, terminal, or child session. The receipt path also reported
`native Ask launch policy is missing or outside the owned runtime`.
The relationship between these errors remains under investigation.

The command exited 1. Its immutable raw export is
`/tmp/gobby-ask-native-probe-12261-sixteenth/raw-probe.json`, SHA-256
`e686b494c12aa73db464219685ab274e886d820804690ff8e02691e699641f24`.
It reports `complete=false`, one `IncompleteLaunch` session capture error,
and `missing_agent_run_ids=[]`. Agent identity is now retained, but no launch
receipt or native boundary acceptance was obtained.

Cleanup verified worker PID 57230 and terminal-host PID 57299 absent, with
their sockets absent. Agent liveness remained unknown, so runtime
`/private/tmp/gobby-ap-myn4pgvr` and isolated schema
`gobby_test_askprobe_321e8f6be3de458588a822029ac49d31` remain retained.
Unknown liveness is not proof of successful cleanup.

Attempts 1–16 remain immutable failures. Attempt 17 and all14 remain unrun.
The next correction must resolve the actual managed-launch policy mismatch
while preserving semantic validation and the native evidence requirements.

## Host recovery and timeout observability

Parent sampled a separate test process after eleven minutes at executable
startup: all 883 samples were in `_dyld_start`, retained at
`/tmp/gobby-12261-gclient-10996.sample.txt`. Identical Ruff 0.14.13 bytes
(SHA-256 `97298756073d8e9550bef0bf63545e118e32cd12bd6d5a557aa56d47f9d688cf`)
started in 0.012 seconds from the main environment, while the cohort
worktree's executable timed out after eight seconds.

After explicit coordination and administrator authentication, a targeted
`SIGTERM` to the verified root-owned syspolicyd PID 7443 succeeded. Launchd
replaced it with PID 51915. The previously stalled cohort Ruff executable
then started in 0.036 seconds. All coordination holds were lifted. No Gobby
daemon restart or shared binary installation occurred during this recovery.

This does not establish the cause of Git status timeouts. The separate Git
PID 9224 sample was already enumerating directories through `getdirentries`;
its 0.8-second sample does not prove that operation exceeded five seconds.
Direct Git and fresh Python-helper controls completed successfully. Earlier
sandboxed `spctl` internal errors were not reproduced by unsandboxed controls
and are not conclusive host-health evidence.

The requested Claude Fable 5.1 xhigh investigation found that earlier Git
diagnostic commits were live, but MCP substituted a fixed error message,
non-stop hooks emitted details only at DEBUG, and the shared Git boundary
did not log a warning. Integration commit `feb7683e04` adds one warning at
that boundary with cwd, timeout, PID, phase and timing, omitting raw command
arguments. Its regression failed before the change, then passed; all 78
focused Git/workflow tests passed. This logging correction awaits landing
and coordinated Gobby daemon restart before it is live.

On September 11, parent adapted the logging correction onto current `0.5.0`
in a separate managed worktree. The conflict resolution preserved main's
`control.diagnostic()` and boolean `consumer_active` cleanup semantics;
only cwd plumbing and the warning were added. Parent passed all 87 tests in
`tests/utils/test_daemon_git.py` and `tests/workflows/test_git_utils.py`, plus
Ruff, formatting, one-file mypy and diff checks. Independent review
`9c9a5707-451d-4f8c-95d6-6919ff325948` allowed both staged files with no findings,
using Git blobs after the code index returned stale pre-patch lines.

Commit `4a91865782` was linked to coordinator #22021 and managed-merged into
local `0.5.0` at `8a4f09356cf1678377ae487cd06f79d4a6b03f96`. The temporary
worktree was deleted through the managed lifecycle. Newer main fixes and
unrelated working changes were preserved. At this checkpoint daemon PID
5175 still predates the correction: restart coordination is pending the
native worker's explicit hold and remaining active-owner acknowledgement.
The warning is landed but not yet verified live; no shared binary was replaced.

## Attempt 17: pinned config preserved; rendered policy still differs

Independent review allowed the three-file SRT correction `5f42ce6ef9`, and
parent independently passed all 98 permission/spawn tests in 1.57 seconds.
The correction uses the immutable Ask sandbox config on fresh launch instead
of the generic helper that adds registry access and cache/inbox writes.
It was managed-merged into integration at
`3f052e1ffd6d6dd28661222d61796040bd5576a7`; its worktree was deleted.

Attempt 17 used that clean commit and the unchanged native binary pins above,
with the same contained-drive command and a new output directory
`/tmp/gobby-ask-native-probe-12261-seventeenth` (controller timeout 1500 seconds).
Ask run `cb993a87-0f56-4ded-8320-51342606f990` retained the absolute deadline
`2026-09-11T16:13:45.030456Z`. Preparation completed from
`16:03:45.307643Z` to `16:09:00.637844Z`; seed completed at
`16:09:34.929894Z`. Investigation failed at `16:09:35.544342Z`.

Agent `2475a764-e674-48ca-934c-a1e6db59678f` still recorded
`Ask SRT policy semantics changed after validation`, then rollback cancelled
it without a PID, terminal or child session. The receipt error remained
`native Ask launch policy is missing or outside the owned runtime`.
The retained config confirms `allow_package_registries=false` and empty
extra write paths: removing the generic config widening did not resolve
the complete rendered-policy mismatch. The next correction must reproduce
the actual compile/render/launch comparison in a focused executable test.

The command exited 1. Immutable `raw-probe.json` SHA-256 is
`6bcd9547b16534d303262c4ea76f0f3969298929a5d4e9a8166e86158d33f2aa`;
`complete=false`, one `IncompleteLaunch` capture error,
`missing_agent_run_ids=[]`, and no accepted receipts. Cleanup verified
worker PID 18937 and terminal-host PID 19424 absent. Agent liveness remains
unknown, so runtime `/private/tmp/gobby-ap-v_t3et1v` and schema
`gobby_test_askprobe_1c2bbc150042477bbd263db84e864a9e` remain retained.
The cleanup artifact SHA-256 is
`00a98570b53866633818b075edf75e010c92d0bccf45b4f62b51b6cfd9e83ce7`.

## Attempt 18: host disk exhaustion rejected preparation

Attempt 18 ran from session `gobby#12858` with output directory
`/tmp/gobby-ask-native-probe-12858-eighteenth`, starting `13:52` and failing at
`13:55` local time.

It never reached the SRT policy question. The pipeline failed at its first step:

```
MCP step prepare failed: gobby-ask:prepare returned error:
gcode_index_failed:1:Error: db error Caused by: ERROR: could not extend file
"base/16384/4770513": No space left on device HINT: Check free disk space.
```

This is an environmental failure of the host, not a defect in Ask. The data
volume was full because two orphaned cargo build trees held roughly 245 GB — the
main checkout's `target/` at 226 GB and 1.37M files, and the epic worktree's at
19 GB. Task #22021 removed both when it moved every checkout onto one shared
build directory per project (82c58ef83c) and reclaimed the orphans; the volume
now reports 6.1 TiB available of 7.3 TiB. Attempt 18 carries no evidence about
launch policy and must not be counted as a policy failure.

Immutable `raw-probe.json` SHA-256 is
`822d9fdded689c1657e7308c466cc61c959b05cd410b0bc5c62c49a82b7ed41b`;
`complete=true`, zero receipts, zero excluded receipts, no cleanup errors. The
runtime root was removed and schema
`gobby_test_askprobe_6e8066b2b3bd421485d62e749992c750` was dropped, so nothing
from this attempt is retained.

## Attempt 19: preparation and seed pass; spawn rejects the launch policy

Attempt 19 ran from the same session with output directory
`/tmp/gobby-ask-native-probe-12858-nineteenth`, starting `14:01` and failing at
`14:07` local time, with disk pressure no longer present.

Preparation and seed both completed — `ask_runs[0].steps[0]` and `steps[1]`
record no error. The pipeline failed at `steps[2]`:

```
MCP step investigate failed: gobby-ask:spawn returned error:
native Ask launch policy is missing or outside the owned runtime
```

Agent run `8f09ef3e-2f7c-4cd7-8cc8-3f186eff40e8` separately recorded
`Ask SRT policy semantics changed after validation`, and capture reported one
`IncompleteLaunch` error, "native Ask agent run has no child session", for the
same run. `runtime_identity.control_digest` is
`b38922fca36491b574448fd752dee104b9285ece1fbcd27fc38ccb16e792ad50`.

Two errors are recorded, and only one of them is causal. The agent-side message
is the digest comparison in `AskRuntimeProfile.validate_launch`
(src/gobby/ask/runtime_profile.py:194) and is the real failure. The spawn-side
message comes from the probe harness at tests/ask/native_probe_harness.py:2387,
where it tries to copy the rendered policy out of `sandbox["policy_path"]` into
its own artifact root and finds nothing to copy because the launch had already
aborted. It is a consequence, not a second defect.

The retained launch sandbox block is in this attempt's `raw-probe.json` under
`agent_runs[0].agent.resume_metadata_json.sandbox`, carrying `backend: srt`,
`enforced: true`, its `policy_hash`, and `managed_bootstrap_path`
`/private/tmp/gobby-ap-cmygt4ow/gobby/runtime/managed-executions/8f09ef3e-2f7c-4cd7-8cc8-3f186eff40e8/grant.json`.

Immutable `raw-probe.json` SHA-256 is
`76bfba5e3ff5ce8bd36b78f8d131e7475529271cc07e36e1e67cd1fa701b5c81`;
`complete=false` with 3 excluded receipts and no accepted receipts. Cleanup
reported three errors: owned native Ask process liveness unknown for run
`8f09ef3e-2f7c-4cd7-8cc8-3f186eff40e8`, cleanup could not verify every owned
process is dead, and the evidence export is incomplete so owned state is
retained. Run root `/private/tmp/gobby-ap-cmygt4ow` therefore remains on disk,
together with schema `gobby_test_askprobe_122d8ebbbe544508b1ead4c273d91f72`.

## Root cause, established from the retained artifacts

Attempt 19 closed the question that attempt 17 left open. The comparison it
demanded was reproduced against the retained policy documents rather than
another probe run.

`gcode_runtime_write_exceptions` (src/gobby/agents/sandbox_policy.py:353) grants
every sandbox its own generated gcode home,
`<GOBBY_HOME>/gcode-runtime/<sha256(workspace)[:16]>`, and
`compute_sandbox_paths` injects it into `allowWrite` and therefore `allowRead`.
That path sits under `GOBBY_HOME`, outside the source, scratch, run and run-temp
roots that `normalized_ask_srt_policy_digest` relabels, so it is hashed
verbatim — while its last segment is a pure function of the sandbox workspace.
Ask gives every stage and every repair attempt its own workspace
(`artifacts.run_root / "scratch" / f"{stage}-{attempt}"`), so the digest could
never be invariant across launches. A perfectly captured receipt would still
fail the next Ask run, the reviewer stage, and repair attempt 1, each with the
identical message.

Attempt 15 retains both sides of an exact capture/launch pair, and their
normalized diff is exactly this entry in `allowRead` and `allowWrite`, plus the
`hooks/inbox` and package-registry rows that 5f42ce6ef9 had already removed.
Attempt 19 narrows it to one row: capture digest
`4e882ae34b95a2428de72f6913693475d0d1299515a309c0892733e878f7df35` against
launch digest
`e4486503f5c3220ae6f5850db497e5c6a02fcd34f1ce4aaf777372a70aba1fca`, differing
only in `/private/tmp/gobby-ap-cmygt4ow/gobby/gcode-runtime/33ac34073d6cee95`
against `…/gcode-runtime/4f91b29328f09863`. `sha256(workspace_path)[:16]`
reproduces all four observed directory names arithmetically.

Corrections 5f42ce6ef9, e21e6eabc6 and 8951b60841 each adjusted policy content
and each left the failure unchanged, because the defect is in the normalizer
that hashes the policy, not in what goes into it. Commit 16be058101 relabels the
runtime home like the other roots and carries four regression tests, two of
which fail on the unpatched normalizer.

## Pinned binaries for attempts 20 to 24

Attempts 20, 21 and 22 ran against `.ask-probe-1d9e243b`, built from
`1d9e243bf9` in the epic worktree:

- gcode `dc8118720ac50cf78e142156acc112cdb820b6029b7903198841803886507558`
- gterm `dd0ee8a926db25b0b534b54803fd92e059785007dece62198e226dd289a94204`,
  byte-identical to attempt 15's gterm

No Rust source changed between `1d9e243bf9` and `4c0f6bce05`, so attempts 21
and 22 ran later commits against these same binaries. Attempts 23 and 24 ran
against `.ask-probe-b92c5542`, built from `b92c554220`:

- gcode `dc8118720ac50cf78e142156acc112cdb820b6029b7903198841803886507558`,
  unchanged, because `b92c554220` touches only `crates/gterminal`
- gterm `b7ad098377451219b6b12f07bae8caa569a9f81163b8d2cab501a2e4e0b48ac2`

The pin directory moved out of `target/` at attempt 20 and into
`<worktree>/.ask-probe-<sha>/`; the reason is attempt 20's finding below.

## Attempt 20: the shared build directory hid the branch-local binary

Attempt 20 ran from session `gobby#12858` at `19:57` with output directory
`/tmp/gobby-ask-native-probe-12858-twentieth`, against source `1d9e243bf9`. It
failed before any Ask work, at runtime identity capture:

```
RuntimeError: native Ask probe did not select the branch-local gcode binary
```

The pinned binaries had been placed under `target/ask-probe-1d9e243b`, which is
where every earlier attempt put them. Since 82c58ef83c that path is no longer
inside the worktree: `target` is a symlink to
`~/.gobby/cache/cargo-target/<project_id>/`, so `(bin_dir /
"gcode").resolve(strict=True)` lands outside `source_root` and
`_capture_runtime_identity` (tests/ask/native_probe_harness.py:764) rejects it.
The check is correct and the pin location was wrong: under one shared build
directory, nothing under `target/` is branch-local.

The pin moved to `<worktree>/.ask-probe-<sha>/`, a real directory inside the
worktree, excluded through the repository's local exclude file.

Immutable `raw-probe.json` SHA-256 is
`0c11a8b5d837c0ec0b24878fd5a72a6ec896eab071b76df744d3bb120b3b2e69`;
`complete=false`, zero receipts, one capture error (the isolated schema was
never created). Runtime root `/private/tmp/gobby-ap-s_es4v11` is retained. This
attempt carries no policy evidence.

## Attempt 21: the shared build-directory link broke snapshot preparation

Attempt 21 ran at `19:58` with output directory
`/tmp/gobby-ask-native-probe-12858-twentyfirst`, against the same source and
the relocated pin. It failed at the first pipeline step:

```
MCP step prepare failed: gobby-ask:prepare returned error:
gcode_index_failed:1:Error: unexpected materialized path: target
```

Second fallout from the same shared build directory, and a strictly better
failure than attempt 20 because it reached Ask. 82c58ef83c made the isolation
sidecar writer link `<root>/target` into any root holding a Cargo.toml, and
`src/gobby/ask/snapshots.py` reuses that writer for the commit-bound evidence
snapshot. The snapshot root therefore held an entry that is not in its binding
commit, and `verify_materialized_paths`
(crates/gcode/src/evidence/snapshot.rs:625) rejected it, correctly.

Commit 4c0f6bce05 skips the link when `snapshot_commit` is supplied, which only
Ask's snapshot writer does. Ordinary isolation roots keep the shared build
directory.

Immutable `raw-probe.json` SHA-256 is
`ae1b7fb67852ffd8d391acd69c8ec8f972158deef82d28de7b640555edb4bc7b`;
`complete=true`, zero receipts, no capture errors. Cleanup was complete: the
runtime root was removed and schema
`gobby_test_askprobe_3a51d9552bbf411a99c51407247881eb` was dropped, so nothing
from this attempt is retained. It carries no policy evidence either.

## Attempt 22: the digest fix holds, and the launch dies one step later

Attempt 22 ran at `20:04` with output directory
`/tmp/gobby-ask-native-probe-12858-twentysecond`, against source `4c0f6bce05`,
which carries the #22018 digest fix (16be058101) and the snapshot link fix. It
is the first attempt to reach the investigator spawn.

The evidence that #22018 is fixed is negative and decisive. `prepare` completed
in five minutes and produced a real commit-bound snapshot with gcode evidence
ids; `seed` completed; `spawn` ran `_preflight_srt` to completion in 111.8 ms
and logged its phase timings (`agents.spawn_executor.execute_spawn`), which is
the exact point where every attempt from 1 to 19 raised "Ask SRT policy
semantics changed after validation". That message does not appear anywhere in
this attempt's logs. The SRT sandbox launched: the run has a sandbox-violations
file recording a `sysctl-read` deny from the preflight's own `node --version`.

The launch then failed 34 ms after the pane opened:

```
mcp_proxy.tools.spawn_agent._implementation._run_spawn_phase
  Background agent boot failed for run 1f39c81f-...: expected welcome, got error
workflows.pipeline_executor._execute
  MCP step investigate failed: gobby-ask:spawn returned error:
  native Ask launch policy is missing or outside the owned runtime
```

The second message is the harness reporting, correctly, that the reaper of an
aborted launch had already emptied
`gobby/runtime/managed-executions/8a284c01-.../`, so
`_capture_agent_launch_receipt` (tests/ask/native_probe_harness.py:2387) had no
policy file to copy. It is downstream of the first, exactly as the root-cause
note predicted.

The first message is the cause, and it is a second isolation defect of the same
family as the digest: two sides of one contract resolving one path differently.
`NativeTerminalRuntime._frame_token` read only `<socket_dir>/local_cli_token`,
while gterm's `read_local_token` read that file and then fell back to
`$HOME/.gobby/local_cli_token`. Production hides the disagreement because
`terminal_host.socket_dir` defaults to `~/.gobby`, so one file answers both
lookups. The probe gives its host `<runtime_root>/gterm-host`, which holds no
token, so the daemon sent an empty token, gterm compared it against the
operator's, and answered `invalid_token`. The daemon read only the frame type
out of that reply, which is why the symptom reads as "expected welcome, got
error" and names neither the code nor the socket.

Commit b92c554220 makes both sides resolve the socket directory first and Gobby
home second, both honouring `GOBBY_HOME`, and puts the host's error code into
`FrameProtocolError`. tests/terminals/test_runtime_contract.py proves the
pairing: it spawns a real host and fails with the Python half alone.

Immutable `raw-probe.json` was not written: the harness raised before its
record step, so this attempt's evidence is its retained runtime root
`/private/tmp/gobby-ap-z9zbycus` and schema
`gobby_test_askprobe_fd875d56650945fcad63ec44ed5b0785`, both retained.

## Attempt 23: the frame credential holds, and the agent is told to decline

Ran at 20:36 with output directory
`/tmp/gobby-ask-native-probe-12858-twentythird`, against source `b92c554220`,
pinned to `.ask-probe-b92c5542` (gcode
`dc8118720ac50cf78e142156acc112cdb820b6029b7903198841803886507558`, unchanged,
and the rebuilt gterm
`b7ad098377451219b6b12f07bae8caa569a9f81163b8d2cab501a2e4e0b48ac2`).

The frame handshake is fixed. `prepare` and `seed` completed as in attempt 22,
`_preflight_srt` ran to completion in 113.8 ms, and this time the pane survived
its first second: at 20:42:44 the daemon logged `Auto-dismissed trust prompt
for agent 7dd7dea8-… (trust folder)`, which attempt 22 never reached because
its pane was torn down 34 ms after opening. `invalid_token` appears nowhere.

The launch died 30 seconds later:

    20:42:21  Spawn phase timings | _preflight_srt: 113.839
    20:42:44  Auto-dismissed trust prompt for agent 7dd7dea8-… (trust folder)
    20:43:14  Agent 7dd7dea8-… PID 40321 no longer matches agent identity
    20:43:14  Marked agent run 7dd7dea8-… as failed
    20:43:14  gobby-ask/spawn: Ask agent ended as error without a valid submission

with `control/fresh-result.json` recording `typed_error.code = "agent_failed"`
at `current_stage = "investigator"`, and the retained pane capture showing what
the agent was answering:

     ❯ No, exit
       Yes, I trust this folder

     Enter to confirm · Esc to cancel

`TerminalPromptMonitor.check_trust_prompts` sends a bare Enter, which confirms
the highlighted row. Claude Code's workspace trust dialog highlights the
decline, so Gobby told the investigator to quit, and the health check found the
process gone on its next 30-second sweep. Nothing else wrote to that pane, and
a swallowed Enter would have left the agent sitting at the dialog rather than
exiting.

The dialog appears at all for the same reason the previous two defects hid in
production. Claude Code's folder trust is hierarchical and lives in
`~/.claude.json`: `/Users/josh/.gobby` carries `hasTrustDialogAccepted: true`,
so a production Ask workspace under `<gobby_home>/ask/…` inherits it, while the
probe's isolated home under `/private/tmp` inherits nothing. Spawns that pass
`--dangerously-skip-permissions` never see the dialog either; Ask withholds
that flag on purpose so its permission service stays in charge, which is why
Ask is the path that surfaced this.

Commit 40c0287769 derives the key sequence from the visible dialog: it reads
the marked selection block out of the pane, navigates to the row that grants
trust — skipping any row that declines or that trusts the parent directory —
and keeps the bare Enter for panes with no navigable list, so no other
provider's behaviour changes.

This attempt is the first to write an immutable `raw-probe.json` (157 KB), and
its runtime root `/private/tmp/gobby-ap-hsat03zs` is retained.


## Attempt 24: test-database volume exhaustion rejected preparation

Attempt 24 ran from the same session with output directory
`/tmp/gobby-ask-native-probe-12858-twentyfourth`, starting `21:00` and failing
at `21:03` local time, against source `40c0287769` and the same
`.ask-probe-b92c5542` pin, which still applies because that commit changes only
Python.

Like attempt 18 it never reached the launch, and for the same class of reason.
The pipeline failed at its first step:

```
MCP step prepare failed: gobby-ask:prepare returned error:
gcode_index_failed:1:Error: db error Caused by: ERROR: could not extend file
"base/16384/5508766": No space left on device HINT: Check free disk space.
```

The exhausted volume this time is not the host's. The isolated test PostgreSQL
container `gobby-postgres-test-1` keeps its `PGDATA` on a 3.0 GB tmpfs, which
`df` reported at 2.6 GB used with 415 MB free. Three retained probe schemas
account for nearly all of it, each with a small `_agent_auth` sibling:

- `gobby_test_askprobe_122d8ebbbe544508b1ead4c273d91f72`, 603 MB, attempt 19
- `gobby_test_askprobe_fd875d56650945fcad63ec44ed5b0785`, 599 MB, attempt 22
- `gobby_test_askprobe_a1da5505996a448eab76b416d9582345`, 597 MB, attempt 23

Those three figures correct the 960, 954 and 951 MB first recorded here, which
were a measurement error rather than a change on disk: summing
`pg_total_relation_size` over every `pg_class` row counts each index twice, once
inside its own table's total and once as a row in its own right. Restricting the
sum to `relkind in ('r','p')` gives the sizes above, and those reconcile with the
volume where the inflated ones did not — `base` is 2.0 GB, of which these three
schemas are 1.8 GB, alongside 128 MB of WAL and roughly 200 MB of catalog. The
third schema is attempt 23's, identified by the session rows it still holds,
01:36 to 01:42 UTC, matching the 20:36 local start recorded above.

A full private snapshot index of this repository is therefore roughly 600 MB
rather than the gigabyte first inferred, and the tmpfs holds three of them. The
conclusion is unchanged, because 415 MB of free space could not admit a fourth
either way. Every probe attempt retained for its evidence therefore costs the
next attempt its working room, and the retention that made attempts 15 and 19
decisive is what blocked this one.

This is an environmental failure of the test database, not a defect in Ask, and
it carries no evidence about the trust-dialog fix that `40c0287769` landed.
Attempt 24 must not be counted as a policy or launch failure.

Immutable `raw-probe.json` SHA-256 is
`77bbee59482ee0eb0b87c3cb944bf32801c2fe7e8a18c5c30277e8a72c746d4a`;
`complete=true`, zero receipts, zero excluded receipts, no capture errors and
no cleanup errors. The runtime root was removed and schema
`gobby_test_askprobe_3d2c37a00cbf41c5a8eda274a7fa69e0` was dropped, so nothing
from this attempt is retained beyond the probe record itself.

## Attempt 25: recording attempt 24 rejected the seed

Attempt 24 was rejected by the test database's tmpfs, so attempt 25 required
freeing it first. The three retained probe schemas were the volume: 1.8 GB of a
3.0 GB filesystem that `df` showed at 2.3 GB used with 799 MB free, against the
roughly 600 MB a fresh run needs for its own index.

Dropping them was made reversible rather than accepted as a loss. Each schema is
overwhelmingly a derived artifact: of attempt 23's 597 MB, 576 MB is `code_*` —
`code_calls` 350 MB over 475,939 rows, `code_symbols` 99 MB over 125,014,
`code_content_chunks` 96 MB over 30,419 and `code_imports` 19 MB over 43,289 —
and the probe run itself is a single `pipeline_executions` row. The findings
those runs produced live outside the database, in each attempt's immutable
`raw-probe.json`, in attempt 23's retained runtime root on the host volume, and
in this document.

All three were dumped to the host before anything was dropped, streamed through
`docker exec` stdout so nothing was written back onto the exhausted tmpfs:

```sh
docker exec gobby-postgres-test-1 pg_dump -U gobby_test -d gobby_test \
  -n <schema> -n <schema>_agent_auth | gzip > <dest>/attempt<N>-<id>.sql.gz
```

Each dump carries the probe schema and its `_agent_auth` sibling, and each was
verified against the live tables before the drop rather than after:

| attempt | schema | dump | `code_calls` | `code_symbols` |
| --- | --- | --- | --- | --- |
| 19 | `122d8ebb…` | 44 MB | 474,758 | 124,781 |
| 22 | `fd875d56…` | 44 MB | 475,856 | 124,999 |
| 23 | `a1da5505…` | 45 MB | 475,939 | 125,014 |

Every count matched the live schema exactly, and each archive passed `gzip -t`
and declares both `CREATE SCHEMA` statements. SHA-256, under
`~/.gobby/backups/d45545c5-ded5-4335-b115-0245752edacf/ask-probe-schemas/`:

- attempt 19 `a031e1c67669cb3d5e7211d82307384a8772280a250e4c709e83d27bacf420e5`
- attempt 22 `6ca52b23fde6b3dc0b0617295bb958e8d6d8d8af990b74981358584feeab4619`
- attempt 23 `f0e49fbbaba801701d196126255fe6491e0220d6a05aa9e89ece23d0bbd17e7e`

Attempts 19 and 22 were then dropped with their `_agent_auth` siblings, on the
user's explicit instruction, and attempt 23 was left live as the most recent
retained state. Free space went from 799 MB to 2.2 GB, which admits attempt 25's
index and one retry.

Attempt 25 ran against source `92afbbe0c0`. That commit is byte-identical to
`40c0287769` under `src/` and `crates/` — the four commits between them touch
only documentation and tests — so the `.ask-probe-b92c5542` pin still applies
and this is the first run carrying all three fixes at once: the digest
normalizer, the gterm frame credential from `b92c554220`, and the trust-row
selection from `40c0287769`.

```sh
UV_NO_SYNC=1 UV_PROJECT_ENVIRONMENT=/Users/josh/Projects/gobby/.venv PYTHONPATH=src \
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test \
GOBBY_TEST_PROTECT=1 \
GOBBY_NATIVE_BIN_DIR=/Users/josh/.gobby/worktrees/gobby/epic-22010-native-ask/.ask-probe-b92c5542 \
uv run --no-sync python tests/ask/native_probe_harness.py contained-drive \
  --project-root /Users/josh/.gobby/worktrees/gobby/epic-22010-native-ask \
  --output-dir /tmp/gobby-ask-native-probe-12858-twentyfifth --timeout-seconds 1500
```

Preparation passed. `prepare` ran `11:14:53` to `11:20:20` UTC and completed;
the private index built without the disk error that stopped attempt 24. The
run then failed at the next step, eleven seconds later:

```
MCP step seed failed: gobby-ask:seed returned error:
invalid gcode evidence response: response contains credential-bearing content
```

Zero agent runs and zero receipts: nothing launched, so attempt 25 carries no
evidence about the trust-dialog fix either. Execution
`046e24b2-fb53-4892-a422-84b4e5622a9d`; immutable `raw-probe.json` SHA-256 is
`08810e7b7417dec4c6d81b97fad78b36ad118dd08c46f6204eea9e10fb7842c6`,
`complete=true`, no capture errors and no cleanup errors. The runtime root was
removed and schema `gobby_test_askprobe_b47d1c4145ab435490dd34eead808564` was
dropped.

### The cause, and why it is this document's fault

This is the same admission guard that rejected attempt 13, but not the same
defect and not the same surface. `_KNOWN_CREDENTIALS` in
`src/gobby/ask/evidence.py` carried

```
sk-[A-Za-z0-9][A-Za-z0-9_-]{15,}
```

with no left word boundary, alone among the five signatures in that tuple. It
therefore matched inside any sufficiently long word containing `sk-`, which in
this repository means every `ask-` and `task-` slug of fifteen characters or
more: `ask-snapshot-preparation` yields `sk-snapshot-preparation`, and
`queue-task-memory-review-after-close` yields
`sk-memory-review-after-close`. Over the tracked files at `ed9d7c5031` the
unanchored form matches 849 times in 97 files where the anchored one matches 10
times in 8; all 10 are standalone key-shaped literals in test fixtures, which is
what the signature is for, and all 839 of the difference are false positives.

The surface that fired is the snapshot binding, which every gcode evidence
response echoes. Its `commit.changed_paths` entries carry the fields `old_path`
and `new_path`, and `_contains_credential` skips only keys literally named
`path` and `paths`, so those two are walked as response content under the
strict, non-source-reference rule. Attempt 25 pinned `92afbbe0c0`, whose single
changed path is
`docs/evidence/wiki-bakeoff-code-2026-09/ask-snapshot-preparation.md` — this
file. **Recording attempt 24 here is what rejected attempt 25.** Attempts 22
and 23 passed seed only because their pinned commits happened to touch no such
path, which is a property of the commit, not of the corpus or of the guard.

The first diagnosis was wrong and is recorded here because the correction is
the useful part. Simulating the two seed lanes against the live index flagged
three excerpts, which looked like the answer; all three were already excluded
by the Rust checker at inventory time and could never have reached the Python
one. Fetching the real inventory instead, and running the Python classifier
over all 8,050 eligible blobs at `92afbbe0c0`, flagged nothing — which proved
the excerpt surface innocent and left the binding as the only response content
remaining.

The same signature exists twice, deliberately: `contains_known_credential` in
`crates/gcode/src/index/security.rs` decides `ExclusionReason::SensitiveContent`
at blob granularity, and the two texts were identical. That parity was
confirmed empirically, zero disagreements over all 8,050 eligible blobs, which
is what made the Python-side test conclusive for both. The Rust side is the
larger consequence: it drops whole blobs from the snapshot inventory and
`search` retains only items whose path is eligible, so a false exclusion
shrinks the evidence corpus silently, with no error anywhere. 54 of the 276
`sensitive_content` exclusions were false, among them
`src/gobby/ask/stage_runtime.py` and `tests/ask/native_probe_harness.py` —
Ask could not cite its own implementation.

`3c623f9b99` anchors both, with regression cases pinning the real binding
shape rather than a synthetic string, and `ed9d7c5031` splits the new Rust
fixture literal so that file stops matching its own signature, following the
convention `tests/ask/test_evidence.py` already used. After the fix the
attempt-25 binding and both seed requests are admitted, the URI-password and
real key shapes are still rejected, and the inventory goes from 8,050 eligible
with 276 sensitive exclusions to 8,104 with 222.

The unanchored signature arrived in `7946f7bb18`, whose validation was a parity
scan over the 262 blobs the *previous* checker had rejected. That design can
only find newly admitted blobs; it is structurally blind to newly rejected ones,
which is exactly the direction a pattern added by the same commit fails in.

The `old_path`/`new_path` asymmetry against the skipped `path` key was left
alone. It stops being load-bearing once the signature is anchored, and whether
repository paths should be scanned as response content at all is a separate
decision from this defect; a regression case now pins that surface so a future
change to it is deliberate.

## Attempt 26: the launch succeeds, and the trust dialog is answered "No, exit"

Attempt 26 ran from the same session with output directory
`/tmp/gobby-ask-native-probe-12858-twentysixth`, starting `06:50` and failing
at `06:56` local time, against source `ed9d7c5031` and a new pin
`.ask-probe-ed9d7c50` holding the rebuilt `gcode`
(`40aeb1431f207c7ee88247412e36e6f45b5e9583418551051e0903ac7621ec0c`) and the
unchanged `gterm`
(`b7ad098377451219b6b12f07bae8caa569a9f81163b8d2cab501a2e4e0b48ac2`). The
rebuild carries the anchored credential signature; `gcode`'s CLI contract is
byte-identical to `tests/contracts/gcode.contract.json`, still version 10 with
45 commands and 41 error codes.

It is the first attempt to get past the seed, and the first to launch at all.
Three steps, two of them new ground:

| step | window (UTC) | outcome |
| --- | --- | --- |
| `prepare` | `11:50:10` – `11:55:24` | completed |
| `seed` | `11:55:24` – `11:55:53` | completed |
| `investigate` | `11:55:53` – `11:56:46` | failed |

```
MCP step investigate failed: gobby-ask:spawn returned error:
Ask agent ended as error without a valid submission
```

### What attempt 26 proves

**The credential anchor holds.** `seed` completed in 29 seconds against the
same corpus and the same binding shape that rejected attempt 25 — and against
a pinned commit whose one changed path is `crates/gcode/src/index/security.rs`,
the file carrying the signature itself.

**The digest normalizer holds, in a completed launch.** Attempt 22's evidence
for `16be058101` was negative — the absence of "Ask SRT policy semantics
changed after validation" — and its launch policy was gone before the harness
could copy it. Attempt 26 supplies the positive half. The launch receipt records
`launch_complete: true` and `launch_error: null`, and the captured launch
policy (SHA-256
`625a4088d1e1a8801bff97a5c277d7b974dbb4b817d498da674d3498aee1d28a`) carries

```
filesystem.allowRead   /private/tmp/gobby-ap-ne0hsfgx/gobby/gcode-runtime/138d19e7f5c2963c
filesystem.allowWrite  /private/tmp/gobby-ap-ne0hsfgx/gobby/gcode-runtime/138d19e7f5c2963c
```

— the workspace-keyed runtime home that is the whole of the defect. It is
present, it is relabelled rather than hashed verbatim, and
`AskRuntimeProfile.validate_launch` admitted it. Attempts 1–17 and 19 all died
on that entry.

The agent then reached its own workspace, which is where it stopped. `PID 28472
no longer matches agent identity`, with the pane holding

```
 ❯ No, exit
   Yes, I trust this folder

 Enter to confirm · Esc to cancel
```

and the daemon logging, thirty seconds before the health check found the
process gone:

```
Auto-dismissed trust prompt for agent b78eb46a-472f-4a48-92bd-baa047f3b66d
(trust folder) with enter
```

### Why the trust fix did not fire

`40c0287769` taught the monitor to select the row that grants trust before
confirming, and its tests pass. The monitor still answered with a bare Enter,
because `with enter` is what the log records: `trust_dismiss_keys` took its
"no navigable selection list" fallback.

The pane is why. `_selection_options` locates the selected row by testing
whether a line's first visible character is a selection marker, and pane
snapshots are not visible text — the tmux runtime captures with `-e`
specifically to preserve SGR, and the native host's `mode="text"` snapshot is
`recent_unwrapped_ansi`. The highlighted row arrives as

```
 \x1b[0m\x1b[38;5;153m❯ No, exit\x1b[0m
```

so after the box-drawing and padding are stripped the first character is ESC,
no marker is found anywhere in the block, and the fallback Enter confirms the
row the dialog opens on. Every fixture in
`tests/agents/test_prompt_detector.py` was written as visible text, which is
exactly why 279 passing tests did not catch it.

`ecde9874e1` strips the escape sequences before the block is read, the way four
other pane readers in this repository already do, and pins the real pane from
this attempt as a fixture; it fails on the unpatched reader with
`('enter',) != ('down', 'enter')`.

### Retained state

Immutable `raw-probe.json` SHA-256 is
`2292d3beab9a42567c2e0810236a021c3e0c6d9124fa060169924e203f028723`. Unlike
every previous attempt it is `complete=false`: zero receipts and three excluded
(`provider-transcript-and-mcp-responses`, `srt-policy`, `srt-violations`, all
`agent-session-launch-identity-mismatch`), because the agent process was gone
before its receipts could be bound to a live identity. The incomplete export is
what made cleanup retain rather than reap, so runtime root
`/private/tmp/gobby-ap-ne0hsfgx` and schema
`gobby_test_askprobe_46a901259c8247598323c0846b1e9b23` both survive, carrying
the launch receipt, the launch policy and the pane capture quoted above. Three
cleanup errors record the same fact: owned-process liveness unknown, not every
owned process verified dead, export incomplete.

This retention costs the next attempt its working room again — the test
database's tmpfs is back to 675 MB free against the roughly 600 MB a fresh
index needs, the same squeeze that rejected attempt 24.

## Attempt 27: the trust fix holds, and the provider refuses the work

Attempt 27 ran with output directory
`/tmp/gobby-ask-native-probe-12858-twentyseventh`, starting `10:22` and ending
`10:30` local, against source `1b8463b05e` and the unchanged pin
`.ask-probe-ed9d7c50`. The only change reaching the runtime since attempt 26 is
`ecde9874e1`, which is pure Python, so the pinned `gcode`
(`40aeb1431f207c7ee88247412e36e6f45b5e9583418551051e0903ac7621ec0c`) and
`gterm` (`b7ad098377451219b6b12f07bae8caa569a9f81163b8d2cab501a2e4e0b48ac2`)
are byte-identical to the ones attempt 26 ran on. One Python function changed
and nothing else, which is what makes attempt 26 a usable control rather than
merely the previous run.

It first had to get its working room back. The retained schemas from attempts
23 and 26 held about 1.2 GB of the 3.0 GB tmpfs between them, and a fresh index
needs roughly 600 MB. Both were dumped to
`~/.gobby/backups/<project>/ask-probe-schemas/` and each dump was verified
against its live schema before anything was dropped: `code_calls` 475,939 and
483,034, matching exactly, 144 tables, 301 indexes and 144 `COPY` blocks per
dump, each with an intact `pg_dump` terminator. Dropping all four schemas — each
probe schema and its `_agent_auth` sibling — took the database from 1229 MB to
24 MB and left 2.7 GB free.

| step | window (UTC) | outcome |
| --- | --- | --- |
| `prepare` | `15:22:27` – `15:27:46` | completed, 126,696 symbols |
| `seed` | `15:27:46` – `15:28:19` | completed in 33s |
| `investigate` | `15:28:19` – `15:30:33` | failed |

### The trust dialog is answered correctly

The daemon logged, fourteen seconds after the spawn:

```
Auto-dismissed trust prompt for agent 49761af2-d5cf-4cef-9ce7-07d0ec22a7c0
(trust folder) with down+enter
```

Attempt 26's line, from the same code path and the same dialog, ended
`with enter`. That one word is the entire proof, because `with <keys>` is
written from the tuple `trust_dismiss_keys` returns: a bare `enter` is the
"no navigable selection list" fallback, which confirms whichever row the dialog
opened on, and that row is `No, exit`. `down+enter` means `_selection_options`
found the marker through the SGR sequence that highlights its row, counted the
options, and navigated to the row that grants trust before confirming.

The agent survived it. Claude Code came up inside the sandbox at
`…/2565c6b9-f8c4-49e5-988c-8d726952c9d8/scratch/investigator-0` and its pane
carries the full investigation prompt — question, boundary list, evidence
manifest `50208d47ef0294c8959e042181af9b2ddcc5bc9771a70e9a4f02e793e92972c5`.
Every runtime layer this document has been chasing since attempt 1 worked at
once: the policy digest admitted the launch, the frame credential passed, the
seed accepted the corpus, and the trust dialog was answered in favour of the
workspace.

### Where it stopped

The provider declined to do the work:

```
⎿  You've hit your monthly spend limit. Run /usage-credits to manage your
   limit and keep using Fable 5.1 or switch models to continue this chat.
✻ Brewed for 0s · done 10:28 AM
```

Nothing followed. The child session recorded no activity for 134 seconds — zero
hook receipt effects, `updated_at` moving only when the kill wrote it — and
`check_initialization_timeout` killed the run for provider rotation at
`15:30:33`, which is that guard working correctly on an agent that was never
going to start. The pipeline reports the same
`Ask agent ended as error without a valid submission` as attempt 26, from an
entirely different cause.

This is an account quota, not a defect, and no change in this repository moves
it. Raising the monthly limit or pointing the `ask-investigator` profile at a
different model is the precondition for attempt 28 and for all 14 cohort
questions.

The `⚠ Safe mode` banner in the same pane is deliberate and not a second
finding: `ask_provider_args` sets `--safe-mode` together with
`--strict-mcp-config` and an explicit `mcp__gobby__*` allowlist, so the Ask MCP
surface is supplied on purpose rather than disabled.

The export came back incomplete again — `complete: false`, 0 receipts, 3
excluded — so runtime root `/private/tmp/gobby-ap-fjteqd9b` and schema
`gobby_test_askprobe_02e7377c72e445b6b546f4c89b22f13b` are both retained, with
the pane capture, cleanup record and raw probe copied out to the session
scratchpad.

Attempts 1–17 and 19 remain immutable policy failures, and 16be058101 closed
the cause they all share: attempt 22 proved it negatively, by the absence of
that message, and attempts 26 and 27 positively, with a captured launch policy
whose allowRead and allowWrite carry the workspace-keyed runtime home every one
of those attempts died on. Attempts 18 and 24 are immutable environment failures carrying no
policy evidence, the first on the host volume and the second on the test
database's tmpfs. Attempts 20 and 21 are immutable harness and snapshot
failures from the shared build directory, fixed by relocating the pin and by
4c0f6bce05. Attempts 22 through 27 each carry the previous fix forward and
reach one step further: 22 cleared the digest and died at the gterm frame
credential, 23 proved the frame-credential fix and died at
the trust dialog, 24 never launched, 25 died at the seed on a credential
signature that matched this document's own filename, 26 passed preparation,
passed the seed, completed the launch, and died at the trust dialog again —
this time because the monitor was reading the highlighted row through the
escape sequence that highlights it — and 27 answered that dialog correctly and
delivered the investigation prompt into a live sandboxed agent.

Every runtime failure this document records now has a landed fix, and attempt
27 exercised all of them at once without a single one recurring. What stops the
work is no longer in this repository: the probe account has reached its monthly
spend limit, so the investigator is refused before it can act. Raising that
limit, or binding the `ask-investigator` profile to a different model, is the
precondition for attempt 28. Whether a native Ask agent can reach a submission
is still unproven, and all 14 cohort questions remain unrun.
