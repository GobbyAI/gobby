# Rerun provisioning: gcode and Graphify, 2026-11

Owner task #22948; leaf #25815 (plan 2.1). The harness lives in `harness/`. It is a dated copy of
the September harness with these changes only: the runtime root, owner task, compose project and
names, database and account identity, the gcode pins, the comparator narrowing to gcode and
Graphify, and the symlink contract in `runtime_boundary.py`.

Live steps belong to the orchestrator root pane. A developer seat runs no step below.

## Identity

| Item | Value |
| --- | --- |
| Runtime root | `DEFAULT_RUNTIME_ROOT` in `harness/provision_environment.py` |
| Owner task | `#22948` (`TASK_REF`) |
| Attempt | `rerun2611b` |
| Owner session | `7674aa11-70a3-40d5-91dd-1fe46140732b` (orchestrator root pane) |
| Compose project | `gcode-graphify-rerun-2026-11` |
| Source commit (gobby, gcode) | `1a13024ceda47fb25bebefc8b42ee9d2e372778d`, the last gobby commit that changed `src/gobby` (reviewed tree, digest `62f58212...`, 3039 files). Was `0bf234a9` before the successor; see the pin move under Judgment calls |
| gcode pins | version read from `sources/gobby/crates/gcode/Cargo.toml` `package.version` (archived at `GOBBY_SHA`; `1.9.105` at candidate time), frozen into the command manifest and checked against `gcode --version` at C0; contract `12` |
| Graphify | PyPI `graphifyy==0.9.103` (version-only pin, `GRAPHIFY_PACKAGE` in `harness/launch_services.py`); recorded by version, executable SHA-256 and lock |
| Graphify wheel | `graphifyy-0.9.103-py3-none-any.whl`, SHA-256 `7948c030f8b04153d88bc1ee23a485ddaa0c2d6aaa03a748c177e3c217bcfe34` (PyPI, latest release at review time) |
| Database and account | `gobby_bakeoff_22948`; account email `bakeoff-22948@example.invalid` (reserved placeholder) |

## Order of live steps (orchestrator)

Pre-steps, in this order, before step 1. They clear the stopped attempt `rerun2611a`, whose root is kept as evidence.

0. Teardown: `uv run --no-sync python docs/evidence/gcode-graphify-rerun-2026-11/harness/launch_services.py teardown --owner-session 7674aa11-70a3-40d5-91dd-1fe46140732b`. Then the lead records the evidence manifest of the `rerun2611a` root.
0b. Move: `mv "/Users/josh/.gobby/agents_workspaces/d45545c5-ded5-4335-b115-0245752edacf/epics/epic-22948/build/gcode-graphify-rerun-2026-11" "/Users/josh/.gobby/agents_workspaces/d45545c5-ded5-4335-b115-0245752edacf/epics/epic-22948/build/gcode-graphify-rerun-2026-11-rerun2611a-evidence"`. The root is `DEFAULT_RUNTIME_ROOT`. `init` refuses an existing root (`init_runtime` raises `FileExistsError`), and `launch_services` always uses `DEFAULT_RUNTIME_ROOT`, so the old root has to move aside. Nobody deletes it.

1. `provision_environment.py init --owner-session <session>`: creates the runtime root, archives
   the sources at `GOBBY_SHA`, copies the templates and corpora, and writes the manifests.
   It refuses an existing root.
1a. `provision_environment.py write-manifest --owner-session <session>`: writes
   `config/project-manifest.json` for the 54 projects in "Project set". It refuses a missing sealed corpus.
1b. `provision_environment.py materialize-workspaces --owner-session <session>`: copies each project's
   corpus to `workspaces/<name>`, keeping symlinks. It checks all 54 targets before it copies anything,
   and refuses an existing workspace.
2. `provision_environment.py write-compose --owner-session <session> ...`: refuses any reference that is not
   digest-pinned or that names `:latest`, renders `config/compose.yaml` and checks the rendered text for
   `:latest`, and records `receipts/image-references.json`. Then it runs `docker image inspect` on each
   reference and `docker pull` only for the references that are missing. A failed pull stops the step
   with `docker pull failed for <service> image <reference>: <stderr>`.
