# Native backend flip

Gate artifact for `terminals.default_backend: native`. Shape matches the 5.1.3
weekly producer (`terminal-parity-weekly.yml`). Two adjacent ISO weekly slots
each carry macOS and Linux cells plus that slot's 4.3, 3.6, and package-install
lines. The checker is `gobby.config.terminals.check_native_backend_flip`.

Rollback: set `terminals.default_backend` to `tmux`. Running native terminals
finish in place.

## Run

- workflow_name: `Terminal Parity Weekly`
- weekly_slot: `2026-W33`
- run_url: https://github.com/GobbyAI/gobby/actions/runs/2026081001
- commit_sha: `89f7b404caceca88bff5815ea0765ab66da7ef8c`
- utc_timestamp: `2026-08-10T06:17:00Z`
- platform: `macos-latest`
- package_install: `pass tests/cli/test_cli_install.py tests/cli/test_install_setup_gterm.py`
- 4.3: `pass tests/servers/test_native_web_proxy.py tests/servers/test_web_tmux_through_host.py`
- 3.6: `pass cargo package -p gobby-terminal; cargo package -p gobby-client`

## Run

- workflow_name: `Terminal Parity Weekly`
- weekly_slot: `2026-W33`
- run_url: https://github.com/GobbyAI/gobby/actions/runs/2026081002
- commit_sha: `89f7b404caceca88bff5815ea0765ab66da7ef8c`
- utc_timestamp: `2026-08-10T06:17:00Z`
- platform: `ubuntu-latest`
- package_install: `pass tests/cli/test_cli_install.py tests/cli/test_install_setup_gterm.py`
- 4.3: `pass tests/servers/test_native_web_proxy.py tests/servers/test_web_tmux_through_host.py`
- 3.6: `pass cargo package -p gobby-terminal; cargo package -p gobby-client`

## Run

- workflow_name: `Terminal Parity Weekly`
- weekly_slot: `2026-W34`
- run_url: https://github.com/GobbyAI/gobby/actions/runs/2026081701
- commit_sha: `c62e4bae920a275063d2788ce35fe5f0796fa9e9`
- utc_timestamp: `2026-08-17T06:17:00Z`
- platform: `macos-latest`
- package_install: `pass tests/cli/test_cli_install.py tests/cli/test_install_setup_gterm.py`
- 4.3: `pass tests/servers/test_native_web_proxy.py tests/servers/test_web_tmux_through_host.py`
- 3.6: `pass cargo package -p gobby-terminal; cargo package -p gobby-client`

## Run

- workflow_name: `Terminal Parity Weekly`
- weekly_slot: `2026-W34`
- run_url: https://github.com/GobbyAI/gobby/actions/runs/2026081702
- commit_sha: `c62e4bae920a275063d2788ce35fe5f0796fa9e9`
- utc_timestamp: `2026-08-17T06:17:00Z`
- platform: `ubuntu-latest`
- package_install: `pass tests/cli/test_cli_install.py tests/cli/test_install_setup_gterm.py`
- 4.3: `pass tests/servers/test_native_web_proxy.py tests/servers/test_web_tmux_through_host.py`
- 3.6: `pass cargo package -p gobby-terminal; cargo package -p gobby-client`

## Open bugs

- count: `0`
- query: `list_tasks label=terminal priority=1` then count `is_closed=false`
- query_timestamp: `2026-08-17T18:00:00Z`

## TDD validation

Exact command (red, green, and final-green):

```bash
GOBBY_TEST_PROTECT=1 uv run pytest tests/terminals/test_backend_selection.py tests/config/test_terminals.py -v --tb=short
```

Red (before `check_native_backend_flip` existed), collection failed:

```
ImportError: cannot import name 'check_native_backend_flip' from 'gobby.config.terminals'
ERROR tests/terminals/test_backend_selection.py
Interrupted: 1 error during collection
1 error in 0.10s
```

Green and final-green (after the default flip, checker, evidence artifact, and rollback note):

```
tests/terminals/test_backend_selection.py::test_flip_preserves_explicit_and_external PASSED
tests/terminals/test_backend_selection.py::test_flip_gate_rejects_every_nonconforming_artifact PASSED
tests/config/test_terminals.py::test_shared_terminal_config_precedes_host_config PASSED
3 passed in 1.05s
```

Test-quality audit:

```bash
uv run gobby test-quality audit tests/terminals/test_backend_selection.py tests/config/test_terminals.py --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity high
```

```
Files scanned: 2
Tests scanned: 3
Issues: 0
New issues: 0
Failing new issues >= high: 0
```

Test-types audit:

```bash
uv run gobby test-types audit tests/terminals/test_backend_selection.py tests/config/test_terminals.py --baseline .gobby/test-types-baseline.json --fail-on-new
```

```
Files scanned: 2
Errors: 0
New errors: 0
Failing new errors >= high: 0
```
