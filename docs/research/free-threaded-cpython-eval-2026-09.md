# Free-threaded CPython as an interim daemon runtime (#22947)

Status: **NO-GO for a live switch** (PD gobby#14610, 2026-09-26). Rust remains the
destination. This note records the evidence so the question can be reopened only when
a blocker below changes.

Evidence labels: VERIFIED means observed with a command in this evaluation; INFERRED
means reasoned from code or docs.

## Question

#22946 found that hook and rule work is already off the asyncio main thread (rule-loop
pool 16, rule-engine pool 16, hook adapter executor, workflow runtime 8 workers) but
still holds the process GIL: hooks plus workflows were 21.5% of GIL-holder samples on
2026-09-24 and 34–36% on 2026-09-25, with only 0.8% on the main thread. Could a
free-threaded (PEP 703) interpreter remove that contention without a process split?

## Interpreters (VERIFIED)

| | Live daemon | Evaluation venv |
| --- | --- | --- |
| Build | Homebrew CPython 3.14.7, GIL build | uv `cpython-3.14.7+freethreaded-macos-aarch64-none` |
| `python -VV` | 3.14.7 | `3.14.7 free-threading build (main, Sep 24 2026, 17:47:03) [Clang 22.1.3]` |
| ABI tag | `cp314` | `cp314t` (`sys.abiflags == "t"`) |

The evaluation ran only in a scratch venv. Nothing touched the daemon, its database, or
the product lockfile.

## Native dependency wheels (VERIFIED)

Native extensions mapped into the live daemon (from `lsof`): argon2-cffi-bindings,
brotli, cffi, charset-normalizer, cryptography, protobuf, grpcio, httptools, jiter,
markupsafe, msgspec, numpy, orjson, psutil, psycopg-binary, pydantic-core, regex,
rpds-py, watchfiles, websockets, pyyaml.

| Package | Locked | cp314t wheel (macOS arm64) | Consequence |
| --- | --- | --- | --- |
| grpcio | 1.76.0 | none at any version | hard blocker (see imports) |
| psycopg-binary | 3.3.4 | none at any version | psycopg falls back to the pure-Python `pq` implementation |
| orjson | 3.11.8 | none at any version | needs a source build or a replacement |
| brotli | 1.2.0 | none at any version | needs a source build (gzip-only fallback is a product change) |
| httptools | 0.7.1 | only 0.8.0 | version bump required |
| psutil | 6.1.1 | only 7.2.2 | major-version bump required |
| All other natives above | locked | available | import with the GIL still disabled |

Optional ML extras (torch/ctranslate2/chatterbox family) also lack cp314t wheels. They
are not on the daemon's core import path.

## Import results

All three results are VERIFIED in the scratch venv:

1. **Default (`PYTHON_GIL` unset), unmodified tree.** `import gobby.runner` fails:
   `ModuleNotFoundError: grpc`. There are two independent routes:
   - `src/gobby/telemetry/exporters.py:12` imports the OTLP gRPC span exporter at
     module scope, and the config default is `otlp_protocol='grpc'`.
   - `qdrant_client` 1.19.0 imports `grpc` at package import. It is reached from
     `src/gobby/memory/vectorstore.py:14` and `src/gobby/cli/hub_backup/_stores.py:23`.
2. **Diagnostic only: the OTLP gRPC exporter stubbed** in `sys.modules`, then every
   `gobby.*` module imported via `pkgutil.walk_packages`:
   - 1,608 modules imported;
   - `sys._is_gil_enabled()` stayed `False` throughout, so no loaded extension
     re-enabled the GIL;
   - 28 modules still failed on `grpc` through `qdrant_client`, among them
     `gobby.runner_init`, the HTTP server modules and `hook_manager`.

   This stub is evidence tooling, not a proposed fix. The stub's success does not mean
   the daemon can run.
3. **Forced (`PYTHON_GIL=0`), same stubbed walk.** The result was identical: the GIL
   stayed off and the same 28 grpc failures remained.

psycopg under the free-threaded build loaded `psycopg.pq.__impl__ == "python"` with
libpq 180006 (VERIFIED). The daemon's hub path would therefore run on the ctypes
implementation, a throughput regression that was not measured here.

## Thread-safety audit (Phase B, bounded, read-only)

This is a scoped audit of module-level mutable state on paths reached from worker
threads (`workflows/`, `hooks/`, `telemetry/`, `storage/`, `sessions/`, `mcp_proxy/`,
`servers/`). It is INFERRED from source unless marked.