2a. `provision_environment.py build-probe --owner-session <session> --probe-source <path> --candidate <sha> --gobby-repo <path>`,
   run once after step 2 and before step 3. `--probe-source` is the worktree's
   `crates/gcore/examples/managed_bearer_probe.rs`; `--candidate` is a full gobby commit whose crates diff from the pin is
   exactly that file, with `Cargo.lock` unchanged. It archives the pinned tree, requires the example absent
   there, builds `probes/managed_bearer_probe` with `cargo build --locked` under `build/probe-target`, and writes
   `manifests/bearer-probe.json` with `example_in_pin: false`. It does not run the probe.
3. `launch_services.py provision --owner-session <session> --attempt <id>`:
   - writes `isolation/external-state.before.json` once;
   - renders Compose, checks names and ports, and starts the three services, or verifies the
     existing services receipt and skips the start;
   - builds `tools/graphify/venv` (`graphifyy==0.9.103`) and `tools/gobby/venv` (`uv sync --frozen`
     from `sources/gobby`), each through a probe receipt;
   - copies the installed binary set into `gobby-home/bin` through `install_binary_set`, with member names from `runtime_gobby.py` (`binary-set-files`); runs `prepare_daemon.py` and `prepare_identity.py`. Every step that touches `gobby` in step 3 runs in `runtime_gobby.py` under `tools/gobby/venv/bin/python` (the gobby-sync environment) with `-I`, one subprocess per operation (`binary-set-files`, `identity`, `prepare-daemon`, `prepare-identity`). Each operation first checks that its `gobby` resolves under `sources/gobby`; every operation except `binary-set-files` also checks that `gdaemon` resolves to `gobby-home/bin/gdaemon`, and refuses otherwise. `prepare()`, `prepare_identity()` and `launch_services.py` load no `gobby` module in the harness process; the tests check this. `prepare-identity` writes the account file before it creates the user, and keeps a file that already exists. Before any write, `prepare_daemon.py` probes the configured embedding provider: `qwen3-embedding:0.6b-q8_0` at `http://localhost:11434/v1` (Ollama), with `GET <api_base>/models`, no proxy, and no key. It refuses when the provider is unreachable, does not answer JSON, or does not list the model. The isolated config takes model, `api_base` and dim 1024 from these values; operator config is never read;
   - runs `gobby init --name <name>` in each of the 54 workspaces that `config/project-manifest.json`
     lists, with the daemon ports bound by nothing, through probe receipts. `_workspaces(root)` reads
     the manifest and refuses any list other than `project_entries()`;
   - asserts zero `code_indexed_file_states` rows for each of the 54 workspace projects.
