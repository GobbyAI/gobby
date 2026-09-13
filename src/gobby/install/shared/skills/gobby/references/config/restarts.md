# Activation and restarts

Load when desired and active settings diverge, a save reports failures, or a
configuration change needs process/client refresh. Read the public schema's
activation metadata and a fresh `get_config_values` snapshot first.

Live keys reconcile in-process. Restart-required keys remain in
`pending_restart_keys` until startup activates the desired configuration.
Managed keys use their lifecycle action. A successful commit may have failed
live activation; inspect `failed_live_keys` and daemon diagnostics before deciding
whether restart is the right recovery. Do not announce activation from commit
success alone.

Bootstrap YAML controls pre-database startup wiring and is not a runtime override
document. `~/.gobby/config.yaml` is an export/import artifact. Project JSON and
build defaults have their own consumers; a daemon restart is not a generic way
to apply every configuration source.

The operator CLI's global `--config PATH` selects bootstrap input for commands
that open the hub. It does not replace revisioned runtime values. A managed
execution grant supplies its own connection binding.

Restart is an operator lifecycle action with effects on active sessions. Follow
the applicable administration/session coordination guidance and existing user
authorization. Start from the main checkout; a linked-worktree daemon start is
normally refused. A protected cron run can defer stop/restart with `--wait`;
`--force` interrupts it and needs authorization for that impact.

Provider hook settings are installed client artifacts. If their configured
deadline changes, refresh the affected provider installation and inspect its
documented exceptions. Runtime activation does not rewrite those files.
After recovery, read values and health again and verify the intended key is active.
See [runtime contract](../../../../../../../../docs/guides/configuration.md#reactive-runtime-configuration-contract)
and [hook deadlines](../../../../../../../../docs/guides/configuration.md#tasks-and-workflows).
