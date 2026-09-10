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
