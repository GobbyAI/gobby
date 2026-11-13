# CI failures at the port's cut

What was already red on 0.9.0 before any port work, so a chunk session can tell an old failure from
one it caused. Ruling 25 (docs/port/rulings.md) says CI does not gate the port until its final
validation; this file is the reference for that validation and for found work.

Runs read: 0.9.0 at 68d0acbc2e, pushed 2026-11-12 21:35 CT. That commit differs from the cut SHA
(0.5.0 at 0256724f1f) only in CI triggers, AGENTS.md, docs/port, scripts/port and the project policy.

| Workflow | Run | Result |
| --- | --- | --- |
| CI (.github/workflows/ci.yml) | 43360002167 | failed |
| Rust CI (.github/workflows/rust-ci.yml) | 43360002310 | failed |
| Release Branch CI (advisory) | 43360002175 | failed |

The runs at the cut SHA itself (43309907685, 43309907157, 43309907083) also failed and were not read.
CI's run on 9014381151 passed only because that commit changed documents, so the test jobs were skipped.
GitHub keeps run logs for a limited time; the failure lines below were copied from them on 2026-11-13.

The "Reading" column is the map author's reading from the log line alone. Only the rows marked
"checked" were confirmed against the source.

## CI

| Job | What failed | Message | Reading |
| --- | --- | --- | --- |
| Test (Python 3.13), Test (Python 3.14) | Collection of tests/reports/test_publishing_retirement.py | `ImportError: cannot import name 'RETIRED_SYSTEM_CRON_JOBS' from 'gobby.runner_init.orchestration'` | Bad test, checked: the name lives in src/gobby/runner_init/cron.py:18. |
| Test (Python 3.13), Test (Python 3.14) | The whole suite | `collected 59490 items / 1 error`, then `Interrupted: 1 error during collection` | No Python test ran. CI says nothing about any Python test at the cut. |
| Type Check (test types audit) | tests/reports/test_publishing_retirement.py:12 | `Module "gobby.runner_init.orchestration" has no attribute "RETIRED_SYSTEM_CRON_JOBS"` | Same bad import. |
| Type Check (test types audit) | tests/events/test_wake_unread_dedup.py:103, 194, 235, 288 and tests/sessions/test_mailbox.py:330 | `Argument "tmux_sender" to "WakeDispatcher" has incompatible type ...; expected "TmuxSender \| None"` | Test typing. TMUX-PURGE removes this argument. |
| Type Check (test types audit) | tests/hooks/test_session_start_handlers.py:21 | `Module "gobby.hooks.event_handlers._session_start.flow" does not explicitly export attribute "_log_session_start_timing"` | Bad test import, checked: the function is defined in `_session_start/diagnostics.py:33`. |
| Terminal Acceptance (macos-latest, ubuntu-latest) | tests/terminals/acceptance/test_native_writes.py: test_send_keys_conflicting_payload_is_refused, test_send_keys_replay_returns_the_recorded_outcome, test_send_keys_is_delivered_under_another_holder_s_lease, test_lease_takeover_settles_the_displaced_write | `gobby.config.bootstrap.BootstrapConfigError: files_home is required for datastore_mode: local` | Test setup: the fixture's bootstrap has no files_home. Not read further. |
| Terminal Acceptance (macos-latest, ubuntu-latest) | tests/terminals/acceptance/test_control_recovery.py::test_control_interruption_recovers_multiple_panes_without_alert_burst | `AssertionError: assert 0 == 1` | Unknown. Could be behavior. |
| Terminal Acceptance (macos-latest only) | tests/terminals/acceptance/test_tmux_parity.py::test_write_and_snapshot_parity[native] | `gobby.terminals.host_client.HostCommandError: capture_invalidated` | Unknown. Passed on Ubuntu. TMUX-PURGE touches this file. |
| Build Package | tests/packaging/test_installed_wheel_ui_smoke.py::test_installed_wheel_serves_packaged_index_html | `gobby.runner exited early with 1`, `Fatal error: native deployment runtime authority is unavailable` | Unknown. The installed wheel's runner refuses to start in the CI environment. |
| Security Scan (uv audit) | virtualenv 21.14.12 | GHSA-cx6f-vjhg-g3cc, fixed in 21.14.13 | Dependency advisory; a lockfile bump clears it. |
| Merge Gate | Gated jobs | Fails because Type Check and the jobs above failed | Follows from the rows above. |

Totals: Terminal Acceptance 6 failed and 11 passed on macOS, 5 failed and 12 passed on Ubuntu.

## Rust CI

| Job | What failed | Message | Reading |
| --- | --- | --- | --- |
| Check & Test, step Test (gcode) | 44 gcode unit tests, by module: commands::graph::tests 8, commands::scope::tests 8, commands::graph::view::communities::tests 6, projection::sync::tests 4, search::graph_boost::tests 4, freshness::tests::serial_db 3, vector::code_symbols::search::tests 3, and one each in commands::graph::view::tests, commands::search::tests, commands::vector::tests, dispatch::tests, graph::code_graph::tests, graph::report::tests, index_lock::tests, vector::code_symbols::tests::collection | every one panics with `read machine id: Missing` | Test setup: the runner has no machine id. One cause for all 44. |
| PostgreSQL-backed Rust tests | crates/gcode/tests/projection_stale.rs, serial_db::vector_sync_file_allows_deleted_local_file_state (panic at line 121) | `bind managed principal to the fixture project: "seed managed principal binding: db error"` | Unknown. The other two tests in that file passed. |
| Coverage (report only) | The job | Its log was not available | Not read. |

## Release Branch CI

The pre-push suite step reported `Cargo tests failed` and `Pytest failed`. It was not broken down
further; it runs the same suites as the two workflows above.
