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
