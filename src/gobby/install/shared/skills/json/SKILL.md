---
name: json
description: "Enforces default JSON coding standards for agents writing or refactoring JSON and JSON-adjacent configuration: parser-aware syntax, schema validation, serialization boundaries, tooling, testing, and secret handling. Use before editing JSON unless the repo provides stricter local rules."
version: "1.1.0"
category: development
triggers: json, jsonc, json5, json-schema, package-json, jq, prettier, biome, eslint, tsconfig
sources:
  - "Primary: RFC 8259 and ECMA-404 JSON syntax/data model, adapted for Gobby's bundled language-skill layout."
  - "Secondary: JSON Schema documentation and common formatter/parser behavior for JSON, JSONC, and JSON5 configuration files."
---

# JSON

Identify the consuming parser, dialect, schema, and generated-file policy before editing.

## Tooling

- Use the repository formatter or linter, strict parser, applicable schema, and
  focused consumer validation.
- Preserve lockfiles and generated artifacts through their owning tool.

## Configuration

- Distinguish strict JSON from JSONC, JSON5, JSON Lines, manifests, and tool-specific
  files before using comments, trailing commas, or duplicate-key behavior.
- Diagnostic hook: treat parser and schema errors as consumer-contract evidence;
  avoid loosening schemas or coercing values before identifying the expected shape.

For dialects, tools, schemas, and generated files:
`get_skill_file(name="json", path="references/configuration.md")`

## Syntax And Data Model

- Preserve object/array shape, key spelling, ordering where a consumer observes it,
  number precision, Unicode, and null-versus-missing semantics.
- Serialize non-JSON values such as dates, bytes, decimals, and large integers through
  an explicit wire convention.

For syntax and data-model boundaries:
`get_skill_file(name="json", path="references/syntax-and-data-model.md")`

## Schema And Validation

- Validate against the schema version and entry point used by the consumer.
- Update dependent fixtures, examples, generated types, and migrations with schema changes.

For JSON Schema and migration patterns:
`get_skill_file(name="json", path="references/schema-and-validation.md")`

## Parsing And Serialization

- Parse untrusted input with bounded size/depth and reject duplicate keys when they
  would create ambiguity or security risk.
- Keep canonicalization, escaping, and round-trip expectations explicit.

For parser and serializer choices:
`get_skill_file(name="json", path="references/parsing-and-serialization.md")`

## Security And Secrets

- Keep credentials and sensitive endpoints in the repository's reference or secret
  mechanism, and review prototype-pollution or signature-canonicalization boundaries.

For secret handling and parser threats:
`get_skill_file(name="json", path="references/security-and-secrets.md")`

## Testing

- Exercise the actual consumer with representative valid, invalid, boundary, and
  round-trip fixtures.
- Include generated-output or lockfile checks when those files are the contract.

For fixture and validation selection:
`get_skill_file(name="json", path="references/testing.md")`
