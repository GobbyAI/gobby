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
