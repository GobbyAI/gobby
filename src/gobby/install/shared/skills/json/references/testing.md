# JSON Testing

## Syntax And Formatting

- Run the repo's configured formatter or linter for the changed JSON files.
- Parse every changed JSON file with the same parser dialect used by its
  consumer.
- Include generated-output checks when a source file or generator owns the JSON.

## Schema And Contract Tests

- Validate changed documents against JSON Schema, OpenAPI, package metadata
  validators, tool config loaders, or app-specific config models.
- Add fixtures for required fields, optional fields, defaults, enum changes,
  unknown keys, null handling, and numeric precision when those behaviors matter.
- Keep golden files stable and review diffs for unintended ordering or
  formatting churn.

## Consumer Tests

- Test the code path that reads the JSON: app startup, CLI config loading,
  package-manager behavior, API schema loading, fixture ingestion, or migration
  import.
- For package metadata or lockfiles, run the package-manager check command used
  by the repo instead of editing integrity data by hand.
- For schema changes, run both positive and negative validation cases.

## Narrow Commands

- Prefer focused commands such as a specific config loader test, schema
  validation command, package-manager check, or fixture test file.
- Avoid broad repository-wide commands unless the JSON change affects shared
  contracts and the repo expects broad validation.
