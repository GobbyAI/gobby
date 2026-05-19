# PostgreSQL Native pg_search from Source

For non-Debian Linux distributions, Gobby does not run a native `pg_search`
installer. Use Docker mode unless you already maintain a local PostgreSQL 17
extension toolchain.

Recommended path:

```bash
gobby postgres install --mode docker
```

Manual path:

1. Install PostgreSQL 17 and development headers for your distribution.
2. Build `pg_search` from upstream ParadeDB sources against the same PostgreSQL
   installation.
3. Create a dedicated Gobby database.
4. Install and verify the extension in that database:

```sql
CREATE EXTENSION pg_search;
SELECT 1 FROM pg_extension WHERE extname = 'pg_search';
```

Then register the database as an external install:

```bash
gobby postgres install --mode external --dsn postgresql://USER:PASSWORD@HOST:PORT/gobby
```
