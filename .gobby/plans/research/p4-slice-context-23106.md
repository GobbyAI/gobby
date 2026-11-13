# P4 slice working context (#23106, Lane 7 gobby#14682)

This is the working context for drafting `.gobby/plans/gdaemon-api-keys-nodes.md`, rooted at #21555. The source plan of record is `.gobby/plans/gdaemon-front-door.md`. On 0.5.0, P4 runs from line 546 and P5 from line 1082. On the #23098 branch a909d55 the PD disposition is at lines 373-385 and P4 starts at 386; that branch is not merged. The section bodies are otherwise identical.

## Obligations to carry forward from obsolete #23058 3792fa4

Do not rebase or land 3792fa4. Carry only these obligations:
- 4.2 migration becomes `456_add_api_keys.sql`, or the next free number. The highest on disk is `455_drop_ask_artifacts.sql`.
- Register it in `crates/gcore/src/schema/assets.rs::MIGRATIONS`.
- Update the 5 signed goldens under `tests/runtime_grants/golden/`, which embed the schema identity. Add acceptance items 4.2.6 and 4.2.7.
- 4.3 drops the retired Ask targets `tests/ask/http_mcp_support.py` and `tests/servers/routes/test_ask.py`.
- The P5 contained-runner seed stays in #21554 for its later slice; it is not part of P4.

## PD disposition (2026-09-29)

- 4.4 stays before P5.
  - The pair test launches the node as a bare `gdaemon serve` with the node bootstrap. `crates/gdaemon/src/serve.rs::watch_parent_fd` is optional without `GOBBY_PARENT_FD`.
  - Drop the clause "node runs no maintenance loop (2.3)" and drop the 2.3 depends.
- 5.2 gains "a node gdaemon never spawns or supervises a Python backend". That belongs to the P5 slice.
- Before P5, a code guard refuses `gobby start` and lease promote/recover on a node bootstrap. Documentation alone is insufficient.

## Gap inventory for P4

Cross-cutting:
- Name the landed topology: `src/gobby/runner_front_door.py::FrontDoorChild` with `GOBBY_PARENT_FD`. The runner spawns gdaemon; `gobby start` does not.
- Add module registration files to Targets when new modules are created: `crates/gcore/src/lib.rs` (tls, api_key_format), `crates/gdaemon/src/lib.rs` (state, nodes), and `crates/gdaemon/src/front_door/mod.rs` (tls, auth, challenge).
- `crates/gcore/src/bootstrap.rs` is 856 lines, and 4.1, 4.2 and 4.4 all grow it. Plan its split in 4.1: the production-size-growth lint applies at 850 or more lines, and Rust counts stop at `#[cfg(test)]`, so re-measure first.

4.1: add the acceptance test files `crates/gcore/tests/tls.rs` and `tests/e2e/test_daemon_lifecycle.py` to Targets. `rcgen` and `tokio-rustls` are not yet in `crates/gdaemon/Cargo.toml`, as expected.

4.2:
- Fix the migration number, `MIGRATIONS` registration and goldens as above.
- State which Rust struct carries `api_key`, `api_key_id` and `hub_cert`. `HubDatabaseBootstrap` (`bootstrap.rs:38`) has no mode fields, and `FilesHomeView` has test-only consumers.

4.5: no drift.

4.3:
- Drop the Ask targets.
- Re-run the `local_cli_token|X-Gobby-Local-Token|api_token_hash` sweep: 76 files match, and many production hits are unlisted.
  - ghook: `src/transport.rs`, `diagnose.rs`, `schemas/diagnose-output.v2.schema.json`
  - gclient: `frame_source.rs`, `startup.rs`, `command.rs`
  - gcode: `cli_error.rs`, `savings.rs`, `commands/embeddings_doctor.rs`, `vector/code_symbols/embedding.rs`, `graph/code_graph/lifecycle.rs`
  - gcore: `ai/daemon.rs`, `ai/daemon/operations.rs`, `ai/generation/transport.rs`
  - Python: `terminals/frame_client.py`, `terminals/native_runtime.py`, `runtime_grants/maintenance.py`, `cli/installers/remote_preflight.py`
  - `skills/gobby/references/admin/authentication.md`, plus about 15 docs.
- The per-boot secret is generated in `FrontDoorChild`, not in `gobby start`. Add `runner_front_door.py` and `tests/test_runner_front_door.py`.
- 4.3.9 (`_implementation.py` split) is stale: the file is 881 lines, and the token moved to `agents/spawn_models.py`, `spawn_executor_providers.py` and `spawn_agent/_request.py`.
- 4.3.8 still holds: `inbox.py` is 983 lines.
- Restore the depends on corpus-slice 3.1. 4.3 re-records the auth cases at `schema_version` 2.

4.4:
- Apply the PD pair-test edit.
- Own creating `crates/gdaemon/src/state.rs` `AppState`/`ModeServices`, and adopt the orphaned former 2.2.1 (no `Option` mode fields).
- Own Rust `hub` and mode parsing: `HubDatabaseBootstrap` and `parse_hub_database_bootstrap` in `crates/gcore/src/bootstrap.rs`, with `(remote, true)` rejected.
- Depend on run-modes 2.1.

Node-refusal guard: new first P4 leaf, depending on run-modes 2.1 `run_mode()`.
- Seam: `src/gobby/runner.py::run_gobby`, between `load_bootstrap` (:432) and `ActiveDaemonLease` (:440). `StandbyLeaseControl` is only built there, so this covers start, service launch, `try_acquire`, and standby promote/recover.
- Add early CLI refusals in `cli/daemon_start.py` and `cli/daemon_preflight.py::restart_start_refusal`.
- Side effect: the shared-stack client daemon `(remote, false)` is refused until 4.4. Add a note to `docs/guides/shared-stack.md`.

D1-D3 stay consistent. D3 inherits 4.3.7's corpus wait.

## Slice-plan patterns to mirror (#23098 at a909d55)

Read `.gobby/plans/gdaemon-run-modes.md` and `gdaemon-http-contract-corpus.md` from a909d55 with `git show a909d55:<path>`. Each has an Overview naming the root epic and the single phase, a Decision Record, Constraints, deliverables with Targets, Research context, Consumers unchanged and Acceptance, D sections, a V1 changelog and V2 verification. M1 is added by the Adversary.