4. `launch_daemon.py --owner-session <session> --attempt <id>`, under the SRT sandbox. It first checks the gobby modules and the gobby package under "Gobby modules checked before step 4". It launches the supervised path, `gobby-home/bin/gdaemon serve`, with `GOBBY_BACKEND_COMMAND` set to `tools/gobby/venv/bin/python -m gobby.runner --config <bootstrap>`, so gdaemon runs the backend. The step records the launch in `active-launches.json`, then polls `GET http://127.0.0.1:<daemon_port>/api/health` (120 probes, one second apart). It passes only when the daemon answers HTTP 200 with JSON `status` `"ok"` and the wrapper is still alive. HTTP 200 with `status` `"degraded"` is a failure: the probe keeps polling until the budget runs out, then the step refuses and names the degraded components (`degraded_services`, and `hook_runtime <state>` when the hook runtime is incompatible). A failed launch stops its srt wrapper group and keeps its records (`receipts/daemon-launch-<attempt>.json`, `receipts/active-launches.json`, `logs/install/daemon-<attempt>.log`). Its error carries the note `cleanup unproven`, because gdaemon starts its backend in a process group the harness cannot observe. Any kept record blocks every later launch, whatever its attempt name, before the port probe runs. The refusal names every kept record, the recorded process group (the srt wrapper's), and the clear command: confirm with `ps -axo pid,pgid,comm` that no gdaemon or backend process remains, then `rm -- <every named record>`. The backend's process group is not recorded.
5. `record_installations.py --owner-session <session> --attempt <id>`.
6. `launch_services.py observe --owner-session <session> --attempt <id>` writes the after-snapshot
   if absent. It fails unless the containers are healthy and the daemon is alive and healthy at its
   `/api/health` route.
7. `c0.py <attempt> --owner-session <session>`: the C0 run, described below.
8. `validate_environment.py --live`.

This is the order ruled by the orchestrator (7d30ebfd). Validation runs last, after C0.

Teardown is leaf 2.2, not one of these eight steps. `launch_services.py teardown --owner-session
<session>` writes the final snapshot, then removes the containers, network and volumes listed in
`receipts/ownership-live.json`, after checking each one's identity and compose label. It writes
`receipts/teardown.json`.

Every step writes a receipt under `receipts/` or `logs/install/`. A probe whose receipt exists and
verifies is skipped on rerun. A failed probe is preserved, and a new attempt ID is required.

## Gobby modules checked before step 4

Steps 4, 7 and 8 import `gobby` from the epic tree in the harness process. Before step 4 starts
the daemon, `launch_daemon.py` measures both copies at run time: the epic tree (`src/gobby`) and
the installed copy in `tools/gobby/venv`. It fails closed, naming the first path, on any missing
copy, any byte difference, or any difference in the file set. Both copies' hashes, the
aggregate digests and the file count go to `receipts/gobby-module-identity-<attempt>.json`.

Each direct-import module must hash equal in both copies. The gobby package is compared as a
whole: every file under `gobby/` except `__pycache__` directories and `*.pyc`, hashed with sha256,
then folded into one digest per side from the sorted `path sha256` lines. Non-`.py` files count:
step 8 reads `gobby/storage/schema_expected_identity.json` from this package.

Expected values. The `tools/gobby/venv` column is the `GOBBY_SHA` blob that `sources/gobby` is
archived from. The table states what each copy must hash to; the live check measures both copies
on each run.

| Module | Direct importer | Expected, epic tree (`src/gobby`) | Expected, `tools/gobby/venv` (`GOBBY_SHA` blob) | Check |
| --- | --- | --- | --- | --- |
| `gobby.agents.srt_monitor` | `launch_daemon.py` (step 4) | 86958462fb14e08e373c718f6ed387a853541e351cf0c4f4f226c83876f12af3 | 86958462fb14e08e373c718f6ed387a853541e351cf0c4f4f226c83876f12af3 | equal |
| `gobby.agents.srt_runtime` | `launch_daemon.py` (step 4) | cb4424dd49ab8ba0e5c3d58e7c9f2c58e1dc6daad4f9932e768aa80a0b40edf5 | cb4424dd49ab8ba0e5c3d58e7c9f2c58e1dc6daad4f9932e768aa80a0b40edf5 | equal |
| `gobby.storage.schema_divergence` | `validate_environment.py` (step 8) | 5b054b2da21c043fb77825c7517fd7cf0e8d07889045fc6f4da9a20fe7d63d69 | 5b054b2da21c043fb77825c7517fd7cf0e8d07889045fc6f4da9a20fe7d63d69 | equal |
| `gobby.utils.native_bin` | `validate_environment.py` (step 8), `schema_head.py` | c37671789aa81b1eef3835d8f89f9212ab4cbb11349bf932b08098b1ed8151e9 | c37671789aa81b1eef3835d8f89f9212ab4cbb11349bf932b08098b1ed8151e9 | equal |

Gobby package aggregate, expected:

- File count: 3039 files in each copy (2196 `.py`, 843 other), with the same set of paths.
- Aggregate digest: `62f582121e9734c706dc3d933060a9bd84c4e19bf5a8dfbfca523d4cf35667dd` in each copy.

Residual: third-party packages that gobby imports are not hashed.

### Status at this candidate (M1)

Read-only, no launch. The values use the same formulas as `compare_package_copies` and
`package_digest`. The pin is `GOBBY_SHA = 1a13024ceda47fb25bebefc8b42ee9d2e372778d`, the last
gobby commit that changed `src/gobby`. Its `src/gobby` equals the epic HEAD's `src/gobby`.

| Copy | Files | `.py` | Other | Aggregate digest |
| --- | --- | --- | --- | --- |
| Pin archive `git archive 1a13024c src/gobby` | 3039 | 2196 | 843 | `62f58212...` (matches the expected value) |
| Runtime `sources/gobby/src/gobby` | 3039 | 2196 | 843 | `62f58212...` (byte-identical to the pin) |
| Epic tree `src/gobby` at `eaf536aca6` | 3039 | 2196 | 843 | `62f58212...` |

- Checked at the successor: `package_digest` over the pin archive and over the epic tree are equal
  (3039 files each, no differing path). `git diff` of `src/gobby` between `1a13024c` and HEAD is empty.
- `verify_gobby_modules` passes at step 4: every pin file equals the epic file. The four direct-import
  modules are equal in all copies.
- The prior pin `0bf234a9` (3031 files, digest `467edf61...`) differed from the epic tree in 69 changed
  and 8 added files. Its refusal at step 4 was correct for that pin.
- `crates/` and `Cargo.lock` are identical between `0bf234a9` and `1a13024c`. The only crate path that
  differs from `1a13024c` to HEAD is `crates/gcore/examples/managed_bearer_probe.rs`, so the
  `build_probe` premise (the pinned tree lacks the example, and the candidate changes only that file under
  crates) holds for the new pin. `build_probe` is unchanged.
- `tools/gobby/venv` is not built. Its side is proven only through the archive, which equals the pin.

## Command lines for attempt `rerun2611b`

Run from the worktree root, as the orchestrator. Every line uses the same owner session. Every command runs `uv run --no-sync`: a plain `uv run` rebuilds the gobby package in this checkout.

```bash
uv run --no-sync python docs/evidence/gcode-graphify-rerun-2026-11/harness/provision_environment.py init --owner-session 7674aa11-70a3-40d5-91dd-1fe46140732b
uv run --no-sync python docs/evidence/gcode-graphify-rerun-2026-11/harness/provision_environment.py write-manifest --owner-session 7674aa11-70a3-40d5-91dd-1fe46140732b
uv run --no-sync python docs/evidence/gcode-graphify-rerun-2026-11/harness/provision_environment.py materialize-workspaces --owner-session 7674aa11-70a3-40d5-91dd-1fe46140732b
uv run --no-sync python docs/evidence/gcode-graphify-rerun-2026-11/harness/provision_environment.py write-compose --owner-session 7674aa11-70a3-40d5-91dd-1fe46140732b --postgres-image gobby-postgres-local@sha256:443364cb8e901fcbf902ee0435e9fc2333cb000d7351e22bd78485365e4d3d02 --qdrant-image qdrant/qdrant@sha256:057ee3a8da769fe7310dd3537b4dc7583bf87a95ce8ac43c0af5a46bc580d1fc --falkordb-image falkordb/falkordb@sha256:e93fcd753fe612fb0a222166a0620a1ae31b826a12f223c3b6d06038d9d7a364
uv run --no-sync python docs/evidence/gcode-graphify-rerun-2026-11/harness/launch_services.py provision --owner-session 7674aa11-70a3-40d5-91dd-1fe46140732b --attempt rerun2611b
uv run --no-sync python docs/evidence/gcode-graphify-rerun-2026-11/harness/launch_daemon.py --owner-session 7674aa11-70a3-40d5-91dd-1fe46140732b --attempt rerun2611b
uv run --no-sync python docs/evidence/gcode-graphify-rerun-2026-11/harness/record_installations.py --owner-session 7674aa11-70a3-40d5-91dd-1fe46140732b --attempt rerun2611b
uv run --no-sync python docs/evidence/gcode-graphify-rerun-2026-11/harness/launch_services.py observe --owner-session 7674aa11-70a3-40d5-91dd-1fe46140732b --attempt rerun2611b
uv run --no-sync python docs/evidence/gcode-graphify-rerun-2026-11/harness/c0.py rerun2611b --owner-session 7674aa11-70a3-40d5-91dd-1fe46140732b
uv run --no-sync python docs/evidence/gcode-graphify-rerun-2026-11/harness/validate_environment.py --live
```

The write-compose images are the September digests from `receipts/image-references.json`
(recorded 2026-09-07). `gobby-postgres-local` is a locally built image name, so it must exist on
this machine before step 2. Otherwise the orchestrator picks new digests.

## Project set (criterion 2.1.3)

The criterion set is 54 gcode projects. Every project draws from `corpora/gcode/<case>` into its own
`workspaces/<name>`. This replaces the 24 comparator workspaces that `_workspaces()` registered
before (`corpora/graphify/<case>` and `corpora/gcode/<case>`). The Graphify side has no project set
in this manifest; the lead must confirm that before the live run.

- Six scored cases C1 to C6. Each has six repetitions (`gcode-<case>-r<1..6>`, role `repetition`) and
  two spares (`gcode-<case>-spare<1|2>`, role `spare`, repetition slots 7 and 8): 48 projects.
- Six singles: `gcode-C7-interrupted` and `gcode-C7-clean` (C7), `gcode-C8-baseline` (C8-baseline),
  `gcode-C8-change` (C8-change), `gcode-C1-labels-properties` (C1), `gcode-C0-probe` (C0): 6 projects.
- `project_entries()` is the single definition. `write-manifest` writes `config/project-manifest.json`
  with `count` 54. `read_project_manifest` refuses any manifest that differs from `project_entries()`
  ("criterion 2.1.3"). `launch_services._workspaces(root)` registers exactly those projects.
- Tests: `test_project_manifest.py`, eight tests. All were RED on the stubs before the implementation.

## Docker call sites and the seat-run boundary

Ruling 64fbdf7f: the Orchestrator runs `write-compose` and `provision` on the host lane, and the
other Docker-reaching steps are live steps of the same orchestrator root pane. No developer seat runs
any of them.

| Call site | Docker command | Reached from |
| --- | --- | --- |
| `provision_environment.py` `ensure_images` | `docker image inspect`, `docker pull` (through `_docker_command`) | `write-compose`, step 2 |
| `provision_environment.py` `external_state_snapshot` | `docker ps`, `docker volume ls` (through `_command_observation`) | `launch_services.py` `provision`, `observe`, `teardown` |
| `launch_services.py` `docker` (helper) | `docker <args>` through `subprocess.run` | `prepare`, `check_fresh`, `record_services`, `_verify_services`, `teardown` |
| `launch_services.py` `start_services` | `docker compose ... up --wait` | `provision`, `start` |

| Subcommand or action | Docker | Who runs it |
| --- | --- | --- |
| `provision_environment.py write-compose` | yes | Orchestrator, host lane |
| `launch_services.py provision` (step 3), `observe` (step 6), `check`, `start`, `teardown` | yes | Orchestrator, live step |
| `provision_environment.py init`, `refresh-manifest`, `build-probe`, `write-manifest`, `materialize-workspaces` | no | seat-run |
| `verify_corpora.py` `main`, `verify_corpora`, `compare_manifests` | no | seat-run |
| `binary_set.py` `install_binary_set` | no | seat-run |

`test_docker_boundary.py` enforces the seat-run column statically. From each seat-run branch and
function it follows bare-name and attribute references into harness functions, and fails if any
reached function contains the string `"docker"`. The walk over-approximates, since it matches names
rather than resolved imports. The positive control requires the same walk to reach Docker from
`write-compose`.

## C0 (step 7)

`c0.py` checks ownership of the runtime, then runs every command under the SRT wrapper. Its policy is
`srt/c0-settings-<attempt>.json`, and violations go to `logs/c0/<attempt>/violations.jsonl`.
The policy is the daemon's SRT policy plus read-only `allowRead` of the graphify interpreter's
resolved base install (`c0_policy` in `c0.py`). The venv `python` is a symlink into a uv-managed base
outside the runtime root, and SRT denied that read without it.

1. **Graphify concurrency probe.** `graphify --help` and `graphify extract --help` are captured under
   `logs/c0/<attempt>/probe/`, with each stdout, its stderr and its return code in
   `graphify-probes.json`. Both must return 0, or the probe stops before deciding. Graphify 0.9.103's
   `extract --help` only points to the top-level help, so the flag is read from either output. If the
   top-level or the extract help lists `--max-workers`, the extract command gets `--max-workers 1`.
   The value and its evidence come from September's `run-records.jsonl` line 3, where
   `GRAPHIFY_NATIVE_CONCURRENCY` is `"1"` and `command_argv` contains `--max-workers 1`. If neither
   output lists the flag, the receipt records `source: unsupported` and the extract runs with default
   workers. This run sets the flag only. `GRAPHIFY_NATIVE_CONCURRENCY` is not set (orchestrator ruling
   4b, via gobby#19309).
2. **Frozen manifest.** `command-manifest.json` lists each exact argv and the gcode pin, which is the
   archived crate version. The first gcode command is the cold index,
   `gcode --project <C0> --format json index <C0> --full --sync-projections`.
3. **C7 poller.** The cold index runs under `IndexPoller`. It samples
   `SELECT count(*) FROM code_indexed_file_states WHERE project_id = %s` every 0.2 s over a
   read-only connection, and logs to `receipts/c7-poller-<attempt>.jsonl`. The project id is read
   from `workspaces/gcode-C0-probe/.gobby/project.json`, the registered C0 workspace, as `launch_services` reads it. The sealed `corpora/gcode/C0` is not a project. The clean count is
   `file_count` in `manifests/corpora/gcode/C1.json` (149), checked against its file entries. C7
   passes only if a sample with `0 < count < 149` is observed. A failed sample read, of any kind,
   fails the poller: the qualifying marker is withdrawn and `stop()` raises.
4. **Probe symbols.** `load_policy`, `repository_root`, `plan_product`, `MCPError`, and `SettingsError`
   are verified in the September C0 corpus copy. They span three files, which meets the minimum of
   three: `src/game_goblins/platform/settings.py` (`SettingsError`, `repository_root`,
   `load_policy`), `src/game_goblins/replenishment/planner.py` (`plan_product`), and
   `src/game_goblins/platform/mcp.py` (`MCPError`).

A non-zero exit from the cold index stops C0 with an `EvidenceError` that carries its exact stderr.
The stderr is also kept in `logs/c0/<attempt>/gcode-index-cold.stderr`, and no
`receipts/c0-<attempt>.json` is written. Otherwise the receipt is `receipts/c0-<attempt>.json`, and
`c0.py` exits 0 only if every check passes. C0 checks that `gcode status --format json` reports an
`id` equal to the project id in `project.json` (`gcode-status-project-id`). C0 has no schema-identity
gate. `gcode schema-identity --json` is recorded as unchecked output in
`logs/c0/<attempt>/gcode-schema-identity.stdout`. The archived six-key identity
is checked at step 8 by `validate_environment.py --live`, through `schema_head.schema_head_proof`.

Exit status (H2). A Graphify marker counts only when the extract exits 0 (`evaluate_c0`). A marker
printed before a nonzero exit fails C0. The inventory needs exit 0 from both the top-level help and
the export help (`_inventory_ok`). Regression tests: `test_a_graphify_marker_printed_with_a_failing_exit_does_not_pass_c0`
and `test_a_graphify_export_help_that_exits_nonzero_fails_the_inventory`. Both were RED before the fix.

## Receipts

- `isolation/external-state.before.json`, `.after.json`, `.final.json`
- `receipts/c0-<attempt>.json`, `receipts/c7-poller-<attempt>.jsonl`, `logs/c0/<attempt>/`
- `receipts/services.json`, `receipts/ownership-live.json`, `receipts/compose.rendered.json`
- `receipts/gcode.json`, `receipts/installations.json` (graphify only)
- `receipts/probe-*-<attempt>.json` and `logs/install/*.log`
- `receipts/daemon-launch-<attempt>.json`, `receipts/active-launches.json`
- `receipts/runtime-verification.json` (written by `validate_environment.py --live`)
- `receipts/teardown.json`

## Judgment calls and what is unverified

- The source pin moved at the successor from `0bf234a9` to `1a13024ceda47fb25bebefc8b42ee9d2e372778d`, the last
  gobby commit that changed `src/gobby`. Reason: step 4 compares the runtime copy with the reviewed tree
  (digest `62f58212...`, 3039 files). Edits against September from this move: `GOBBY_SHA` only. `build_probe`
  and the step-4 check are unchanged. Re-verify the digest and the 3039-file count after any later re-pin,
  including to the landed 0.5.0 tip.
- `validate_environment.py --live` checks the daemon through its SRT wrapper's descendants (`validate_live_processes`).
  The wrapper itself must not listen. Its one descendant running the recorded daemon argv is the native gdaemon,
  which must own the public ports 59480 (HTTP) and 59481 (websocket). The Python backend that gdaemon supervises
  must own the backend HTTP port 59580 (`backend_ports`, offset +100) and carry `GOBBY_SUPERVISOR_FD`. In native
  mode the backend does not bind its websocket port 59581, so a listener there is refused. Any other listener under
  the wrapper is refused and named by endpoint. Each listener keeps the uid, runtime-root and managed-environment
  checks. The check returns the listener identities with their roles (`public` or `backend`), which the proof
  records as `daemon_listeners`.
- `verify_corpora.compare_manifests` compares the corpus identity (source commit, exclusions, file records and
  tree digest), not raw JSON, so the run's root and JSON layout do not fail a correct corpus. Each corpus is
  still verified file by file.
- The source pin moved from the September `7394b97c` to the 0.5.0 tip. The plan requires the
  source archived at the run's commit. If the tip moves before provisioning, the pin must be
  updated and the gcode version and contract checks will fail loudly until it is.
- The gcode pins were read from the source at `GOBBY_SHA`. The installed binary `1.9.105`,
  contract `12` agrees, but the built binary has not been run here.
- The grok-wiki and opendeepwiki live checks were dropped with the comparator narrowing. C9 remains
  in `INDEX_CASES`; this is flagged for the lead.
- `code_indexed_file_states` and its `project_id` column are assumed from the plan. Verify the
  table name against the schema before the first provision.
- `teardown` removes data as well as containers. It runs only in leaf 2.2.
- The supervised daemon launch (`gdaemon serve` with `GOBBY_BACKEND_COMMAND`), the `/api/health`
  probe on the daemon port, and the graphify help placement of `--max-workers` have not run live.
  The cause of the C0 interpreter denial is inferred from the symlink layout; the runtime root's
  violations log was not readable here. Each is confirmed only by a live run.
- `prepare_daemon.py` drops config keys the registry no longer defines. This has a test
  (`test_known_config_values_drops_unregistered_keys`), but no RED run was captured before the
  implementation. The lead has been told.
- The provision, teardown, observe-after-snapshot, service-verification and row-count paths have
  not been run against Docker or Postgres. Their pure helpers are tested: owner binding,
  step skipping and the zero-row gate.
- Amendment (orchestrator ruling, before any live step): the external-state snapshot records five
  operator-owned paths by stat only: `~/.gobby/bootstrap.yaml`, `~/.gobby/.secret_kek`,
  `~/.gobby/.secret_salt`, `~/.gobby/local_cli_token` and `~/.codex/auth.json`. Each record carries
  `exists`, `type`, `mode`, `size`, `mtime_ns` and `inode`, and no SHA-256, because the operator's
  standing rule forbids reading their bytes. A stat change between the before and after snapshots
  fails validation the way a hash change does. `test_external_snapshot_never_opens_operator_files`
  guards the read path. Every other stable path keeps its SHA-256. The isolated runtime's own
  `gobby-home/bootstrap.yaml` and `local_cli_token` are unchanged; they sit inside the runtime root.
- Graphify concurrency is set by the probe, not by an environment variable. Only `--max-workers 1`
  is passed, and only when this run's top-level or extract help lists the flag. The top-level help
  is checked because the 0.9.103 extract help does not list it. `GRAPHIFY_NATIVE_CONCURRENCY` is not
  set.
- gcode schema identity: `schema-identity --json` has a different shape from the archived six-key
  identity, so C0 records it unchecked. The six archived keys are the only identity gate.
  `schema_head.check_schema_head` projects the gdaemon identity onto those keys. A missing key fails
  closed, and extra keys are ignored.
- Project id: `gcode status --format json` flattens the indexed project into the top-level object
  (`IndexedProject` through `#[serde(flatten)]` in `crates/gcode/src/node/mod.rs`), so its `id` is
  top-level. An unindexed report carries `project_id` and no `id`. C0 compares the status `id` with
  `project.json` (`gcode-status-project-id`). The C7 poller uses the same id, so a mismatch fails C0
  closed.
- `MCPError` callers (static probe of the September C0 corpus, not live): the definition is
  `src/game_goblins/platform/mcp.py:23`. Source callers are `replenishment/sync.py:240`,
  `integrations/lightspeed.py:96`, and `cli.py:167`. Test references are in
  `tests/platform/test_mcp.py` and `tests/replenishment/test_lossless_sync.py`. C0's live
  `callers-probe` check runs `gcode callers` for each probe symbol and requires a total of at least one.
- The `uv run python <script>` form: each of the nine main-guarded scripts answers `--help` under
  `uv run --directory <worktree> python docs/...`, checked locally. No live step has run yet.
- Images: `falkordb` is pinned only to the September digest `sha256:e93fcd75...`. The local
  `falkordb:latest` (`bef8acb1`) is never used. `gobby-postgres-local` must exist on this machine. If
  it does not, `ensure_images` stops step 2 with the exact `docker pull` error.
- Grant: at the step that hits `malformed grant: grant project does not match local project`, the run
  stops and reports the exact error. The orchestrator does not retry with a hand-edited grant.
- Known live risk: September saw `malformed grant: grant project does not match local project`
  under the isolated grant. It is the likely first live failure, at step 3 or step 8.
- Reviewer readings (6f1201aa), checked against the code at this candidate:
  1. Embedding provider config key: removal confirmed statically. `ai.embeddings.provider` is no longer
     read (`embedding_keys.py`). Provider availability and auth are live questions: the embedding `api_key`
     is optional in `src/gobby/config/ai.py`, and the harness sends no key to Ollama. The live status run
     decides whether the configured provider needs one.
  2. Nomic prefix: refuted. The harness adds no nomic prefix. `query_prefix` was removed at `70ec575688`.
     The only `nomic-embed-text` string is a wrong-model fixture in `test_daemon_preparation.py`.
  3. `embedding_readiness` (`prepare_daemon.py`): confirmed call graph. Only `prepare` calls it, and only
     `launch_services.provision` calls `prepare`. `launch_daemon.launch` does not call it. Its gate is
     `daemon_health_failure`, which requires status `ok`. Not a defect under the provision order; no change.
  4. Degraded status on the startup path: open. A static read cannot settle it. The live status run decides.
  5. Health loop: confirmed bounded. 120 probes, one second apart, with a two-second timeout per probe.
     Each answered probe returns at once, so the loop takes about 120 s. When no probe answers, the worst case is 360 s. No change.
- Open, not resolved by this candidate: startup (reading 4), lease (the `malformed grant` risk above),
  and AI (the embedding provider and its key, reading 1).
- Package equality (M1): the step-4 refusal under the current pin is correct. See "Status at this candidate (M1)".
