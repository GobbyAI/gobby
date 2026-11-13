## Research Findings

### Research Questions

- For #24752 Concurrent-test capacity research, which limits bind now, which of the six macOS hypotheses apply, what breaks under concurrent hub-backed pytest, and which fixes should come first?

### Domain Context

The demonstrated bottlenecks are admission latency and a terminal-test isolation defect. Neither the three-run policy nor the available host sample establishes a hardware ceiling of three. Existing isolation already addresses ordinary shared Cargo target directories and Python worker row/schema collisions; preserve those mechanisms.

Read-only research at checkout HEAD 5c29d24f76d9f5336dd4d138b0e0c21926ce177a. No files, limits, services, or processes changed; no cargo or pytest executed. Orchestrator owns task lifecycle and saving this report to .gobby/plans/research/concurrent-tests-2026-10-19.md.

### Evidence & Sources

**1. Binding limits and observed delay**

| Surface | Evidence | Finding |
| --- | --- | --- |
| Hub-backed pytest admission | RM confirms cap 3: two hub-node places and one ordinary place. Orchestrator confirms this reservation is a standing policy, distinct from a technical limit. RM source message aa8db936-9f67-4568-aed1-f480105892c9, 2026-10-19T13:56:15.964768Z; original ORCH ruling eedb7166-a0b1-42b5-be7d-d22513005f8f. | Policy binds eligibility. At the sample, RM assigned all three places, but assigned/reserved is not proof of three simultaneously running commands. |
| Heavy cargo | Current cap 3, two places reserved for hub-node; each needs RM GO and an Orchestrator ARM. Memory b21324f3-b475-55ac-a01f-cf545808b579 and RM/ORCH reports. At 08:52 CDT two ARM leases existed but RM had no live cargo GO. | Admission can hold cargo before execution. An ARM is not an executing cargo job or evidence that CPU capacity is full. |
| Manual dispatch | Node-events READY 08:41:28.591903 CDT to GO 08:51:35.711858 = 607.120s; Rust-TDD READY 08:38:38.333729 to GO 08:52:17.329595 = 818.996s. Their reported executions took 1.30s and 1.69s. RM reports context recovery and manual routing during the interval, with no evidence the hub cap was full. Exact message IDs below. | 23.77 task-minutes of observed READY-to-GO delay across two commands totaling 2.99s execution. This is admission delay, not measured database service time. How much a fix saves remains unmeasured. |
| Shared test hub | ORCH 08:55:47: max_connections=100, max_locks_per_transaction=256, shared_buffers=128 MiB; one active connection, zero lock waits, six databases. | No hub saturation in this snapshot. It is not a stress/capacity proof; peak connections, lock occupancy and memory pressure during overlap remain unmeasured. |
| Host/e2e brackets | RM requires ORCH START/END and no terminal/e2e overlap until #24747 Foreign-host leak guard is fixed. Same-task failure and isolated-pass evidence below. | A correctness barrier presently forces extra serialization independent of CPU count. |
| Host capacity | ORCH 08:52:43: 18 CPUs, 128 GiB RAM; load averages 48.00/37.24/36.84; one-second CPU sample 21% user, 18% system, 61% idle; disk0 6,752 transfers/s, 51.98 MB/s, 7.88 KB/transfer. | Pure CPU saturation is not demonstrated. High load and small I/O warrant correlated sampling; neither load average nor MB/s alone proves CPU, disk, memory, scanning, or a blocked-lock cause. |

Exact admission pairs, supplied from stored messages by RM:
- #24531 Node-events RED READY: 7c599d1c-51a0-48ce-bbc1-40ccd10f7bc6; GO: f7f7b970-1c58-4a2e-a46b-d5a7c96c4cc9, delivered 08:51:37.546015 CDT.
- #24527 Rust-TDD RED READY: a73af600-3aaa-45d6-91ed-78e7b57efa16; GO: ff49bab0-baac-4f57-a8bc-dd47466df51d, delivered 08:52:24.355008 CDT.
- START/DONE message timestamps were not recovered. The 1.30s/1.69s values are reported command durations, not GO-to-DONE durations.

