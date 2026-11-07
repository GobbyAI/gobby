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
| Source commit (gobby, gcode) | `30d2a5956241c208e670133efbc9d9452721118f`, the 0.5.0 tip at candidate time |
| gcode pins | version read from `sources/gobby/crates/gcode/Cargo.toml` `package.version` (archived at `GOBBY_SHA`; `1.9.104` at candidate time), frozen into the command manifest and checked against `gcode --version` at C0; contract `12` |
| Graphify | PyPI `graphifyy==0.9.103` (version-only pin, `GRAPHIFY_PACKAGE` in `harness/launch_services.py`); recorded by version, executable SHA-256 and lock |
| Graphify wheel | `graphifyy-0.9.103-py3-none-any.whl`, SHA-256 `7948c030f8b04153d88bc1ee23a485ddaa0c2d6aaa03a748c177e3c217bcfe34` (PyPI, latest release at review time) |
| Database and account | `gobby_bakeoff_22948`; account email `bakeoff-22948@example.invalid` (reserved placeholder) |

## Order of live steps (orchestrator)

Pre-steps, in this order, before step 1. They clear the stopped attempt `rerun2611a`, whose root is kept as evidence.

0. Teardown: `uv run python docs/evidence/gcode-graphify-rerun-2026-11/harness/launch_services.py teardown --owner-session 7674aa11-70a3-40d5-91dd-1fe46140732b`. Then the lead records the evidence manifest of the `rerun2611a` root.
0b. Move: `mv "/Users/josh/.gobby/agents_workspaces/d45545c5-ded5-4335-b115-0245752edacf/epics/epic-22948/build/gcode-graphify-rerun-2026-11" "/Users/josh/.gobby/agents_workspaces/d45545c5-ded5-4335-b115-0245752edacf/epics/epic-22948/build/gcode-graphify-rerun-2026-11-rerun2611a-evidence"`. The root is `DEFAULT_RUNTIME_ROOT`. `init` refuses an existing root (`init_runtime` raises `FileExistsError`), and `launch_services` always uses `DEFAULT_RUNTIME_ROOT`, so the old root has to move aside. Nobody deletes it.

1. `provision_environment.py init --owner-session <session>`: creates the runtime root, archives
   the sources at `GOBBY_SHA`, copies the templates and corpora, and writes the manifests.
   It refuses an existing root.
2. `provision_environment.py write-compose --owner-session <session> ...`: refuses any reference that is not
   digest-pinned or that names `:latest`, renders `config/compose.yaml` and checks the rendered text for
   `:latest`, and records `receipts/image-references.json`. Then it runs `docker image inspect` on each
   reference and `docker pull` only for the references that are missing. A failed pull stops the step
   with `docker pull failed for <service> image <reference>: <stderr>`.
3. `launch_services.py provision --owner-session <session> --attempt <id>`:
   - writes `isolation/external-state.before.json` once;
   - renders Compose, checks names and ports, and starts the three services, or verifies the
     existing services receipt and skips the start;
   - builds `tools/graphify/venv` (`graphifyy==0.9.103`) and `tools/gobby/venv` (`uv sync --frozen`
     from `sources/gobby`), each through a probe receipt;
   - runs `prepare_daemon.py` and `prepare_identity.py`. Each checks the runtime's `gobby` in a subprocess under `tools/gobby/venv/bin/python` (the gobby-sync environment), because the harness interpreter imports the epic tree;
   - runs `gobby init --name <case>` in each corpus workspace with the daemon ports bound by
     nothing, through probe receipts;
   - asserts zero `code_indexed_file_states` rows for every workspace project.
4. `launch_daemon.py --owner-session <session> --attempt <id>`, under the SRT sandbox.
5. `record_installations.py --owner-session <session> --attempt <id>`.
6. `launch_services.py observe --owner-session <session> --attempt <id>` writes the after-snapshot
   if absent.
7. `c0.py <attempt> --owner-session <session>`: the C0 run, described below.
8. `validate_environment.py --live`.

This is the order ruled by the orchestrator (7d30ebfd). Validation runs last, after C0.

Teardown is leaf 2.2, not one of these eight steps. `launch_services.py teardown --owner-session
<session>` writes the final snapshot, then removes the containers, network and volumes listed in
`receipts/ownership-live.json`, after checking each one's identity and compose label. It writes
`receipts/teardown.json`.

Every step writes a receipt under `receipts/` or `logs/install/`. A probe whose receipt exists and
verifies is skipped on rerun. A failed probe is preserved, and a new attempt ID is required.

## Command lines for attempt `rerun2611b`

Run from the worktree root, as the orchestrator. Every line uses the same owner session.

