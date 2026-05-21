# PostgreSQL 18 Docker Upgrade

Use this runbook for internal Docker-mode installs moving from the bundled
PostgreSQL 17 image to PostgreSQL 18.

```bash
uv run gobby stop
uv run gobby postgres backup
uv run gobby postgres uninstall --remove-data
uv run gobby postgres install --mode docker
uv run gobby postgres restore ~/.gobby/backups/postgres/<timestamp>
uv run gobby postgres status
uv run gobby start
uv run gobby status
```

Do not run `uninstall --remove-data` until `gobby postgres backup` prints a
verified backup directory containing `gobby.dump`, `metadata.json`, and
`SHA256SUMS`.