The earlier .gobby/plans/research/close-throughput-2026-10-18.md, sections “Three directly paired queue/retest intervals” and “Slot occupancy over the day,” records 5.90m and 8.30m close READY-to-GO waits and a 3.28m landed-retest READY-to-GO wait. It also records close3/3 and heavy2/3 at 21:50 UTC. This rules out a general claim that RM only ever permits one live run. It does not provide continuous utilization or exclusive attribution. The report explicitly distinguishes reservations, execution, terminal verdict and drain.

**2. Six macOS hypotheses**

| Hypothesis | Applies here? | Evidence and confirming measurement |
| --- | --- | --- |
| Cargo file locks | Ordinary cross-worktree target lock is largely addressed; other lock delays remain unmeasured today. | ORCH measured distinct per-worktree CARGO_TARGET_DIR paths. Shared CARGO_HOME alone does not serialize all compilation: current Cargo uses a shared read lock for builds and exclusive modes for cache mutation/download coordination [Cargo cache locking](https://doc.rust-lang.org/stable/nightly-rustc/cargo/util/cache_lock/index.html). Confirm with exact “Blocking waiting for file lock” output, lock path and ORCH-owned waiter/holder measurements during a stall. Historical nested builds did deadlock in one target (#23769 Frame-live validation, memory 57dabbd1-96d2-5fda-8ae7-a41bddb35003); that is a distinct incident. Current nextest configuration prebuilds the gterm test host in a setup script and serializes frame_source_live. No current nested-cargo deadlock was observed. |
| CPU oversubscription | Configured fanout risk is real; active CPU saturation is unproved. | Seats use CARGO_BUILD_JOBS=3, which is a build-job budget, separate from nextest execution. Repository nextest config has no test-threads setting. The default uses available CPU parallelism; three unoverridden runs on this 18-CPU host can request 54 concurrent test slots, plus child work. Actual command/environment overrides are unmeasured. Groups serialize selected families only within their run. [Nextest running tests](https://nexte.st/docs/running/) and [test groups](https://nexte.st/docs/configuration/test-groups/). Confirm concurrent run settings and compare aggregate completion time and CPU/pressure under one, two and three authorized runs. |
| Open-file limit | Ruled out as a 256-descriptor ceiling for this seat; fleet-wide inheritance remains unmeasured. | Shell builtins here returned soft 1,048,575 and hard unlimited. ORCH shell soft is 1,048,576. launchctl soft 256 does not describe this seat's actual inherited limit. No observed EMFILE/“Too many open files.” Sample limits inside an affected worker if that symptom appears. Do not change global limits based on launchctl alone. |
| Gatekeeper/XProtect scanning | Unconfirmed. | Developer mode disabled is a configuration fact, not a scan-duration measurement. ORCH found zero syspolicyd unified-log lines in 30m, which can be inconclusive because of logging visibility. Developer Tools permission is app-specific [Apple guidance](https://support.apple.com/en-euro/guide/mac-help/mchlc5fb7f9c/27/mac/27). Confirm with ORCH-owned launch latency and security-process attribution during fresh test-binary execution; do not infer that DevToolsSecurity status proves the relevant app permission or explains the queue. |
| Spotlight/Time Machine | Exposure exists; attributable slowdown is unmeasured. | ORCH: Spotlight enabled on Data volume and cargo-target root included in Time Machine; 57 target directories total 492 GB. Volume indexing enabled does not prove this path is unexcluded or actively indexed during a stall. Confirm path exclusions and overlapping indexer/backup I/O. Target-only exclusions are supported by [Spotlight Privacy](https://support.apple.com/en-ie/guide/mac-help/mchl1bb43b84/27/mac/27) and [Time Machine exclusions](https://support.apple.com/en-ie/guide/mac-help/mh15622/27/mac/27). |
| Full debuginfo/link cost | Default configuration applies; attributable minutes are unmeasured. | Cargo.toml has no dev/test debug override. Cargo test inherits dev, whose default debug level is full; environment/config overrides were not measured. On macOS Cargo defaults split-debuginfo to unpacked, so do not assume every run spends its time in dsymutil. [Cargo profiles](https://doc.rust-lang.org/cargo/reference/profiles.html). Measure authorized build timing, artifact sizes, link/debug-generation phases and an equivalent reduced-debug experiment before claiming savings. |

Nextest config was read directly because gcode evidence returned invalid_selector: the .config/nextest.toml path is not visible in this caller's index. Raw citation: .config/nextest.toml:1-89. Relevant exact lines: 6| command = ["cargo", "build", "-p", "gobby-terminal", "--features", "vt-engine", "--bin", "gterm"]; 38| ghook-process = { max-threads = 2 }; 39| serial-db = { max-threads = 1 }; 40| gterm-pty = { max-threads = 1 }; 41| gdaemon-lease = { max-threads = 1 }. Lines 19-26 also document the macOS inherited capture-pipe race and the ten-second leak timeout. That pipe leak is distinct from the foreign-host guard bug.

**3. What concurrent hub-backed tests share**

Python fixtures already use PID/worker/nonce-specific schemas and schema-keyed advisory locks; daemon fixtures can create uniquely named throwaway databases. A shared TCP port for the server is not itself a port-binding collision. Tests still share PostgreSQL connection/lock/memory resources and some cluster-wide roles. The xdist auto-worker cap is eight per invocation, not a machine-wide cap: three invocations using auto could request 24 workers, but the cited tiny RM commands did not establish that they used xdist.

There is an explicit Rust database bottleneck independent of Python schema isolation: lease/lifecycle cases serialize on the same advisory namespace in gobby_test, with a 30-second acquisition timeout. Separate nextest invocations each have their own group semaphore and can still contend on this database lock. Current serial-db/gdaemon-lease limits therefore protect real fixture constraints; removing their limits would not create isolation.

Confirmed foreign-host failure: #24747 Foreign-host leak guard records a landed run at 2ae5cc34ca with 238 PASS and one teardown error after transient host 66298 ended during another seat's isolated E2E. An isolated rerun passed 238. The guard classifies hosts outside this run's known temporary roots as durable, which admits another sandbox's temporary host into the production-host check. The task is already claimed in lane 5. Its isolated proof bracket ran 07:50:03–07:55:38 CDT (5m35s); that is a bracket window, not an exact test-runtime measurement.

No current port-collision, hub connection-exhaustion, lock-table-exhaustion or tmpfs-full failure was established. These remain measurement targets, not diagnosed causes.

**Verified Docker/fsync correction**

ORCH host-side inspect at 09:03 CDT confirms HostConfig.Tmpfs mounts /var/lib/postgresql with size=3g,nr_inodes=6000000, and PGDATA=/var/lib/postgresql/18/docker. Mounts has no bind/volume entries; that does not negate the separate Tmpfs field. Memory=0 and NanoCpus=0 mean no explicit container cap. Docker Desktop VM has 18 CPUs and 16.74 GB RAM; DockerRootDir=/var/lib/docker, overlayfs, ShmSize=64 MiB.

Thus fsync=on/synchronous_commit=on do not establish physical disk0 WAL-flush I/O: PGDATA is on tmpfs in the VM. It is not a direct disk-backed PostgreSQL fsync explanation for the host's disk0 sample. Docker tmpfs can be swapped, so indirect VM/swap I/O remains possible and unmeasured [Docker tmpfs](https://docs.docker.com/engine/storage/tmpfs). The relevant VM memory domain is 16.74 GB, not all 128 GiB host RAM. Runtime tmpfs occupancy, VM memory/swap pressure and the host backing-disk file/allocation were not measured. Disabling fsync is not supported by this evidence.

**Retained artifacts and logs**

With explicit ORCH read authorization, parsed all 30 available worktree .nextest/*/junit.xml files using read-only XML parsing. Twenty-nine root records had zero failures/errors. One dated Oct11 is a semantic evidence-contract assertion failure, not resource exhaustion. Examples:
- lane-4-close-out/default/junit.xml: run 61f1af05-9a7c-4c21-9460-efd776255198, Oct19 05:38:06.175 CDT, 94 tests, 3.367s, zero failures/errors.
- lane-4-close-out/ci/junit.xml: 6e3036de-0c60-45ff-808b-faa39f810266, 25 tests, 26.637s.
- lane-5-24584-receipt-sweep/default/junit.xml: bb5594b2-40dd-4e14-afb4-611f2ad13503, 33 tests, 44.299s.
- lane-1-runbooks-1/default/junit.xml: b6187161-9f63-4852-8c78-c4f91e739050, Oct11, evidence::tests::bound_search_rejects_live_index_empty_results.

These are last retained results per profile, not an attempt census, overlap proof or benchmark. They do not include Cargo compilation or admission wait and can overwrite previous failures. No local .nextest existed in the researcher tree.

A bounded keyword scan of current daemon.log and errors.log found a production send_message “deadlock detected” at errors.log:89179, Oct19 06:12:47. It does not establish isolated-test-hub contention. No absence claim is made about historical test logs or rotated files.

**4. Ranked fixes**

Ranking follows observed delay where available. No change has been applied, so no realized saved-minutes were measured.

| Rank | Proposal and owner | Classification | Observed delay / expected gain |
| --- | --- | --- | --- |
| 1 | Shorten eligible READY-to-GO handling in the existing RM flow: retain exact-command grants/reservations, use complete packets and durable coordination state across compaction, and dispatch independent host-free packets together. RM/ORCH own workflow; lane 1 owns any code needed to persist/advance the existing admission flow. | Config/workflow; code lane 1 if required. | 23.77 task-minutes observed for two sub-two-second runs; earlier report adds separate 5.90m/8.30m/3.28m waits. Recoverable fraction and realized savings unmeasured. This is the strongest measured throughput opportunity. |
| 2 | Finish already-owned #24747 Foreign-host leak guard, then validate overlapping temporary-host exit while preserving own-host leak and production-host loss detection. Lane 5. | Code, lane 5. | One extra 238-test rerun and a 5m35s isolated proof window observed. Exact avoidable runtime unmeasured. Removes a demonstrated reason for host-test serialization; it does not authorize removing all host brackets. |
| 3 | Make nextest execution threads explicit in admitted commands, separately from build jobs; benchmark one/two/three runs under the unchanged heavy cap and select a shared host budget. Keep resource groups. Lane 4/RM. | Config; code lane 4 only if existing launch flow needs to carry the setting. | Default fanout can reach 54 test slots for three unoverridden runs. Saved minutes unmeasured; the 61%-idle snapshot means a CPU-only explanation is unsupported. |
| 4 | Benchmark reduced dev/test debuginfo, such as limited or line tables, against the same scoped tests and diagnostic needs. Lane 4 owns Cargo profile configuration. | Config, lane 4. | Full-debug defaults and 492 GB of targets create a plausible build/storage cost. No phase timings or savings measured. |
| 5 | Verify path-specific exclusions, then exclude rebuildable cargo targets from Spotlight and Time Machine if currently included. Route the concrete operator action through the Assistant. | Operator action / host config. | 492 GB across57 directories exposed to backup/indexing policy. Scan/backup overlap and saved minutes unmeasured. |
| 6 | Measure Docker VM memory/swap and 3 GiB tmpfs use during authorized overlapping tests. If constrained, tune the isolated test environment based on that evidence; preserve the live hub. ORCH owns measurements, lane 5 owns test-stack config. | Config, lane 5; operator action for Desktop VM changes. | 16.74 GB VM memory and 3 GiB tmpfs are verified domains. No exhaustion observed or savings measured. |
| 7 | Preserve checkout-specific target directories. Audit ownership/age of the57 target directories and reclaim only confirmed inactive build caches through the existing lifecycle. Lane 4/ORCH. | Config/maintenance; code lane 4 for an established cleanup defect. | 492 GB inventory measured; inactive fraction, rebuild penalty and saved minutes unmeasured. A common writable target would reintroduce cross-branch interference. |
| 8 | For Rust shared-role/database-lock families, distinguish genuine shared-resource tests from fixtures that can be isolated per run; retain exclusion for the former. Lane 5 with lane 4 Rust ownership. | Code, lanes 5/4 after scoped fixture analysis. | Thirty-second lock timeout is verified; no current cross-run timeout sampled. Throughput gain unmeasured. |
| 9 | Consider app-specific Developer Tools permission only after fresh-launch scanning is implicated; send any request through the Assistant. | Operator action. | No attributable delay measured. Lower priority than confirmed admission and isolation defects. |

Raising pytest/heavy caps is an operator policy decision via the Assistant. Establish reliable overlap and aggregate throughput before proposing a new value. No limit changes are requested by this report.

**Repository evidence ledger**

All following gcode evidence reads were complete, against the checkout HEAD above; hashes are excerpt_hash values. Selected numbered lines omit leading indentation; hashes identify the complete exact excerpts.
- tests/fixtures/postgres.py:541-577, hash 7f528ded1367c01461fcf6e9105810b9c1ce71c65d15fc5add77e5c7d7517fa1: “551| nonce = uuid.uuid4().hex[:6]”; “576| conn.execute("SELECT pg_advisory_lock(hashtext(%s))", (schema,))”. Per-worker schema uniqueness and lock key.
- tests/fixtures/postgres.py:273-290, hash b02ddc04daff33e39ce794def71f61784a3f315c374ab7fe9ba099b17a50a8e4: “276| database_name = f"gobby_test_{os.getpid()}_{uuid.uuid4().hex[:12]}"”. Throwaway database isolation.
- tests/conftest.py:251-254, hash 4864d32ff14ce48009225e4037b6ca3af26f64c0b02d6ac79bd3ec8d953a1ab9: “254| return min(os.cpu_count() or 1, 8)”. Per-invocation xdist auto budget.
- src/gobby/guard_set_g.py:118-144, hash 559d806805b9c399cd0a1383d40131c927700da7817fa71bdc977c1de534608f: “131| if not any(path_under(socket_dir, root) for root in temp_roots)”. Combined with tests/terminals/conftest.py:87-102, hash 8e101036fcbe530fd990c15b64023126123dd1cecc74729b1bdaab720b01c9b3, “96| for pid, socket_dir in _durable_hosts(before, temp_roots).items()”.
- crates/gdaemon/tests/lease.rs:217-246, hash c84f5c4c7966d956841351ccd52aef75436c08fa61362d12b95f165db69d4d0e: “235| Duration::from_secs(30),”; “236| admin.simple_query("SELECT pg_advisory_lock(hashtext('gdaemon-lease-tests'), 1)"),”. Explicit protected60892 database requirement and cross-process exclusion.
- docker-compose.test.yml:13-29, hash 1d182fb7ae0b3d210bb995e9107d3f97fa27b585b0e60bf8d97ff1d34a61ef14: “18| - max_locks_per_transaction=256”; “27| - /var/lib/postgresql:rw,size=3g,nr_inodes=6000000”. Runtime readback supplied independently by ORCH.
- Cargo.toml:1-38, hash bf7f72e537daece17929e9f9b4e6e3eb8c815695ebd4fc34752c933a4df55d71. Full manifest has release overrides and “34| [profile.dev.package.sha2]”, with no dev/test debug-level override.

### Risks

- A snapshot, reservation or ARM must not be presented as actual concurrency or sustained saturation. JUnit reports exclude queue/build time and are overwrite-prone.
- Default nextest fanout and full debuginfo are configuration findings; per-run overrides, link costs and attributable savings remain unmeasured.
- Docker tmpfs removes the direct PostgreSQL physical-fsync explanation; VM swap and other sources of disk0 traffic remain unmeasured.
- The Docker socket and ~/.docker/config.json were denied inside this seat's sandbox. Reported that SRT denial to ORCH in message7ab52328-dc81-4f99-a58d-9fd3380430c0; ORCH supplied readback. No bypass attempted.
- Researcher claim was denied by the installed serve step. ORCH explicitly corrected the assignment: no claim/write/close by this seat; send the full report for ORCH to persist and close.
