# Review: support infra (utils + install + runner_init + telemetry + savings)

- **Scope:** `src/gobby/utils/`, `src/gobby/install/` (incl. `shared/hooks/validate_settings.py`, `shared/skills/impeccable/.upgrade/transform.py`), `src/gobby/runner_init/`, `src/gobby/telemetry/`, `src/gobby/savings/` (~8.5k lines)
- **Reviewer:** Claude Fable 5 — 6-agent fan-out (utils-core, session/git/daemon-client, utils-small, install, runner_init, telemetry+savings); every Blocker independently re-verified against source by the synthesizer
- **Commit / branch:** `af6b8b4d3` / `0.5.0`
- **Summary:** 4 Blocker · 43 Important · 48 Nit — functionally sound on happy paths, but built to degrade silently: diagnostics that lie when systems are unhealthy, startup that amputates features without signal, an unattended binary updater weaker than the manual install path it shadows, and telemetry/savings numbers that cannot be trusted as shipped.

## Findings

### [BLOCKER] Re-init from a subdirectory plants an id-less `.gobby/project.json` that permanently shadows the real project root

- **Where:** `src/gobby/utils/project_init.py:163,170-173`; `src/gobby/project_verification/refresh.py:71-88,186-209`; `src/gobby/project_verification/evidence.py:143-146`; `src/gobby/utils/project_context.py:49-68`
- **Failure mode:** `initialize_project` detects "already initialized" via `get_project_context(cwd)`, which walks **up** to the real root (`find_project_root`, project_context.py:49-68) — but then calls `refresh_project_verification_deterministic(cwd, fix=True)` with the **unresolved** cwd. `collect_evidence` resolves the passed path and treats it as the root (evidence.py:145), and `_write_verification` does `mkdir(parents=True)` + write (refresh.py:187), creating `subdir/.gobby/project.json` containing only `{"verification": ...}` — no project id. Reproduced end-to-end: after `gobby init` in a `web/` subdir containing a package.json, `find_project_root(web)` returns `web/` and `get_project_context(web)["id"]` is `None`. A second init from that subdir sees a context without an `id` and creates a brand-new project named after the subdir, splitting one repo into two projects.
- **Why it matters:** Silent corruption of project resolution for every hook/session/tool running under that subdir, plus duplicate-project creation — while the command reports success ("Project already initialized").
- **Minimal fix:** In the re-init branch, refresh against the resolved root (`Path(project_context["project_path"])`), not `cwd`.
- **Confidence:** high — reproduced live; the walk-up vs treat-as-root seam verified in source by the synthesizer.

### [BLOCKER] Re-initializing a soft-deleted project name always crashes on the UNIQUE(name) constraint

- **Where:** `src/gobby/utils/project_init.py:200,230-234`; `src/gobby/storage/projects.py:159-167` (get_by_name filters `deleted_at IS NULL`), `:137-143` (plain INSERT, no ON CONFLICT), `:289-300` (soft_delete keeps the name); `src/gobby/storage/postgres_baseline_schema.sql:8` (`name TEXT NOT NULL UNIQUE`); deleters: `src/gobby/cli/projects.py:176`, `src/gobby/servers/routes/projects.py:267`
- **Failure mode:** `gobby projects delete` soft-deletes (sets `deleted_at`; the name stays in the unique index). `initialize_project` checks `get_by_name(name)` — which excludes soft-deleted rows — gets None, and proceeds to `project_manager.create(...)`; psycopg UniqueViolation propagates raw and `gobby init` exits 1 with an opaque DB error (`src/gobby/cli/init.py:53-55`). No migration relaxes the constraint (verified against `src/gobby/storage/migrations.py`).
- **Why it matters:** A normal user flow — delete a project, init the same directory again — is deterministically broken with no recovery path short of renaming the directory or hand-editing the DB.
- **Minimal fix:** In `initialize_project`, also look up `get_by_name(name, include_deleted=True)` and undelete/adopt the row or surface a clear actionable error; alternatively catch UniqueViolation and re-fetch.
- **Confidence:** high — every hop verified in source by the synthesizer (lookup filter, plain INSERT, soft-delete semantics, schema constraint).

### [BLOCKER] `llm_tracing` config is dead — the daemon never calls the init path that activates LLM instrumentors

