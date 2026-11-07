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
| Compose project | `gcode-graphify-rerun-2026-11` |
| Source commit (gobby, gcode) | `30d2a5956241c208e670133efbc9d9452721118f`, the 0.5.0 tip at candidate time |
| gcode pins | version `1.9.104`, contract `12`; read from `crates/gcode` at the source commit and matching the installed binary |
| Graphify | PyPI `graphifyy==0.9.103` (version-only pin, `GRAPHIFY_PACKAGE` in `harness/launch_services.py`); recorded by version, executable SHA-256 and lock |
| Graphify wheel | `graphifyy-0.9.103-py3-none-any.whl`, SHA-256 `7948c030f8b04153d88bc1ee23a485ddaa0c2d6aaa03a748c177e3c217bcfe34` (PyPI, latest release at review time) |
| Database and account | `gobby_bakeoff_22948`; account email `bakeoff-22948@example.invalid` (reserved placeholder) |

## Order of live steps (orchestrator)

1. `provision_environment.py init --owner-session <session>`: creates the runtime root, archives
   the sources at `GOBBY_SHA`, copies the templates and corpora, and writes the manifests.
   It refuses an existing root.
2. `provision_environment.py write-compose --owner-session <session> ...`: renders the compose file.
3. `launch_services.py provision --owner-session <session> --attempt <id>`:
   - writes `isolation/external-state.before.json` once;
   - renders Compose, checks names and ports, and starts the three services, or verifies the
     existing services receipt and skips the start;
   - builds `tools/graphify/venv` (`graphifyy==0.9.103`) and `tools/gobby/venv` (`uv sync --frozen`
     from `sources/gobby`), each through a probe receipt;
   - runs `prepare_daemon.py` and `prepare_identity.py`;
   - runs `gobby init --name <case>` in each corpus workspace with the daemon ports bound by
     nothing, through probe receipts;
   - asserts zero `code_indexed_file_states` rows for every workspace project.
4. `launch_daemon.py --owner-session <session> --attempt <id>`, under the SRT sandbox.
5. `record_installations.py --owner-session <session> --attempt <id>`.
6. `validate_environment.py --live`, then the C0 run.
7. `launch_services.py observe --owner-session <session>` writes the after-snapshot if absent.
8. `launch_services.py teardown --owner-session <session>` (leaf 2.2): writes the final snapshot,
   then removes the containers, network and volumes listed in `receipts/ownership-live.json`,
   after checking each one's identity and compose label. It writes `receipts/teardown.json`.

Every step writes a receipt under `receipts/` or `logs/install/`. A probe whose receipt exists and
verifies is skipped on rerun. A failed probe is preserved, and a new attempt ID is required.

## Receipts

- `isolation/external-state.before.json`, `.after.json`, `.final.json`
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