```bash
uv run python docs/evidence/gcode-graphify-rerun-2026-11/harness/provision_environment.py init --owner-session 7674aa11-70a3-40d5-91dd-1fe46140732b
uv run python docs/evidence/gcode-graphify-rerun-2026-11/harness/provision_environment.py write-compose --owner-session 7674aa11-70a3-40d5-91dd-1fe46140732b --postgres-image gobby-postgres-local@sha256:443364cb8e901fcbf902ee0435e9fc2333cb000d7351e22bd78485365e4d3d02 --qdrant-image qdrant/qdrant@sha256:057ee3a8da769fe7310dd3537b4dc7583bf87a95ce8ac43c0af5a46bc580d1fc --falkordb-image falkordb/falkordb@sha256:e93fcd753fe612fb0a222166a0620a1ae31b826a12f223c3b6d06038d9d7a364
uv run python docs/evidence/gcode-graphify-rerun-2026-11/harness/launch_services.py provision --owner-session 7674aa11-70a3-40d5-91dd-1fe46140732b --attempt rerun2611b
uv run python docs/evidence/gcode-graphify-rerun-2026-11/harness/launch_daemon.py --owner-session 7674aa11-70a3-40d5-91dd-1fe46140732b --attempt rerun2611b
uv run python docs/evidence/gcode-graphify-rerun-2026-11/harness/record_installations.py --owner-session 7674aa11-70a3-40d5-91dd-1fe46140732b --attempt rerun2611b
uv run python docs/evidence/gcode-graphify-rerun-2026-11/harness/launch_services.py observe --owner-session 7674aa11-70a3-40d5-91dd-1fe46140732b --attempt rerun2611b
uv run python docs/evidence/gcode-graphify-rerun-2026-11/harness/c0.py rerun2611b --owner-session 7674aa11-70a3-40d5-91dd-1fe46140732b
uv run python docs/evidence/gcode-graphify-rerun-2026-11/harness/validate_environment.py --live
```

The write-compose images are the September digests from `receipts/image-references.json`
(recorded 2026-09-07). `gobby-postgres-local` is a locally built image name, so it must exist on
this machine before step 2. Otherwise the orchestrator picks new digests.

## C0 (step 7)

`c0.py` checks ownership of the runtime, then runs every command under the SRT wrapper. Its policy is
`srt/c0-settings-<attempt>.json`, and violations go to `logs/c0/<attempt>/violations.jsonl`.

1. **Graphify concurrency probe.** `graphify --help` and `graphify extract --help` are captured under
   `logs/c0/<attempt>/probe/`. If the extract help lists `--max-workers`, the extract command gets
   `--max-workers 1`. The value and its evidence come from September's `run-records.jsonl`
   line 3, where `GRAPHIFY_NATIVE_CONCURRENCY` is `"1"` and `command_argv` contains
   `--max-workers 1`. If the flag is absent, the receipt records `source: unsupported` and the
   extract runs with default workers. This run sets the flag only. `GRAPHIFY_NATIVE_CONCURRENCY`
   is not set (orchestrator ruling 4b, via gobby#19309).
2. **Frozen manifest.** `command-manifest.json` lists each exact argv and the gcode pin, which is the
   archived crate version. The first gcode command is the cold index,
   `gcode --project <C0> --format json index <C0> --full --sync-projections`.
3. **C7 poller.** The cold index runs under `IndexPoller`. It samples
   `SELECT count(*) FROM code_indexed_file_states WHERE project_id = %s` every 0.2 s over a
   read-only connection, and logs to `receipts/c7-poller-<attempt>.jsonl`. The project id is read
   from `corpora/gcode/C0/.gobby/project.json`, as `launch_services` reads it. The clean count is
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

- The source pin moved from the September `7394b97c` to the 0.5.0 tip. The plan requires the
  source archived at the run's commit. If the tip moves before provisioning, the pin must be
  updated and the gcode version and contract checks will fail loudly until it is.
- The gcode pins were read from the source at `GOBBY_SHA`. The installed binary `1.9.104`,
  contract `12` agrees, but the built binary has not been run here.
- The grok-wiki and opendeepwiki live checks were dropped with the comparator narrowing. C9 remains
  in `INDEX_CASES`; this is flagged for the lead.
- `code_indexed_file_states` and its `project_id` column are assumed from the plan. Verify the
  table name against the schema before the first provision.
- `teardown` removes data as well as containers. It runs only in leaf 2.2.
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
  is passed, and only when this run's `extract --help` lists the flag. `GRAPHIFY_NATIVE_CONCURRENCY`
  is not set.
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