- **Where:** `src/gobby/runner_init/storage.py:22,43` (imports `init_telemetry` from `gobby.telemetry.logging`); `src/gobby/telemetry/__init__.py:66-100` (the only `init_telemetry` that wires `setup_llm_instrumentors`); `src/gobby/telemetry/logging.py:237-260` (the daemon's version — no instrumentor wiring)
- **Failure mode:** Setting `telemetry.llm_tracing.enabled: true` does nothing in a running daemon. Repo-wide proof: the only production caller of any `init_telemetry` is `runner_init/storage.py:43`, importing from `gobby.telemetry.logging`; nothing in src imports the package-level one (`git grep "from gobby.telemetry import"` → only `inject_into_env` and `shutdown_telemetry`). The `LoggingInstrumentor` bridge at `__init__.py:100` is equally unreachable. The LangWatch epic's validation criteria ("init_telemetry calls setup_llm_instrumentors when llm_tracing.enabled") were validated against the wrong function.
- **Why it matters:** A shipped, documented, validated feature is silently nonfunctional.
- **Minimal fix:** Have `gobby.telemetry.logging.init_telemetry` (the one the runner calls) perform the `llm_tracing` block, or make the package-level function the single entry point and delete the duplicate.
- **Confidence:** high — import chain and both function bodies verified by the synthesizer.

### [BLOCKER] `capture_content=False` is a no-op — if LLM tracing is ever activated, prompts/completions are captured by default

- **Where:** `src/gobby/telemetry/instrumentors.py:59` (`instrumentor_cls().instrument(enrich_token_usage=True, capture_content=capture_content)`); `src/gobby/telemetry/config.py:21-25` ("privacy-first default: off")
- **Failure mode:** Verified against the installed packages: `AnthropicInstrumentor._instrument` never reads `capture_content`; content capture is governed by `should_send_prompts()`, which defaults to **true** unless env `TRACELOOP_TRACE_CONTENT=false`. Also `enrich_token_usage` is a *constructor* arg (`AnthropicInstrumentor(enrich_token_usage=...)`), not an `instrument()` kwarg — and OpenAI's instrumentor doesn't have that parameter at all. Both kwargs are silently swallowed. Test gap: `tests/telemetry/test_instrumentors.py:47-69` asserts the kwargs on a `MagicMock`, mocking the assertion away.
- **Why it matters:** The documented privacy contract is inverted: the moment the dead init path (Blocker above) is fixed, full prompt and completion text flows into spans, into the local `spans` table, and to any configured OTLP endpoint.
- **Minimal fix:** Set `TRACELOOP_TRACE_CONTENT` ("false" unless capture_content) before instrumenting, and pass `enrich_token_usage` to the constructor; add a non-mocked test against the real instrumentor.
- **Confidence:** high — synthesizer re-verified the installed `AnthropicInstrumentor.__init__` signature, the absence of `capture_content` in `_instrument`, and the `should_send_prompts()` default via live introspection.

---

### [IMPORTANT] Catch-and-None is the runner_init package's architecture — nine services degrade to None with no aggregated signal

- **Where:** `src/gobby/runner_init/services.py:43-49` (llm_service), `:84-134` (memory stack), `:192-207` (memory_sync), `:220-233` (task_validator), `:236-255` (project context, DEBUG-only); `src/gobby/runner_init/orchestration.py:81-89` (workflow_loader), `:134-143` (agent_runner), `:147-162` (lifecycle monitor), `:175-312` (cron/dispatch), `:314-329` (communications)
- **Failure mode:** Every init failure becomes `None` plus one log line (often without traceback); every downstream consumer nil-guards; the daemon reports healthy with an arbitrary subset of subsystems off. Second-order skips log **nothing**: when memory died, an explicitly user-enabled `memory_sync.enabled=true` is skipped silently (services.py:197 has no else); task validation gates off with no signal (services.py:223). Tests codify the swallow contract (tests/test_runner_init.py:362,454,556,593,618,659) — none assert a user-visible degraded-state signal. This confirms the systemic question from `docs/reviews/llm-prompts.md`: the audio-binding → `llm_service=None` collapse is not a one-off; it is the package's design.
- **Why it matters:** A Gobby daemon is "healthy" whenever Postgres is up, regardless of how much of Gobby actually started; the only diagnostic is grepping the log.
- **Minimal fix:** Keep the degrade-don't-crash posture but (a) log every dependent skip at warning, (b) log all init failures with `exc_info=True`, (c) record failed/skipped service names on the runner (e.g. `runner.degraded_services`) and expose them via the health/status route.
- **Confidence:** high

### [IMPORTANT] Single broad try makes cron + dispatch automation all-or-nothing, with a misleading one-line failure log

- **Where:** `src/gobby/runner_init/orchestration.py:175-312` (assigns at 184-190, 209-216, 231-233, 305-309; catch at 311-312); consumers: `src/gobby/runner_lifecycle_subsystems.py:273,313`
- **Failure mode:** Any exception in the 130-line block — `CronJobStorage`, `CronExecutor`, `SystemAutomationLoop`, `LocalProjectManager`, or `CronScheduler` — is caught at 311 and logged as `"Failed to initialize CronScheduler: {e}"` with no `exc_info`. The lifecycle nil-guards then silently skip starting cron and the automation loop: every scheduled job (Linear sync, GitHub triage, memory dream, wiki) and the entire `gobby build` dispatch heartbeat are dead while the daemon runs and serves HTTP normally. Partial state is possible: if `CronScheduler` (305) raises, `system_automation_loop` (set at 209) survives and starts while `cron_scheduler` stays None and registered handlers are orphaned on a dead executor.
- **Why it matters:** A single bad cron row turns off all task automation for the daemon's lifetime with one mislabeled log line.
- **Minimal fix:** Split into per-subsystem try/excepts, log each with `exc_info=True` and an accurate message, and reset dependent fields to None when their chain failed.
- **Confidence:** high

### [IMPORTANT] Incomplete embedding config silently disables the entire memory stack — but only when the LLM service is healthy

- **Where:** `src/gobby/runner_init/services.py:92-93` (validation gated on `runner.llm_service`), `:63-67` (raise), `:133-134` (swallow → `memory_manager`/`vector_store` stay None); `src/gobby/search/embeddings.py:746-749` (`is_embedding_configured` False for defaults); cascades: services.py:196-197 (memory_sync skipped), orchestration.py:285-286 (dream cron skipped), orchestration.py:169, services.py:149
- **Failure mode:** With a working LLM service but no embedding endpoint configured, `_validate_memory_embedding_config` raises before `VectorStore` is constructed; the broad except converts it to one log line and the whole memory system — storage, recall, sync, dream — is off. The asymmetry proves incoherence: when `llm_service` is None the validation is *skipped* and `MemoryManager` is built with `embed_fn=None` (services.py:92,99-100,116-132) — memory demonstrably works without embeddings, yet a config-completeness check kills it entirely. No test covers this path.
- **Why it matters:** A user who skipped the embedding installer step loses all memory features with a single startup log line; deterministic and permanent until restart-with-fix.
- **Minimal fix:** On validation failure, log a warning and construct `MemoryManager` with `embed_fn=None` (degrade embeddings only); add a test asserting memory survives unconfigured embeddings.
- **Confidence:** high (mechanism), medium (frequency)

### [IMPORTANT] Trace WebSocket broadcasting is dead code: the exporter callback runs on the BatchSpanProcessor thread where `get_running_loop()` always raises

- **Where:** `src/gobby/runner_init/storage.py:115-134` (`_broadcast_proxy`; `asyncio.get_running_loop()`/`create_task` at 119-120; RuntimeError → debug at 133-134); `src/gobby/telemetry/providers.py:59-65` (exporter wrapped in `BatchSpanProcessor`); `src/gobby/telemetry/span_store.py:31-47`
- **Failure mode:** `BatchSpanProcessor` invokes `export()` on its own worker thread, which has no running event loop, so every broadcast attempt raises RuntimeError and is swallowed at debug level ("Trace broadcast skipped (no running loop)"). Live trace events never reach the WebSocket UI; the feature is wired but can never fire. Even on-loop, `runner._pending_tasks.add(task)` from a foreign thread would be a thread-safety hazard. Zero tests exercise the seam. (Flagged independently by both the runner_init and telemetry reviewers.)
- **Why it matters:** A whole observability feature silently no-ops; the debug message normalizes the failure.
- **Minimal fix:** Capture the daemon loop at init time (inside `run_gobby`'s loop) and use `asyncio.run_coroutine_threadsafe(...)` in `_broadcast_proxy`; don't touch `_pending_tasks` cross-thread.
- **Confidence:** high

### [IMPORTANT] `_init_code_indexer` catches an exception its try-body cannot raise, while real failures crash daemon startup

- **Where:** `src/gobby/runner_init/services.py:137-161` (`except GcodeGatewayError` at 160-161); `src/gobby/code_index/gcode_gateway.py:133-143`, `src/gobby/code_index/context.py:40-57`, `src/gobby/code_index/storage.py:29-31` (constructors only assign fields)
- **Failure mode:** (a) The `GcodeGatewayError` handler is unreachable — none of the constructors in the try raise it — so the intended degrade path is dead. (b) Any real exception (ImportError from the lazy imports at 141-143, future constructor changes) escapes → `GobbyRunner.__init__` (`src/gobby/runner.py:176`) and aborts the daemon, unlike every sibling `_init_*`.
- **Why it matters:** Error handling is inverted relative to intent; the most tolerable service to lose is the one wired to crash startup.
- **Minimal fix:** Broaden the except to `Exception` with `exc_info=True`, matching siblings; probe gcode availability explicitly inside the try if it should gate the indexer.
- **Confidence:** high

### [IMPORTANT] HTTPServer silently re-creates the LLM service into the ServiceContainer, diverging from `runner.llm_service`

- **Where:** `src/gobby/runner_init/servers.py:43`; `src/gobby/servers/http.py:86-94` (`if not services.llm_service ... create_llm_service(...)`); stranded consumers: `src/gobby/runner_init/services.py:119,223`, `src/gobby/runner_init/orchestration.py:123,170,296`
- **Failure mode:** When `_init_llm_service` swallows a failure, `HTTPServer.__init__` retries the identical construction and, if it succeeds, assigns only to the container. HTTP routes then see a live LLM service while every daemon-resident feature constructed in phases 2-3 holds None forever — two sources of truth in one process.
- **Why it matters:** Asymmetric degradation ("/api/llm works but summaries/validation/expansion don't") that partially masks the catch-and-None defect flagged in the llm-prompts review.
- **Minimal fix:** Delete the fallback in `HTTPServer.__init__` (the runner is the single construction point), or write back through a shared setter.
- **Confidence:** high

### [IMPORTANT] Pipeline executor and heartbeat are pinned to the daemon's startup-CWD project; non-project startups log a spurious ERROR every boot

- **Where:** `src/gobby/runner_init/orchestration.py:111,117-119,195-196,206-207`; project_id source: `src/gobby/runner_init/services.py:247-249` (`get_project_context(Path.cwd())`)
- **Failure mode:** A daemon started outside a project directory gets `project_id=None` → no pipeline executor or execution manager, and `SystemAutomationLoop` receives `pipeline_heartbeat=None` — for *all* projects the daemon serves, since this is process-wide. The expected non-project state is reported as ERROR ("pipeline_execution_manager required for heartbeat") via raise-as-control-flow.
- **Why it matters:** Cross-project capability loss keyed on which directory `gobby start` ran from, plus guaranteed error-log noise in a legitimate configuration. Same shape as the wiki-cron startup-CWD coupling from prior reviews.
- **Minimal fix:** Distinguish "no project at startup" (skip with info log, no raise) from real failure; longer term, resolve the execution manager per-project.
- **Confidence:** high (mechanism), medium (frequency)

### [IMPORTANT] HTTP bind failure: uvicorn's `sys.exit(1)` escapes via an unobserved task, bypassing graceful shutdown and PID cleanup

- **Where:** construction `src/gobby/runner_init/servers.py:105-110`; `src/gobby/runner_lifecycle.py:130-138,148,173-174,191,193` (cross-layer)
- **Failure mode:** On EADDRINUSE, uvicorn 0.40.0's `Server.startup` calls `sys.exit(1)` (verified in-env); the SystemExit raised inside the bare `create_task`'d `server_task` kills the process without `shutdown_daemon_services` running — no WS drain, no agent-run cancellation, stale PID file left behind (`except Exception` at 191 cannot catch SystemExit).
- **Why it matters:** Port conflicts are routine operator error; the failure path skips the entire teardown contract.
- **Minimal fix:** Attach a done-callback to `server_task` that converts an early exit into `runner._shutdown_requested = True` plus a clear log.
- **Confidence:** medium — uvicorn behavior verified; asyncio SystemExit propagation reasoned, not executed.

### [IMPORTANT] Freshness updater downloads and promotes binaries with no checksum verification, while the install-time path for the same artifacts verifies `.sha256` fail-closed

- **Where:** `src/gobby/install/bin_freshness_github.py:118-128`; `src/gobby/install/bin_freshness_updater.py:250-261`; contrast `src/gobby/cli/install_setup.py:200,206,268-272`
- **Failure mode:** The daemon's background updater trusts `browser_download_url` plus TLS alone; `gobby install` for the exact same release artifacts fetches and verifies the published SHA-256 digest and refuses on mismatch. A corrupted/truncated/poisoned asset is installed over the live binary by the daemon path but rejected by the install path. Also no size cap on `resp.read()` (github.py:123).
- **Why it matters:** Two layers enforce different integrity contracts for the same supply chain; the unattended, auto-running layer is the weaker one.
- **Minimal fix:** Fetch `f"{asset.asset_url}.sha256"` in `download_asset` (or `_stage_and_promote`) and verify before `os.replace`, reusing the install_setup parsing helper.
- **Confidence:** high

### [IMPORTANT] `_is_up_to_date` is unreachable in the only branch that calls it, enabling perpetual hourly re-downloads

- **Where:** `src/gobby/install/bin_freshness_updater.py:116,166-175,177-186,233-237`; `src/gobby/install/bin_freshness_inspector.py:76`; `src/gobby/install/bin_freshness_models.py:94-97`
- **Failure mode:** `floor_satisfied` (updater:116) and `inspection.floor_drift` (inspector:76) are computed from the identical expression, so `floor_satisfied == not floor_drift` always. The floor-satisfied early return (166-175) means line 177 is reached only when `floor_drift` is True — and `_is_up_to_date` short-circuits to False on `floor_drift`, so the `up_to_date` record at 178-186 and the version comparison at 236-237 are dead code. Whenever the floor pin is ahead of the newest published release, or the installed binary's probed version is unparseable (probe takes the last whitespace token, version_probe.py:64-68), every freshness cycle (default 3600s) re-downloads the same artifact, replaces the live binary, and records status `"updated"` — success reported while nothing changed, forever.
- **Why it matters:** Hourly multi-MB downloads, hourly live-binary replacement (PermissionError churn on Windows), and a DB audit trail of fake "updated" rows masking the floor violation.
- **Minimal fix:** Drop the `floor_drift` short-circuit in `_is_up_to_date` (compare versions directly) or check `compare_versions(installed, asset.version) >= 0` before staging; add a test for pin-ahead-of-release.
- **Confidence:** high

### [IMPORTANT] Per-tool update lock is not honored by the other writers of `~/.gobby/bin`, and those writers overwrite the live binary non-atomically

- **Where:** `src/gobby/install/bin_freshness_locks.py:89-108`, `bin_freshness_updater.py:96-101` (updater takes the lock); `src/gobby/cli/install_setup_gcode.py:139,230` (`copy2` straight onto the live path), `src/gobby/cli/install_setup.py:151,161` (`dest.write_bytes(...)` in place)
- **Failure mode:** `git grep try_acquire_native_bin_lock src/` returns only the bin_freshness modules — `gobby install` writes the same binaries with no lock and no staged rename. A user running `gobby install` while the daemon's hourly cycle is promoting the same tool races: the daemon's `os.replace` can land mid-`write_bytes`, or the installer's truncate-and-write leaves a torn binary visible to any concurrent exec.
- **Why it matters:** The lock exists precisely to serialize binary swaps; the most likely concurrent writer bypasses it, and the installer side is the exact corrupted-binary-on-crash hazard the updater's staging dance avoids.
- **Minimal fix:** Make install_setup's path acquire the lock and reuse the updater's staged extract + `os.replace` (the two extraction functions are near-duplicates already).
- **Confidence:** high

### [IMPORTANT] Committed bundled-content manifest has drifted from the shared tree at HEAD (9 files)

- **Where:** `src/gobby/install/bundled_content_manifest.json` (last touched c8b67e606) vs `src/gobby/install/shared/**` (af6b8b4d3); consumer `src/gobby/sync/integrity.py:173-218`; enforcement `src/gobby/cli/sync.py:84-121`
- **Failure mode:** Regenerating via `build_bundled_content_manifest(...)` against a clean tree shows 9 hash mismatches (python/javascript skill files, 5 skill-discovery rules, completion-readiness rule). In non-dev mode without git fallback, `_verify_manifest_integrity` marks these dirty and `gobby sync` blocks the affected content types as "tampered". Wheel builds regenerate the manifest, limiting blast radius to source/sdist-without-rebuild flows — but the project's own regenerate-on-edit convention was skipped in the latest commit.
- **Why it matters:** Drift detection's whole contract is "manifest == tree"; HEAD violates it, so the gate misfires on pristine content.
- **Minimal fix:** Regenerate the manifest and add a CI check that the builder's output matches the committed JSON.
- **Confidence:** high — verified by executing the manifest builder against the tree.

### [IMPORTANT] Probe-parsing contract drift: distribution.py keeps its own version probe despite version_probe.py's "exactly one place" contract

- **Where:** `src/gobby/install/version_probe.py:1-7,64-68` (last-token parse) vs `src/gobby/install/distribution.py:144-160` (private regex over `stdout or stderr`)
- **Failure mode:** The two parsers disagree on common outputs: `ghook 0.4.5 (build abc)` → version_probe returns `abc)` (unparseable → floor_drift → feeds the perpetual-redownload loop above) while distribution extracts `0.4.5` (passes Homebrew floor check). Same binary: healthy per `gobby install`, perpetually floor-violated per the daemon freshness loop. Distribution's regex also accepts 2-component versions that `parse_version_tuple` then fails to parse.
- **Why it matters:** Silent cross-layer divergence in the most load-bearing string parse in this subsystem, explicitly contradicting the module's stated contract.
- **Minimal fix:** Route `_probe_helper_version` through `probe_native_bin_version`, or move the regex extraction into version_probe so both layers share it.
- **Confidence:** high (drift), medium (real-world trigger today)

### [IMPORTANT] GitHub release listing is unpaginated; a tool's latest release silently falls off page 1

- **Where:** `src/gobby/install/bin_freshness_github.py:20` (`?per_page=100`, single page), `:75-91,130-150`
- **Failure mode:** Five tools share one repo's releases feed. Once >100 releases exist, a tool whose latest release is older than the 100 most recent gets `SourceUnavailableError` (143-146). If its floor is satisfied the cycle records `up_to_date` (masking); with floor drift it records `floor_violated` forever and the recovery path (download to reach floor) is permanently unavailable.
- **Why it matters:** Silent time-bomb failure of the floor-recovery mechanism as release count grows.
- **Minimal fix:** Follow `Link: rel="next"` pagination (bounded) or query `releases/tags/{prefix}{pin}` directly when hunting a floor release.
- **Confidence:** medium

### [IMPORTANT] Floor-satisfied error paths record fabricated `latest_version` and discard the real error

- **Where:** `src/gobby/install/bin_freshness_updater.py:126-135,146-155` (`latest_version=inspection.installed_version`, `error=inspection.sidecar_error`)
- **Failure mode:** When GitHub is unreachable but the floor is met, the record claims `latest_version == installed_version` — a value never observed from any release feed — and the actual `SourceUnavailableError`/`GithubAPIError` text is replaced by the (usually None) sidecar error. A permanently broken GitHub path is indistinguishable in the DB from a genuinely current binary.
- **Why it matters:** Monitoring can never detect that freshness checking itself is dead; "up_to_date" is reported while the check's contract was never executed.
- **Minimal fix:** Record `latest_version=None` and keep the exception string in `last_error` while still using status `up_to_date`.
- **Confidence:** high (behavior), medium (severity)

### [IMPORTANT] Deployed `validate_settings.py` cannot run standalone: it imports the `gobby` package under a `#!/usr/bin/env python3` shebang

- **Where:** `src/gobby/install/shared/hooks/validate_settings.py:1,32-34`; deployment `src/gobby/cli/installers/shared.py:54-80`
- **Failure mode:** The script's documented usage (lines 16-19) is direct execution; the copy in `~/.gobby/hooks/` runs under whatever `python3` is on PATH. For uv-tool/pipx installs the gobby package is not importable there, so it dies with ModuleNotFoundError before validating anything. Fail-closed (nonzero exit) but unusable as shipped.
- **Why it matters:** The one diagnostic users are given for "are my hooks wired correctly" breaks in the most common install topology, with a traceback instead of guidance.
- **Minimal fix:** Inline the small contracts it needs (hook-name tuples, `is_gobby_hook_command` prefix check) so the script is dependency-free, or have the installer rewrite the shebang to gobby's interpreter.
- **Confidence:** medium

### [IMPORTANT] Promoted binary is never fsynced before the atomic rename; text sidecars are

- **Where:** `src/gobby/install/bin_freshness_updater.py:284,297` (`dest.write_bytes(...)`, no fsync), `:259` (`os.replace`) vs `:330-335` (`_write_atomic_text` does flush+fsync for the 30-byte stamp)
- **Failure mode:** On power loss/kernel crash shortly after promotion, journaled filesystems can persist the rename before the data blocks, leaving a zero-length or partial executable at the final path — the exact corrupted-binary hazard, guarded for trivial text files but not the binary itself.
- **Why it matters:** A torn helper binary bricks hooks/code-index until the next freshness cycle.
- **Minimal fix:** Write the staged binary via an fd, fsync before close, then `os.replace`.
- **Confidence:** medium

### [IMPORTANT] `create_exporters` is called twice and two-thirds of its side-effectful output is dropped

- **Where:** `src/gobby/telemetry/providers.py:42,76`; `src/gobby/telemetry/exporters.py:52-70,75,83-91`
- **Failure mode:** Each call constructs everything. The tracer-path call creates a `PrometheusMetricReader` whose `__init__` registers a collector in the global prometheus REGISTRY (verified in installed package) — the orphan stays registered forever and logs "Cannot call collect on a MetricReader until it is registered on a MeterProvider" on every `/api/admin/metrics` scrape (`src/gobby/servers/routes/admin/_health.py:570`). The meter-path call constructs OTLP span exporters (gRPC channel opened at construction) that are dropped unshutdown. Both calls open a `RotatingFileHandler` on gobby.log that is never attached and never closed; the `log_handlers` return value is ignored by every caller.
- **Why it matters:** Leaked FDs/gRPC channels per init, warning spam per scrape; on Windows an extra open handle to gobby.log can break log rotation.
- **Minimal fix:** Split `create_exporters` into per-signal factories and call only what each provider needs; drop the unused log-handler path (setup_otel_logging already owns file handlers).
- **Confidence:** high

### [IMPORTANT] Middleware reads `scope["route"]` before routing happens — metric label cardinality is unbounded

- **Where:** `src/gobby/telemetry/middleware.py:43-45` (route lookup), `:62` (`call_next`), `:47-59` (attributes incl. `session_id`/`project_id`)
- **Failure mode:** In `BaseHTTPMiddleware.dispatch`, routing happens inside `call_next`; at lines 43-45 the scope has no `"route"` key yet, so `http.target` is always the raw URL path. Every distinct URL (`/api/tasks/<uuid>`, 404 garbage) plus every distinct `session_id`/`project_id` creates a permanent cumulative aggregation point in the Prometheus reader — unbounded memory growth and a useless `http.target` dimension. The existing test (tests/telemetry/test_middleware.py:75) only uses static paths, so it can't catch this.
- **Why it matters:** Memory growth on a long-running daemon; PII-adjacent — session UUIDs and arbitrary request paths become Prometheus label values exposed on `/api/admin/metrics`.
- **Minimal fix:** Read `request.scope.get("route")` *after* `call_next` returns (the router mutates the shared scope); fall back to `"unmatched"` for 404s; drop or hash `session_id` from metric attributes.
- **Confidence:** high

### [IMPORTANT] Telemetry is initialized from phase-1 config — DB config-store overrides for `telemetry.*` silently never apply

- **Where:** `src/gobby/runner_init/storage.py:40,43,89-94`; `src/gobby/telemetry/providers.py:35-36,71-72,85-86` (providers cached, never rebuilt); `src/gobby/config/app.py:836-905`
- **Failure mode:** Any telemetry setting stored via the runtime config store (log level, `traces_enabled`, `otlp_endpoint`, `otlp_headers` incl. `$secret:` refs) is ignored: providers and log handlers are built from the file-only phase-1 config and cached for process lifetime; the phase-2 reload doesn't re-init telemetry. Secret-referenced `otlp_headers` can never resolve.
- **Why it matters:** Contract drift between the config hierarchy ("CLI > DB > bootstrap") and the telemetry subsystem; users changing telemetry config via `gobby config` get no effect and no error.
- **Minimal fix:** Re-run (or finish) telemetry init after the phase-2 config load — providers can be created lazily on the second config, with logging set up early from phase-1.
- **Confidence:** medium

### [IMPORTANT] `JsonOTelFormatter` uses `json.dumps` without a fallback — non-serializable `extra` drops the log record

- **Where:** `src/gobby/telemetry/logging.py:130-137`
- **Failure mode:** With `log_format: json`, any `logger.info(..., extra={...})` carrying a Path/datetime/exception/dataclass raises TypeError inside the formatter; logging machinery swallows it, prints "--- Logging error ---" to stderr, and the record never reaches the file. `extra=` is used pervasively (e.g., `src/gobby/agents/isolation.py:243,325,541`).
- **Why it matters:** The records most worth keeping (rich structured context) are the ones silently lost, and only in JSON mode.
- **Minimal fix:** `json.dumps(log_data, default=str)`.
- **Confidence:** medium

### [IMPORTANT] `get_telemetry_metrics()` singleton has no synchronization

- **Where:** `src/gobby/telemetry/instruments.py:356,359-368`
- **Failure mode:** Two threads (hook-handler thread racing the event loop's `update_daemon_metrics`) can both observe `_telemetry_metrics is None` and construct two `TelemetryMetrics`, double-registering ~40 instruments (duplicate-instrument warnings) and splitting the legacy `_values` counters across instances — `/admin/status` undercounts whichever lost the race. The class takes `self._lock` everywhere, but the constructor path is unguarded.
- **Minimal fix:** Guard creation with a module-level `threading.Lock`.
- **Confidence:** medium

### [IMPORTANT] Discovery savings are recorded on every summary generation — systematic multiple counting

- **Where:** `src/gobby/savings/discovery.py:64-83` (unconditional INSERT per call); `src/gobby/sessions/summarize.py:319-336` (runs even when refresh decision is `noop`); callers: `src/gobby/cli/sessions.py:526`, `src/gobby/hooks/session_summary_dispatcher.py:43`, `src/gobby/mcp_proxy/tools/sessions/_handoff.py:155`, `_terminal.py:378`, `src/gobby/sessions/lifecycle.py:484`; schema `src/gobby/storage/postgres_baseline_schema.sql:1318-1329` (no uniqueness on session/category)
- **Failure mode:** Summaries are regenerated repeatedly per session (handoff, session end, lifecycle refresh, CLI — the delta/noop refresh logic exists precisely because of repeat calls). Each call inserts a fresh `discovery` ledger row carrying the full all-skills+all-tools baseline, so a session summarized N times contributes N× the savings to `get_summary` totals.
- **Why it matters:** The headline dashboard number (`total_tokens_saved`) is inflated by a multiple that grows with summary frequency — accounting integrity failure.
- **Minimal fix:** Upsert one discovery row per session (unique `(session_id, category)`), and skip recording when `decision.mode == "noop"`.
- **Confidence:** high

### [IMPORTANT] Discovery savings baseline is fabricated — assumes every skill's full content and every tool schema would otherwise be in context

- **Where:** `src/gobby/savings/discovery.py:24-27,40-50,65-66`
- **Failure mode:** The counterfactual is "without Gobby, the entire skill library and every MCP tool schema would be loaded into every session" — no real client does that (Claude Code loads skill descriptions, not full bodies; MCP clients load schemas of attached servers only). `actual` only counts `session_skills` + `unlocked_tools`, so a session that touched nothing "saves" the whole library. Zero tests exist for this module.
- **Why it matters:** The discovery category manufactures savings proportional to how much content exists, not how much was avoided; combined with the multiple-counting above, the metric is unanchored.
- **Minimal fix:** Baseline against what a client would actually preload (e.g., skill descriptions + schemas of session-visible servers), or label the metric as an upper-bound estimate in metadata; add tests.
- **Confidence:** medium — the math is verified; "fabricated" is a judgment about the baseline's validity.

### [IMPORTANT] LM Studio "not running" output is reported as running

- **Where:** `src/gobby/utils/deps.py:356,367`; consumed at `src/gobby/utils/status.py:338-344`
- **Failure mode:** `running = "running" in output.lower()` is a substring check. Status text like "The server is not running" contains "running", so a stopped LM Studio server is reported as `{"running": True}` whenever `lms server status` exits 0 with that phrasing. Tests only cover positive phrasings (tests/utils/test_deps.py:300-314).
- **Why it matters:** False health read in the exact tool users run to diagnose embedding failures; status.py picks the embeddings provider display based on this flag (status.py:349-357).
- **Minimal fix:** Match a negative phrase first or anchor on exit semantics; add a "not running" test.
- **Confidence:** medium — structural fragility certain; exact lms wording unverified.

### [IMPORTANT] Brittle string-match exception classifier can crash `gobby status` outright

- **Where:** `src/gobby/utils/deps.py:425-445` (classifier), `:478-480` (re-raise); caller `src/gobby/cli/daemon.py:744-747` (no handler)
- **Failure mode:** `get_configured_embedding_provider` re-raises any exception whose type name/message lacks the markers ("database", "relation", "schema", ...). `BootstrapConfigError(HUB_BACKEND_POSTGRES_REQUIRED)` (`src/gobby/config/bootstrap.py:35-39,163`) contains none; psycopg-pool timeouts ("couldn't get a connection...") likewise. The exception propagates through `collect_all_deps()` (deps.py:581) into the `status` CLI command — a traceback instead of a status report. The classifier also over-matches: any unrelated exception containing "database" is silently swallowed.
- **Why it matters:** The diagnostic command crashes precisely for misconfigured/degraded installs — the users who most need it.
- **Minimal fix:** Catch concrete exception types (`psycopg.Error`, `BootstrapConfigError`, `RuntimeError`, `OSError`), or wrap the embeddings probe in `collect_all_deps` so status never crashes on it.
- **Confidence:** medium

### [IMPORTANT] `except Exception: pass` in get_lmstudio_info

- **Where:** `src/gobby/utils/deps.py:368-369`
- **Failure mode:** The stderr-fallback subprocess call swallows every exception (including `TimeoutExpired` and programming errors) and silently reports `running=False`. A wedged `lms` hanging to timeout on every status call is indistinguishable from "stopped".
- **Why it matters:** Violates the repo error-handling contract; hides real failures in a diagnostics path.
- **Minimal fix:** Catch `(subprocess.TimeoutExpired, OSError)` and log at debug, mirroring `_run_cmd` (deps.py:34-35).
- **Confidence:** high

### [IMPORTANT] Embedding provider inference misses OpenAI/cloud when api_base is explicitly set

- **Where:** `src/gobby/utils/deps.py:417-421,473-477`; display impact `src/gobby/utils/status.py:327-357`
- **Failure mode:** The "openai" branch requires `normalized_api_base in (None, "")`. A user who explicitly sets `api_base=https://api.openai.com/v1` (or an Azure endpoint) with an API key gets None from inference; port-based inference (deps.py:390-400) also returns None; status falls into the heuristic chain and can print "Embeddings: Ollama (stopped)" while OpenAI embeddings are configured and working.
- **Minimal fix:** Treat known cloud hosts — or any non-local api_base plus api_key — as the cloud provider before falling through.
- **Confidence:** medium

### [IMPORTANT] Sync DB + filesystem I/O from project_context runs on the event loop per HTTP request

- **Where:** `src/gobby/utils/project_context.py:164-166,199,207`; caller `src/gobby/servers/middleware/project_context.py:51-57,70-74`
- **Failure mode:** `ProjectContextMiddleware.dispatch` is async and calls the context seeding synchronously for every request carrying `x-gobby-session-id` — a blocking Postgres query plus file reads on the event loop. The agents path correctly offloads (`src/gobby/agents/isolation.py:615-619` uses `asyncio.to_thread`); the middleware does not.
- **Why it matters:** Under pool exhaustion (psycopg pool timeout up to 5s, `src/gobby/storage/hub/postgres.py:92`) every daemon HTTP request stalls behind the middleware.
- **Minimal fix:** Wrap in `asyncio.to_thread` (or an async variant) in the middleware.
- **Confidence:** high

### [IMPORTANT] Sync blocking helpers from session_context and git are invoked directly on the daemon event loop

- **Where:** `src/gobby/utils/session_context.py:233-238,264-265,279,305,317,331,343,355` called sync inside `async def call_mcp_tool` (`src/gobby/servers/routes/mcp/endpoints/execution.py:600,649` via `_set_context_for_request` at 140-208) and `src/gobby/mcp_proxy/server.py:247`; `src/gobby/utils/git.py:44-92,173-222` (up to 4 subprocesses, 5s timeout each) called from `async def register_session` (`src/gobby/servers/routes/sessions/core.py:269,299`)
- **Failure mode:** Under pool exhaustion or a hung git (network FS), the whole daemon event loop stalls for seconds — every concurrent WebSocket/HTTP client freezes. Session registration and MCP tool dispatch are hot paths.
- **Minimal fix:** Offload at the async call sites via `asyncio.to_thread`; for `resolve_and_seed_contexts`, offload the DB lookups internally so ContextVar seeding stays in-task.
- **Confidence:** medium — the stall mechanics are real; the pattern is tolerated elsewhere in the repo.

### [IMPORTANT] check_health classifies errors by substring matching on exception text

- **Where:** `src/gobby/utils/daemon_client.py:113-129`, consumed at `:161-167`; tests pin only `Exception("Connection refused")` (tests/utils/test_utils_daemon_client.py:86-91,127-138)
- **Failure mode:** `except Exception` then `"refused" in msg or "connection" in msg` decides "daemon not running" (returning `(False, None)` with `None` as an overloaded not-running sentinel). `httpx.ReadError("Connection reset by peer")` is misreported as "not running" when the daemon is up but unhealthy; `httpx.ConnectTimeout` ("timed out") — an actual cannot-connect — is reported as "cannot_access". The exception type (`httpx.ConnectError`) is available and ignored.
- **Why it matters:** Wrong operator-facing diagnosis in status paths; the `error_reason=None` sentinel makes the tuple contract fragile.
- **Minimal fix:** Catch `httpx.ConnectError` for not-running and `httpx.HTTPError` for the rest; stop signaling "not running" via `None`.
- **Confidence:** high (mechanism), medium (frequency)

### [IMPORTANT] Hardcoded default port 60887 in DaemonClient lets callers ignore the configured daemon_port

- **Where:** `src/gobby/utils/daemon_client.py:64`; drifting callers: `src/gobby/cli/agents.py:549,597,668`, `src/gobby/cli/workflows/check.py:28`; the port is user-configurable (`src/gobby/config/app.py:224-225`, `src/gobby/config/bootstrap.py:28,57`)
- **Failure mode:** Commands constructing `DaemonClient()` bare (`gobby agents stop/kill`, `gobby workflows check`) hit the wrong port for any user who changed `daemon_port`, failing with "daemon not running" while sibling commands (cli/mcp_proxy.py:28, cli/skills.py:47, cli/memory/common.py:22) correctly pass `config.daemon_port`.
- **Minimal fix:** Default `port` to the bootstrap-config value (or require it); fix the four bare call sites.
- **Confidence:** high

### [IMPORTANT] Concurrent init race: check-then-create with no unique-violation recovery

- **Where:** `src/gobby/utils/project_init.py:200-237`; `src/gobby/storage/projects.py:137-143`
- **Failure mode:** Two processes initializing the same (or same-named) directory both pass `get_by_name` → None and both call `create`; the loser gets a raw UniqueViolation instead of adopting the winner's project. No transaction spans the check+insert.
- **Minimal fix:** Catch the unique violation and re-fetch by name (or upsert in `create`).
- **Confidence:** high

### [IMPORTANT] Unrelated repos with the same directory basename silently merge into one project

- **Where:** `src/gobby/utils/project_init.py:185-186` (name defaults to `cwd.name`), `:200-226` (adopts existing DB row by name; `repo_path` backfilled only when missing — never compared)
- **Failure mode:** Initializing `/work/api` when a project named `api` exists for `/other/api` writes the existing project's id into the new directory's project.json and returns `already_existed=True`.
- **Why it matters:** Tasks, sessions, and memory from two unrelated repos cross-contaminate under one project id, with no warning.
- **Minimal fix:** When `existing.repo_path` is set and differs from `cwd`, refuse or disambiguate (suffix the name / require explicit `--name`).
- **Confidence:** high (mechanics), medium (intent)

### [IMPORTANT] `initialize_project` has zero behavioral tests — every test mocks it

- **Where:** `src/gobby/utils/project_init.py:129-248`; `tests/cli/test_cli_init.py:97`, `tests/cli/test_cli.py:247,273` (patched out); `tests/utils/test_utils_project_init.py` covers only dataclasses/helpers
- **Failure mode:** The adopt-by-name, create-new, and re-init branches — where both project_init Blockers live — are exercised by no test (verified via `git grep -rln "initialize_project" tests/`).
- **Minimal fix:** Add tests for: re-init from subdir, name-exists adoption, soft-deleted-name conflict, fresh create.
- **Confidence:** high

### [IMPORTANT] project.json writes are non-atomic read-modify-write with concurrent writers

- **Where:** `src/gobby/utils/project_init.py:280-281,302-304,347-349` (plain `open("w")`); concurrent writers: `src/gobby/sync/linear.py:411`, `src/gobby/cli/linear.py:116`; atomic counterexample in-tree: `src/gobby/project_verification/refresh.py:193-203` (mkstemp + os.replace)
- **Failure mode:** A crash mid-`json.dump` truncates project.json (downstream readers warn and return None — project identity lost until manual repair); two writers (daemon Linear sync vs CLI) interleaving read→modify→write drop each other's fields.
- **Minimal fix:** Reuse the tmp-file + `os.replace` pattern from refresh.py for all three writers.
- **Confidence:** medium

### [IMPORTANT] ensure_project_json_for_isolation: blanket `except Exception` + non-atomic overwrite

- **Where:** `src/gobby/utils/project_context.py:296-312`
- **Failure mode:** `data["id"]` (300) raises KeyError for a hand-edited project.json without "id"; the blanket except reduces every failure to a warning and returns None either way, so callers (`src/gobby/agents/isolation.py:615`, `src/gobby/mcp_proxy/tools/worktrees/_helpers.py:132`) cannot detect the worktree was left without `parent_project_path`. The docstring guarantees "Always overwrites any existing project.json" — silently violated. `json.dump` writes in place (307-308); a crash mid-write leaves a truncated project.json in the worktree, after which workflow/verification discovery silently breaks.
- **Minimal fix:** Temp-file + `os.replace`; narrow the catch to `(OSError, json.JSONDecodeError, KeyError)`; return a bool or raise.
- **Confidence:** medium

### [IMPORTANT] extract_json_from_text gives up after at most two brace positions and never extracts arrays

- **Where:** `src/gobby/utils/json_helpers.py:55-73,84`; production callers on LLM fallback paths: `src/gobby/llm/claude.py:619`, `src/gobby/mcp_proxy/importer.py:705`, `src/gobby/memory/digest.py:510`
- **Failure mode:** The candidate list is at most {brace after first fence, first `{` in the whole text}. A non-JSON brace before the real payload with no code fence (`Based on {context}: {"a":1}`) → raw_decode fails at the first `{` (71-73) and the function returns None instead of scanning forward. Only `"{"` is ever searched (58, 66, 71), so a top-level JSON array — fenced or not — is never extracted.
- **Why it matters:** Valid LLM output is silently discarded; callers degrade (digest skipped, import parse failure) even though the model answered correctly. The docstring advertises robustness over "brittle regex patterns" while the scan is strictly weaker than a brace-scan loop.
- **Minimal fix:** Loop `text.find("{", pos+1)` (and `"["`) after each failed raw_decode.
- **Confidence:** high (behavior), medium (trigger frequency)

### [IMPORTANT] machine_id persisted write is neither atomic nor race-safe despite claiming "atomically"

- **Where:** `src/gobby/utils/machine_id.py:74-86,89-106` (docstring 90, open at 102, write at 104)
- **Failure mode:** `_write_file_secure` opens the real target with `O_TRUNC` and writes in place — no temp-file + `os.replace`, no fsync, unchecked `os.write` return. A crash mid-write leaves a truncated `~/.gobby/machine_id`; the next read sees empty content and generates a *new* ID — machine identity silently changes. Two processes racing first-run (daemon + CLI hook adapter both call `get_machine_id`) generate two different IDs, each caches its own for process lifetime, last writer wins on disk. `0o600` applies only at creation; a pre-existing looser-mode file is never tightened.
- **Why it matters:** machine_id feeds the sessions unique index `(external_id, machine_id, source, project_id)` and session/hook records (`src/gobby/adapters/base.py:225`, `src/gobby/agents/launcher_session.py:27`); identity drift breaks session dedup/attribution.
- **Minimal fix:** Write to a tmp path via the same `os.open(..., 0o600)`, fsync, `os.replace`; `os.chmod` existing files.
- **Confidence:** medium (window narrow but real); high that the "atomically" docstring is wrong

### [IMPORTANT] Raw hardware machine GUID stored and propagated instead of an app-scoped hash

- **Where:** `src/gobby/utils/machine_id.py:118-120` (`return str(machineid.id())`)
- **Failure mode:** `machineid.id()` returns the raw OS machine identifier (IOPlatformUUID on macOS, `/etc/machine-id` on Linux, MachineGuid on Windows). py-machineid's own guidance is `machineid.hashed_id(app_id)` so apps can't be cross-correlated and the raw hardware ID never leaves the OS. Gobby persists the raw value to `~/.gobby/machine_id` and attaches it to hook events and session rows in the shared Postgres hub.
- **Why it matters:** PII/trackability: any export, multi-user hub, or future telemetry/relay carrying session rows leaks a globally stable hardware identifier; migrating later is painful because the value participates in a unique index.
- **Minimal fix:** Use `machineid.hashed_id("gobby")` when generating a new ID; existing persisted IDs stay valid via the file.
- **Confidence:** high (raw ID used); medium (exposure — no off-box telemetry path confirmed; `src/gobby/sync/` does not carry machine_id in git-synced JSONL)

### [IMPORTANT] 32-bit (and 24-bit) random IDs used as primary keys with no collision handling

- **Where:** `src/gobby/utils/id.py:7,33-36` (default `length=8` → 32 bits); callers: `src/gobby/storage/communications.py:39,133,238-243` (plain INSERT, no ON CONFLICT/retry), `src/gobby/storage/clones.py:149` (`length=6` → 24 bits), `src/gobby/dispatch/dispatcher.py:677`
- **Failure mode:** Birthday math: ~1% collision probability by ~9.3k rows, 50% by ~77k. Inserts using these as `id TEXT PRIMARY KEY` (e.g. `create_message`) eventually raise a unique violation with no retry path, failing the user operation with an opaque DB exception.
- **Minimal fix:** Bump the default length (uuid4().hex is available — use 16+ chars), or document caller retry; fix the `length=6` site.
- **Confidence:** high (math/insert path), medium (time-to-incident)

### [IMPORTANT] is_dev_mode checks only the exact path, contradicting its "inside the repo" contract

- **Where:** `src/gobby/utils/dev.py:55-69` (docstring "inside" at 56/66; `return is_gobby_project(path)` at 69); caller `src/gobby/cli/sync.py:57` (`is_dev_mode(Path.cwd())`)
- **Failure mode:** `is_gobby_project` requires `path` to be the repo root (dev.py:31-52). Running `gobby sync` from a subdirectory of the gobby checkout returns False, so dev-mode gating of `scope='bundled'` writes silently flips off. `service_common.py:56-93` already works around this with its own parent-walk, confirming the drift.
- **Minimal fix:** Walk parents in `is_dev_mode`, or fix the docstring to "is the repo root" and audit cwd callers.
- **Confidence:** medium

### [IMPORTANT] parse_stored_datetime has no direct unit test despite being load-bearing for agent timeout math

- **Where:** `src/gobby/utils/datetime.py:8-19`; consumers `src/gobby/agents/agent_health.py:146-150,252`
- **Failure mode:** This function is the only thing preventing naive/aware subtraction errors in the agent health monitor, and it silently asserts "naive == UTC". Zero test references repo-wide (`git grep -rln "parse_stored_datetime" -- tests src`). A regression would break agent timeout kills with no test catching it. Undocumented `ValueError` on malformed strings; one caller guards (agent_health.py:251-252), the adjacent one relies on a loop-level catch (146).
- **Minimal fix:** Add tests covering naive/aware/passthrough/None/malformed; document the ValueError.
- **Confidence:** high (test absence proven), medium (regression likelihood)

---

### [NIT] mathutil2.py is dead code

- **Where:** `src/gobby/utils/mathutil2.py:4-6`
- **Note:** Zero callers (`git grep "mathutil2\|multiply("` → only the definition and tests/utils/test_mathutil2.py). Delete both, or move under tests/ fixtures if the code-index pipeline needs scaffolding.

### [NIT] Half of tool_summarizer.py is production-dead

- **Where:** `src/gobby/utils/tool_summarizer.py:48-87,90-121`
- **Note:** `summarize_tools` and `_summarize_description_with_llm` have no production caller; only `generate_server_description` is live (`src/gobby/mcp_proxy/actions.py:11,78`). The per-tool serial-await design would also be a perf cliff if wired up.

### [NIT] init_summarizer_config's project_dir parameter is dead

- **Where:** `src/gobby/utils/tool_summarizer.py:25-35`; sole call site `src/gobby/servers/http.py:225-229` doesn't pass it.

### [NIT] Broad `except Exception` in tool_summarizer masks config/template bugs; "max 180 chars" contract unenforced

- **Where:** `src/gobby/utils/tool_summarizer.py:55-56,84,87,175`
- **Note:** Missing prompt template, TypeError, or KeyError logged as "Failed to summarize" and falls back; the LLM result is never length-checked against the documented cap.

### [NIT] decode_llm_response and the JSONValue alias have zero production callers

- **Where:** `src/gobby/utils/json_helpers.py:19,140-189`
- **Note:** The msgspec-typed decode path is unexercised by any real LLM flow; only tests reference it.

### [NIT] extract_json_object re-parses already-validated JSON; its JSONDecodeError handler is unreachable

- **Where:** `src/gobby/utils/json_helpers.py:109-120`
- **Note:** `extract_json_from_text` returns spans only after `raw_decode` succeeded on exactly that span; the object is discarded and re-parsed (twice more counting callers).

### [NIT] validation.py checks are structurally impossible under the Postgres hub schema

- **Where:** `src/gobby/utils/validation.py:18-30,32-43,56-71`; schema `src/gobby/storage/postgres_baseline_schema.sql:340,488-489`
- **Note:** NOT NULL FKs + ON DELETE CASCADE mean orphan dependencies and invalid project links cannot exist; `check_orphan_dependencies`/`check_invalid_projects`/`clean_orphans` always return empty — SQLite-era leftovers still wired to the CLI (`src/gobby/cli/tasks/main.py:242-289`). Also duplicates `sql_placeholders` inline (validation.py:66 vs `src/gobby/utils/sql.py:6-11`).

### [NIT] session_refs strips all leading '#' while the resolver strips exactly one

- **Where:** `src/gobby/utils/session_refs.py:29-30` vs `src/gobby/storage/session_resolution.py:54`
- **Note:** `"##5"` passes the helper's gate but fails resolution; `isdigit()` accepts Unicode digits the resolver's `int()` rejects. Mirror the resolver (`value[1:]`, `isdecimal()`).

### [NIT] get_machine_id docstring contradicts behavior; trailing return None is near-dead

- **Where:** `src/gobby/utils/machine_id.py:34,48,52-56`
- **Note:** Docstring promises None on failure but OSError is re-raised (and re-wrapped, discarding the errno-typed subclass); the falsy branch is unreachable.

### [NIT] generate_prefixed_id docstring malformed; deterministic mode lacks separator guidance

- **Where:** `src/gobby/utils/id.py:19-22`; `src/gobby/storage/merge_resolutions.py:177` (unseparated concat)
- **Note:** `Raises:` nested inside `Returns:`; deterministic content should be delimiter-joined.

### [NIT] version.py claims a single source of truth but there are two

- **Where:** `src/gobby/utils/version.py:10-12,20-25`; `pyproject.toml:3`; `src/gobby/__init__.py:9`
- **Note:** A release bump touching only pyproject.toml leaves dev checkouts reporting the stale `__version__`.

### [NIT] DaemonClient status-cache methods are dead, with a latent lock bug

- **Where:** `src/gobby/utils/daemon_client.py:82-86,246-258,260-274`
- **Note:** Zero production callers (HealthMonitor reimplements caching, `src/gobby/hooks/health_monitor.py:78-110`). If resurrected: `update_status_cache` holds `_cache_lock` across the full HTTP round-trip, so `get_cached_status` — documented "without making HTTP calls" — blocks up to the request timeout.

### [NIT] Dead code in project_init: `_find_frontend_dirs`/`_FRONTEND_SUBDIRS` and `_update_project_json_verification`

- **Where:** `src/gobby/utils/project_init.py:85,88-105,251-283`
- **Note:** Only test callers; the live frontend-dir implementation is the duplicate at `src/gobby/project_verification/evidence.py:464`. Two implementations invite drift.

### [NIT] `is_valid_sha_format` is dead, and `normalize_commit_sha` doesn't enforce its own SHA contract

- **Where:** `src/gobby/utils/git.py:225-254,257-271`; callers `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py:317`, `src/gobby/storage/tasks/_lifecycle.py:149`
- **Note:** `normalize_commit_sha` only checks `len >= 4`, so any revspec ("HEAD", "main", a tag) gets "normalized" into a real short SHA despite the documented SHA-only contract; dash-prefixed inputs become option-shaped argv (list-form, no shell injection — git just errors).

### [NIT] `get_git_branch` fallback never extracts a branch and mislabels failures as detached HEAD

- **Where:** `src/gobby/utils/git.py:158-170`
- **Note:** The `symbolic-ref` result is checked only for None-ness, never parsed — both branches return None; on git <2.22 a successful `refs/heads/X` is discarded; "detached HEAD state" debug log fires for unrelated errors (timeout, not-a-repo).

### [NIT] `call_http_api` edge semantics: `timeout=0` silently ignored; body dropped on GET/DELETE

- **Where:** `src/gobby/utils/daemon_client.py:197,200-207`
- **Note:** `timeout or self.timeout` swallows explicit 0; `json_data` with GET/DELETE is silently discarded.

### [NIT] Module docstring drift in project_init; hub DB never closed

- **Where:** `src/gobby/utils/project_init.py:4-5,193`
- **Note:** Claims hook-system usage; only CLI callers exist (`src/gobby/cli/init.py:52`, `src/gobby/cli/install.py:219`). The DB opened at line 193 is never closed — harmless only because every caller is a short-lived CLI process; the ConnectionPool (min_size=2) would leak in a long-lived caller.

### [NIT] gwiki version collected but never displayed

- **Where:** `src/gobby/utils/deps.py:561-562`; `src/gobby/utils/status.py:247-272`
- **Note:** Wasted subprocess on a hot diagnostic path; gwiki install problems are invisible. Add a row or stop collecting.

### [NIT] Hooks-installed detection only checks user-scope config; project-scope installs report False

- **Where:** `src/gobby/utils/deps.py:181-205`; installer writes project scope at `src/gobby/cli/installers/claude.py:122,147-150`
- **Note:** A project-mode install puts hooks in `<project>/.claude/settings.json`; status reports the CLI without "hooks installed" despite success.

### [NIT] fetch_rich_status silently maps non-200 responses to "did not respond"

- **Where:** `src/gobby/utils/status.py:37-44`; message at `src/gobby/cli/daemon.py:738-741`
- **Note:** A 5xx falls through to `{}` with zero logging; the CLI claims the endpoint "did not respond" — false.

### [NIT] Postgres "unhealthy" on merely-missing key, inconsistent with Health Issues gating

- **Where:** `src/gobby/utils/status.py:107` vs `:476-477`
- **Note:** Services section treats missing `healthy` as unhealthy; Health Issues requires `is False`. The two sections disagree on the same payload.

### [NIT] `**kwargs: Any` on format_status_message swallows typo'd/stale arguments

- **Where:** `src/gobby/utils/status.py:162`
- **Note:** Hides caller/renderer drift — exactly the bug class this file is prone to (see gwiki). Delete it.

### [NIT] get_project_mcp_dir / get_project_mcp_config_path are dead code with unsanitized path joins

- **Where:** `src/gobby/utils/project_context.py:315-326,329-339`
- **Note:** Zero production callers; `project_name.replace(" ", "_")` doesn't strip `/` or `..` — latent traversal foot-gun if ever wired up.

### [NIT] get_project_context returns the live ContextVar dict by reference

- **Where:** `src/gobby/utils/project_context.py:96-98`
- **Note:** Callers mutating the returned dict mutate the shared per-task context (the file's own pattern is `data["project_path"] = ...` at 108/131). Return a shallow copy.

### [NIT] Env-override contract drift: GOBBY_PROJECT_ID trumps explicit-cwd resolution; every spawned agent carries it

- **Where:** `src/gobby/utils/project_context.py:92-94` (comment) vs `:101-114` (env branch wins); `src/gobby/agents/constants.py:130`
- **Note:** Inside an agent shell, gobby CLI calls in a *different* project's directory resolve to the spawn-time project with a minimal dict lacking `project_path`/`name`/`verification` — `get_verification_config`/`get_hooks_config`/`get_workflow_project_path` all return None despite a valid project.json on disk. Test-asserted (tests/utils/test_project_context.py:526-537), but the in-function comment contradicts it.

### [NIT] collect_all_deps runs ~15 serial subprocesses with 3-5s timeouts each

- **Where:** `src/gobby/utils/deps.py:540-585`; `_run_cmd` timeout at 23
- **Note:** With a few wedged tools (Docker Desktop paused, flaky tailscale), `gobby status` takes tens of seconds. Thread-pool the probes or cache with a short TTL.

### [NIT] Docstring drift: "stamp file or CLI" vs implementation order

- **Where:** `src/gobby/utils/deps.py:79-99` vs `:66-75`
- **Note:** Five per-tool docstrings state the inverse precedence of `_get_native_binary_version` (CLI wins, stamps are fallback).

### [NIT] fetch_releases re-fetches the identical URL once per spec per cycle

- **Where:** `src/gobby/install/bin_freshness_github.py:130-132`; `bin_freshness_updater.py:54,62`
- **Note:** 5 identical unauthenticated API calls per hourly cycle; no `GITHUB_TOKEN` support — burns the 60/hr per-IP quota in shared-IP environments. Memoize per cycle.

### [NIT] HTTPS enforcement checks only the initial URL; urllib follows cross-scheme redirects

- **Where:** `src/gobby/install/bin_freshness_github.py:62-66,85,122`
- **Note:** The `nosec` justification "scheme validated above" covers hop zero only; compounds with the missing checksum verification.

### [NIT] `parse_version_tuple` silently equates pre-releases with finals

- **Where:** `src/gobby/install/bin_freshness_models.py:73` (`(?:[-+].*)?` discarded)
- **Note:** `0.4.5-rc1` satisfies floor `0.4.5`. Low exposure (GitHub `prerelease` flag filtered at github.py:133).

### [NIT] validate_settings crashes with a raw traceback on structurally malformed settings; validates only the first hook entry

- **Where:** `src/gobby/install/shared/hooks/validate_settings.py:228-236,238-243`
- **Note:** Non-dict hook entries → uncaught AttributeError (fail-closed but ugly); only `hook_configs[0]`/`hooks[0]` inspected; non-gobby commands warn yet "All validations passed!" prints.

### [NIT] Droid can never be auto-detected from script path; Codex comment contradicts its config

- **Where:** `src/gobby/install/shared/hooks/validate_settings.py:131-137,153-156,233-236`; `src/gobby/cli/installers/droid.py:271`
- **Note:** Droid lives under `.factory/hooks` but detection probes `.droid/`; the flat branch is dead under every current config (all six set `nested=True`).

### [NIT] transform.py crashes for its documented "run from anywhere" usage, after partially overwriting reference files

- **Where:** `src/gobby/install/shared/skills/impeccable/.upgrade/transform.py:7-9,115,237-239`
- **Note:** `dst.relative_to(Path.cwd())` raises whenever cwd isn't an ancestor; first file already overwritten before the crash (git-recoverable). Also `write_text` without `encoding=` and a read-side timeout escaping the catch.

### [NIT] Lock release can mask the original unlock error

- **Where:** `src/gobby/install/bin_freshness_locks.py:63-74`
- **Note:** A non-EBADF `os.close` error raised from `finally` replaces an in-flight unlock exception. Diagnostic-quality only; fd closes on all paths.

### [NIT] Init failure logs lack `exc_info` throughout runner_init

- **Where:** `src/gobby/runner_init/orchestration.py:89,132,143,161,207,266,283,301,312,329`; `services.py:49,134,207,232`; `storage.py:102,201`
- **Note:** Every degraded-startup diagnosis starts from a one-line `{e}` with no traceback. `_init_mcp_stack` (services.py:169) does it correctly.

### [NIT] helpers.py hides the load-bearing DB bootstrap behind weak contracts

- **Where:** `src/gobby/runner_init/helpers.py:23-27,62,68`
- **Note:** `init_hub_database(...) -> Any` plus `getattr(config, "database_url", None)` despite the Protocol declaring the field — mypy verifies nothing about the most critical seam. (The retry/advisory-lock logic itself is sound: bounded retries at helpers.py:82-95, advisory-locked migrations at postgres.py:236.)

### [NIT] `_ensure_headless_settings` never reconciles an existing file with the current hook schema

- **Where:** `src/gobby/runner_init/helpers.py:32-46,50-51`
- **Note:** Early-return-if-exists means older installs never receive newly added hook event keys. Merge missing keys or version the file.

### [NIT] Blocking construction on the event loop, including `time.sleep` retries

- **Where:** `src/gobby/runner.py:192-194`; `src/gobby/runner_init/helpers.py:95`
- **Note:** The whole init (sync DB/file I/O, up to 3.75s of `time.sleep`) runs inside async `run_gobby`. Benign today; signal handlers aren't installed yet either.

### [NIT] Private-attribute pokes across module boundaries

- **Where:** `src/gobby/runner_init/storage.py:196-198` (`hub_manager._skill_description_config`); `src/gobby/runner_init/servers.py:133` (`http_server._internal_manager`)
- **Note:** Init wiring depends on private internals of HubManager/HTTPServer; refactors break it without type errors.

### [NIT] Three separate `LocalAgentRunManager` instances; redundant re-wiring

- **Where:** `src/gobby/runner_init/orchestration.py:97,149,201`; `src/gobby/runner_init/servers.py:143`
- **Note:** Same stateless wrapper constructed thrice; `message_processor.session_manager` reassigned redundantly (already passed at services.py:216).

### [NIT] Two public `init_telemetry` functions with divergent behavior

- **Where:** `src/gobby/telemetry/__init__.py:66-100` vs `src/gobby/telemetry/logging.py:237-260`
- **Note:** The exported public API (`__all__` line 40) sets up instrumentors but no file logging; the daemon's sets up file logging but no instrumentors. Anyone importing the obvious one gets a daemon with no log files. Collapse to one (see Blocker).

### [NIT] Dead code: `@traced`, `get_trace_id`, `set_trace_context`, `extract_from_env`

- **Where:** `src/gobby/telemetry/tracing.py:36-108`; `src/gobby/telemetry/context.py:41-106`
- **Note:** `inject_into_env` IS live (`src/gobby/agents/constants.py:148`, `src/gobby/agents/spawners/base.py:44`) but nothing ever extracts — cross-process propagation is half-wired. The unused `capture_args=True` path stringifies raw arguments into span attributes — a PII foot-gun for its first caller (zero hits today).

### [NIT] OTel log bridge feeds a LoggerProvider with zero processors — per-record work, everything dropped

- **Where:** `src/gobby/telemetry/providers.py:88-90`; `src/gobby/telemetry/logging.py:232-234`
- **Note:** Every gobby log record is translated to an OTel LogRecord and handed to a provider that exports nothing; logs are never exported even with `otlp_endpoint` configured (only spans are).

### [NIT] Hook/MCP logger errors never reach gobby-error.log or the OTel bridge

- **Where:** `src/gobby/telemetry/logging.py:213-214,223,232-234`
- **Note:** `propagate = False` on `gobby.hooks`/`gobby.mcp.*`; the error handler and OTel handler attach only to `gobby`. ERROR records from hooks (hook_manager.py:88) land only in their dedicated files.

### [NIT] Log files and directories created with default umask; no redaction layer

- **Where:** `src/gobby/telemetry/logging.py:186-197`; `src/gobby/telemetry/exporters.py:83-91`
- **Note:** `~/.gobby/logs/` typically 0644/0755 on Linux — readable by other local users; no redaction filter in the formatter chain (DSN redaction exists only in CLI surfaces, e.g. `src/gobby/cli/postgres.py:204`). No concrete secret-logging site found in this review.

### [NIT] `shutdown_providers` ignores `_PROVIDER_LOCK`; `add_span_storage_exporter` silently no-ops

- **Where:** `src/gobby/telemetry/providers.py:53-65,95-110`
- **Note:** Concurrent get-during-shutdown can hand out a dead provider; a pre-provider call to `add_span_storage_exporter` would silently disable span persistence (current runner ordering — storage.py:43 before 136 — happens to be safe).

### [NIT] Legacy histogram `buckets` never populated; middleware exception path mislabels cancellations

- **Where:** `src/gobby/telemetry/instruments.py:252` vs `:307-318`; `src/gobby/telemetry/middleware.py:79-90`
- **Note:** Dead `buckets` key; every exception counted as `http.status_code="500"` including client disconnects/cancellations; BaseHTTPMiddleware adds per-request task-group overhead.

### [NIT] Savings ledger inconsistency and missing hardening

- **Where:** `src/gobby/savings/tracker.py:75,86-88`; `src/gobby/savings/discovery.py:54-60`
- **Note:** `tokens_saved = max(0, ...)` clamped but raw pairs stored unclamped — `SUM(original)-SUM(actual)` can disagree with `SUM(tokens_saved)`; `unlocked_tools` trusted to be a list while sibling code defends (`src/gobby/mcp_proxy/services/schema_guidance.py:63-64`); no retention on retired_token_ledger.

## Systemic patterns

1. **Silent feature amputation as a design style.** runner_init's catch-and-None across nine services, second-order skips that log nothing, the all-or-nothing cron/dispatch block, and the code-indexer's inverted error handling all produce the same outcome: a daemon that reports healthy with an arbitrary subset of Gobby off. There is no aggregated degraded-state surface anywhere.
2. **Validated-but-unwired features.** LLM tracing (wrong init function), the trace WebSocket broadcast (wrong thread), and the OTel log bridge (no processors) all "completed" with passing tests that assert against mocks or static fixtures instead of the real integration seam. Test design is the root cause: mock-heavy tests certified three dead features.
3. **Two parallel implementations drifting apart.** install_setup vs bin_freshness (integrity, atomicity, locking), version_probe vs distribution's private regex, two `init_telemetry` functions, two frontend-dir detectors, duplicated LLM-service construction (runner vs HTTPServer), savings tracker vs record.py vs HTTP route. Most Important findings in install/ are seams between the pairs.
4. **String heuristics where structured signals exist.** Substring "running" for LM Studio, marker-matching on exception text (deps.py and daemon_client.py independently), port-substring provider inference — exception-type information available and ignored. The diagnostics layer is confidently wrong exactly when systems are unhealthy.
5. **Atomic-write discipline is known but unevenly applied.** refresh.py and `_write_atomic_text` do it right; project.json (three writers), machine_id, the promoted binary itself, and worktree project.json all write in place. The most identity-critical files have the weakest write paths.
6. **Walk-up vs treat-as-root path asymmetry.** `find_project_root` walks up; `collect_evidence`, `is_dev_mode`, and startup project binding treat the given dir as the root. Nothing in the type system distinguishes "any cwd" from "resolved project root" — Blocker 1 lives exactly on that seam, and the startup-CWD coupling (pipeline executor, project_id, git_manager, wiki cron in prior reviews) is the same disease at daemon scale.
7. **Dead code kept alive by its own tests.** mathutil2, tool_summarizer halves, decode_llm_response, DaemonClient cache, project_init helpers, validation.py's structurally-impossible checks, telemetry tracing helpers — a substantial fraction of utils/ has no live caller, and unit tests mask the dead-code signal.
8. **Untracked PII posture.** Raw hardware GUID in the hub, session UUIDs as Prometheus labels, `capture_args` stringification waiting for a caller, world-readable logs, and a capture_content flag that doesn't do anything — individually small, collectively a privacy contract that exists in config comments but not in code.
