# C# Data And Serialization

Keep persistence, migrations, and wire contracts explicit.

## Entity Framework Core

- Keep DbContext lifetime scoped to the unit of work.
- Use provider-backed tests for query translation, constraints, transactions,
  and concurrency behavior.
- Review generated migrations before committing them. Preserve migration order
  and avoid hand-editing snapshots unless the migration requires it.
- Use concurrency tokens and transactions intentionally.
- Avoid lazy-loading surprises in APIs; shape queries with includes/projections.

## SQL And Dapper

- Parameterize every query. Never concatenate untrusted values into SQL.
- Keep SQL near the adapter/repository that owns the persistence concern.
- Test query shape, null handling, transactions, and database-specific behavior.
- Use explicit column lists for contracts that external reports or APIs depend
  on.

## Serialization

- Treat JSON, protobuf, XML, CSV, and message schemas as compatibility contracts.
- Prefer `System.Text.Json` defaults already configured by the repo.
- Preserve property names, required fields, enum encoding, date/time formats,
  and null-handling for external clients.
- Use DTOs at wire boundaries. Map to domain types after validation.

## Validation And Mapping

- Validate untrusted payloads before persistence or domain conversion.
- Keep mapping code explicit enough to review. Source generators are fine when
  the repo already uses them and generated output is deterministic.
- Do not let EF entities, API DTOs, and domain models collapse into one type
  when their invariants differ.

## Operational Safety

- Check indexes, query plans, batching, and pagination for large reads.
- Make migrations reversible or document why rollback requires a forward fix.
- Avoid data backfills inside request paths.