- **The worker-thread shared state already has locks.** Each of the following is
  guarded by `threading.Lock`:
  - `hooks/adapter_execution.py` `_session_admissions` (`_session_admissions_lock`,
    plus a `CrossLoopFifoLock` for each admission);
  - `hooks/session_activation.py` `_ACTIVE_RULE_NAMES_CACHE`;
  - `sessions/activity.py` `_SESSION_ACTIVITY_TIMESTAMPS`.

  65 modules in `src/gobby` create `threading.Lock`/`RLock`. This discipline exists
  because the daemon already runs work across threads. Free threading changes which
  interleavings are fast, not which ones are legal: the GIL never made compound
  read-modify-write atomic.
- **Unlocked caches are idempotent single operations.**
  - `workflows/git_utils.py` `_WORKTREE_ROOT_CACHE` does a single `dict.get` and a single
    store of a value derived only from the key.
  - `storage/task_close_reviews.py` `_finalizing_in_process` does a single `set.add`,
    `set.discard` or membership test.
  - The free-threaded build keeps individual dict and set operations atomic through
    per-object critical sections, so a race costs at most one duplicate
    `git rev-parse`. There is no defect to route.
- **These containers are touched only on the event loop.** The following are mutated
  only from coroutines, so free threading does not change them:
  - `sessions/transcript_index_sidecar.py` `_INDEX_CACHE`, guarded by an `asyncio.Lock`;
  - `mcp_proxy/services/output_repair.py` `_INDEX_CACHE`, where `to_thread` returns
    before the store runs on the loop;
  - the background-task registries (`set[asyncio.Task]`).
- **`functools.lru_cache`** (`workflows/safe_evaluator.py`, `workflows/tdd_paths.py`,
  `storage/schema_contract.py`, `storage/agents/_sandbox_records.py`) is internally
  synchronised in free-threaded CPython 3.13 and later.
- **`threading.local`** is used only in `telemetry/logging.py` (re-entrancy guard) and
  in the CLI installer lock. Both remain correct.

Library guarantees:
- Every native module that has a cp314t wheel declared free-threading support: none
  re-enabled the GIL on import (VERIFIED, section 2 above).
- asyncio, `logging`, `concurrent.futures`, psycopg-pool and the OpenTelemetry SDK
  synchronise with explicit locks and do not rely on the GIL (INFERRED from upstream
  sources).
- The audit found no correctness defect that blocks a free-threaded runtime. It is not
  proof of absence: C-extension internals and third-party pure-Python modules outside
  the listed paths were not reviewed.

## Blockers

1. **grpcio** has no cp314t wheel, and there are two import routes (the OTLP gRPC
   exporter and `qdrant_client`). Fixing this needs either a scratch source build of
   grpcio for cp314t, or product changes: a lazy or HTTP OTLP exporter, and a lazy
   qdrant import or `prefer_grpc=False` without the grpc import. The PD ruled that
   product imports, telemetry and Qdrant must not change to make the experiment run.
2. **psycopg-binary** has no cp314t wheel. The fallback is pure Python; psycopg-c
   needs a source build.
3. **orjson** and **brotli** have no cp314t wheel at any version.
4. **httptools** 0.7.1 → 0.8.0 and **psutil** 6.1.1 → 7.2.2 need lockfile bumps
   before cp314t wheels exist.
5. **Unmeasured.** Nothing has measured single-thread slowdown on the free-threaded
   build, the cost of pure-Python psycopg, or the actual GIL-contention win under daemon
   load. All three need a PD-granted heavy slot.

## Path if reopened

Option (a) is queued behind Chrome/stability validation and has no slot yet:
1. Scratch-build grpcio and psycopg-c (plus orjson and brotli) for cp314t outside the
   repo.
2. Rerun the unstubbed import of `gobby.runner` under default and `PYTHON_GIL=0`
   settings.
3. Run an isolated test daemon on temporary state and ports under recorded hook load.
   Compare rule_evaluation and admission_wait latency and loop lag with the GIL build.

Rollout, if it were ever warranted, would ship an alternate interpreter pin behind an
operator switch. Rollback would restart on the GIL interpreter, and no schema or data
changes are involved. Given the wheel gaps, the process-boundary rule evaluation
proposed in #22946 and the Rust plan remain the recommended routes to relieve GIL
contention.
