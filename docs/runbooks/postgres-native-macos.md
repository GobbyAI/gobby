# PostgreSQL Native pg_search on macOS

This runbook is retained for historical context only. Gobby PostgreSQL setup is
Docker-only; native and external PostgreSQL install modes are no longer
supported.

Supported path:

```bash
gobby postgres install --mode docker
```

If the Docker image is missing `pg_search`, rebuild it with
`gobby postgres install --mode docker`.
