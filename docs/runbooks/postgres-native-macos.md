# PostgreSQL Native pg_search on macOS

Gobby does not install `pg_search` natively on macOS because upstream does not
publish a supported macOS package for the PostgreSQL extension.

Recommended path:

```bash
gobby postgres install --mode docker
```

Manual path:

1. Install PostgreSQL 18 with your preferred package manager.
2. Build `pg_search` from upstream ParadeDB sources for that PostgreSQL
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
