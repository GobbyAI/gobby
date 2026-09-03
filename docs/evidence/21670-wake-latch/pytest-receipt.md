# 21670 receipt: terminal, wake, and runner_init suites on 0.5.0 d4ac668f6c
date: Thu Sep  3 06:49:25 UTC 2026
host: Darwin 25.6.0 arm64, /tmp writable, real isolated daemon + tmux/native backends for runtime-contract cases
command: DATABASE_URL="${DATABASE_URL:-postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test}" GOBBY_TEST_PROTECT=1 uv run pytest tests/events/test_wake_native_terminal.py tests/test_runner_init.py tests/terminals/ -q
```
============================= test session starts ==============================
platform darwin -- Python 3.14.3, pytest-9.0.3, pluggy-1.6.0
rootdir: /Users/josh/Projects/gobby
configfile: pyproject.toml
plugins: anyio-4.12.1, mock-3.15.1, xdist-3.8.0, timeout-2.4.0, asyncio-1.3.0, cov-7.0.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collected 170 items

tests/events/test_wake_native_terminal.py .........                      [  5%]
tests/test_runner_init.py .............................................. [ 32%]
..                                                                       [ 33%]
tests/terminals/test_backend_selection.py .                              [ 34%]
tests/terminals/test_composer.py ....                                    [ 36%]
tests/terminals/test_composition_roots.py .....                          [ 39%]
tests/terminals/test_dimensions.py ..                                    [ 40%]
tests/terminals/test_error_classification.py .........                   [ 45%]
tests/terminals/test_frame_client.py .....                               [ 48%]
tests/terminals/test_host_manager.py ................                    [ 58%]
tests/terminals/test_host_shutdown_preservation.py .                     [ 58%]
tests/terminals/test_lease_authority.py ..                               [ 60%]
tests/terminals/test_native_runtime.py ..........                        [ 65%]
tests/terminals/test_no_direct_tmux_consumers.py ......                  [ 69%]
tests/terminals/test_no_direct_tmux_spawn.py ...                         [ 71%]
tests/terminals/test_pane_io.py ........                                 [ 75%]
tests/terminals/test_runtime_contract.py ....                            [ 78%]
tests/terminals/test_runtime_registry.py ...                             [ 80%]
tests/terminals/test_sync_bridge.py ..                                   [ 81%]
tests/terminals/test_tmux_discovery.py .....                             [ 84%]
tests/terminals/test_tmux_runtime.py .......                             [ 88%]
tests/terminals/test_wire_golden.py .....                                [ 91%]
tests/terminals/test_write_coordinator.py ...........                    [ 97%]
tests/terminals/test_write_outcomes.py ....                              [100%]

======================== 170 passed in 83.10s (0:01:23) ========================
exit=0
```
