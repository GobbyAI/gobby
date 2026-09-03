# 21670 receipt: wake latch settle after a delivered wake-clear drain (branch task-21670-wake-clear-key d47b18fdbe on 0.5.0 6a0abd99b4)
date: Thu Sep  3 07:31:31 UTC 2026
host: Darwin 25.6.0 arm64, /tmp writable, real isolated daemon + tmux/native backends for the runtime-contract cases, isolated gobby_test hub
command: DATABASE_URL="${DATABASE_URL:-postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test}" GOBBY_TEST_PROTECT=1 uv run pytest tests/events/test_wake_native_terminal.py tests/test_runner_init.py tests/terminals/ -q
```
============================= test session starts ==============================
platform darwin -- Python 3.14.3, pytest-9.0.3, pluggy-1.6.0
rootdir: /Users/josh/.gobby/worktrees/gobby/task-21670-wake-clear-key
configfile: pyproject.toml
plugins: anyio-4.12.1, mock-3.15.1, xdist-3.8.0, timeout-2.4.0, asyncio-1.3.0, cov-7.0.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collected 173 items

tests/events/test_wake_native_terminal.py ..........                     [  5%]
tests/test_runner_init.py .............................................. [ 32%]
..                                                                       [ 33%]
tests/terminals/test_backend_selection.py .                              [ 34%]
tests/terminals/test_composer.py ....                                    [ 36%]
tests/terminals/test_composition_roots.py .....                          [ 39%]
tests/terminals/test_dimensions.py ..                                    [ 40%]
tests/terminals/test_error_classification.py .........                   [ 45%]
tests/terminals/test_frame_client.py .....                               [ 48%]
tests/terminals/test_host_manager.py ................                    [ 57%]
tests/terminals/test_host_shutdown_preservation.py .                     [ 58%]
tests/terminals/test_lease_authority.py ..                               [ 59%]
tests/terminals/test_native_runtime.py ..........                        [ 65%]
tests/terminals/test_no_direct_tmux_consumers.py ......                  [ 68%]
tests/terminals/test_no_direct_tmux_spawn.py ...                         [ 70%]
tests/terminals/test_pane_io.py ........                                 [ 75%]
tests/terminals/test_runtime_contract.py ....                            [ 77%]
tests/terminals/test_runtime_registry.py ...                             [ 79%]
tests/terminals/test_sync_bridge.py ..                                   [ 80%]
tests/terminals/test_tmux_discovery.py .....                             [ 83%]
tests/terminals/test_tmux_runtime.py .......                             [ 87%]
tests/terminals/test_wire_golden.py .....                                [ 90%]
tests/terminals/test_write_coordinator.py .............                  [ 97%]
tests/terminals/test_write_outcomes.py ....                              [100%]

============================= 173 passed in 39.69s =============================
exit=0
```

Red check before the fix (sources reset to 0.5.0, tests kept): test_undelivered_composer_clear_leaves_the_earlier_wake_latched failed because the 'Settling the unresolved earlier wake' log fired before any drain key was dispatched; test_unlatched_sequence_leaves_no_entry_for_a_lost_reply failed with TypeError (run_sequence had no latch keyword). 2 failed.

Other gates on d47b18fdbe: ruff check src/ tests/ clean; ruff format --check 4039 files; bundled manifest matches HEAD; mypy src/ no issues (1940 files); test-types audit 0 new; test-quality audit 0 new; suppression ratchet 224/224.
