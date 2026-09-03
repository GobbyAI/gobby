# 21671 receipt: test_daemon_restart_continuity on 0.5.0 d4ac668f6c
date: Thu Sep  3 06:48:48 UTC 2026
host: Darwin 25.6.0 arm64, /tmp writable, real isolated daemon + tmux/native backends
command: DATABASE_URL="${DATABASE_URL:-postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test}" GOBBY_TEST_PROTECT=1 uv run pytest "tests/terminals/test_runtime_contract.py::test_daemon_restart_continuity" -q
```
============================= test session starts ==============================
platform darwin -- Python 3.14.3, pytest-9.0.3, pluggy-1.6.0
rootdir: /Users/josh/Projects/gobby
configfile: pyproject.toml
plugins: anyio-4.12.1, mock-3.15.1, xdist-3.8.0, timeout-2.4.0, asyncio-1.3.0, cov-7.0.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collected 2 items

tests/terminals/test_runtime_contract.py ..                              [100%]

============================== 2 passed in 18.72s ==============================
exit=0
```
