# JSON Schema And Validation

## Validate The Consumed Contract

- Syntax validity is necessary but not enough. Validate the schema or domain
  contract used by the target system.
- Check `$schema`, `$id`, `$defs`, `$ref`, `required`, `properties`,
  `additionalProperties`, `oneOf`, `anyOf`, `allOf`, `enum`, `const`, `format`,
  and default semantics before editing a schema.
- Match the draft supported by the validator. Draft differences can change
  reference resolution, unevaluated properties, formats, and annotation behavior.

## Schema Changes

- When a schema changes, update example documents, fixtures, generated clients,
  validators, docs, and migration data together.
- Add backward-compatibility tests for optional fields, removed fields, renamed
  fields, and enum expansion.
- Keep strictness intentional. Widening `additionalProperties`, changing
  required fields, or loosening enums can hide bugs in downstream consumers.

## OpenAPI And Platform Schemas

- For OpenAPI, validate both JSON Schema-like shape and OpenAPI-specific rules.
- For package metadata or tool config, prefer official validators or the tool's
  own config loader over hand-written checks.
- For app config, use the same runtime validation path the application uses at
  startup or request time.

## Error Reporting

- Keep validation failures actionable: include paths, field names, expected
  types, and accepted values.
- Avoid suppressing unknown-key errors unless the consumer really supports
  extension fields.
