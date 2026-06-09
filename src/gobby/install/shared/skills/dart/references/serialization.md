# Dart Serialization And Code Generation

Use this reference when editing JSON, generated models, routes, API clients,
local storage, database adapters, or build-runner surfaces.

## Generated Ownership

- Determine whether generated files are committed or ignored before changing
  annotations or source models.
- Regenerate with the repo's command when builders own output.
- Do not hand-edit `.g.dart`, `.freezed.dart`, generated routes, generated API
  clients, localization output, or database adapters unless the repo explicitly
  documents that ownership.

## JSON And Wire Formats

- Validate external JSON before trusting it.
- Keep wire DTOs separate from domain models when the API can be incomplete,
  versioned, nullable, or incompatible with domain invariants.
- Preserve unknown fields or migration paths when backward compatibility matters.
- Test required, optional, null, malformed, and versioned payloads.

## Builders And Annotations

- Keep `part` directives, annotations, builder options, and generated filenames
  in sync.
- Check `build.yaml` before changing builder behavior.
- Avoid broad rebuilds when a targeted generation command is available.

## Storage And APIs

- Treat Drift, Isar, Hive, shared preferences, secure storage, GraphQL, REST,
  and platform channel schemas as external boundaries.
- Test migrations and adapters when persisted or generated shape changes.
