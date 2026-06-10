---
name: json
description: "Enforces default JSON coding standards for agents writing or refactoring JSON and JSON-adjacent configuration: parser-aware syntax, schema validation, serialization boundaries, tooling, testing, and secret handling. Use before editing JSON unless the repo provides stricter local rules."
version: "1.0.0"
category: development
triggers: json, jsonc, json5, json-schema, package-json, jq, prettier, biome, eslint, tsconfig
sources:
  - "Primary: RFC 8259 and ECMA-404 JSON syntax/data model, adapted for Gobby's bundled language-skill layout."
  - "Secondary: JSON Schema documentation and common formatter/parser behavior for JSON, JSONC, and JSON5 configuration files."
---

# JSON

Default coding standards for JSON and JSON-adjacent configuration. Repo
conventions and configured tooling take precedence. If JSON Schema, OpenAPI,
package-manager lockfiles, `package.json`, `tsconfig.json`, `biome.json`,
`.eslintrc`, `.prettierrc`, generated fixtures, or platform docs are stricter,
follow the repo.

## Tooling

Run the repo's configured format, lint, schema validation, parser checks, and
focused tests before finishing. If none are configured, use the local JSON
context:

- Format/lint: prettier, biome, jq, eslint config validators, editorconfig, or
  repo wrappers
- Parse/schema: JSON parser round-trip, JSON Schema, OpenAPI, package-manager,
  TypeScript config, app config, or domain-specific validators
- Consumer checks: package manager install checks, CLI config validation,
  fixture tests, schema conformance, or application tests that load the JSON
- Tests: focused tests for the system that consumes the changed JSON
- Packages: preserve lockfiles, generated JSON, checked-in schemas, snapshot
  fixtures, pinned tool versions, and file ordering conventions

Do not treat JSON as plain text. Do not relax schemas, add comments/trailing
commas to strict JSON, reorder generated files casually, inline secrets, or
silence parse errors to make a quick edit pass.

## Configuration

- Identify the owning consumer before editing. A JSON file may be strict JSON,
  JSONC, JSON5, package metadata, lockfile data, TypeScript/ESLint/Biome config,
  OpenAPI/JSON Schema, test fixture data, or app-specific config.
- Match the repo's parser dialect, indentation, key order, newline policy,
  generated-file policy, and package-manager expectations.
- Prefer local schemas, examples, fixture builders, and validation commands
  already in use before adding new tooling.

For parser dialects, generated files, package/config ownership, and formatting:
`get_skill_file(name="json", path="references/configuration.md")`

## Syntax And Data Model

- Preserve JSON's data model intentionally: objects, arrays, strings, numbers,
  booleans, and null are not interchangeable with host-language types.
- Watch for duplicate keys, numeric precision, Unicode escaping, null vs missing
  fields, object ordering expectations, and top-level array/object contracts.
- Use JSONC/JSON5 features only when the target parser explicitly supports them.

For duplicate keys, numbers, string escaping, null semantics, and dialect
differences:
`get_skill_file(name="json", path="references/syntax-and-data-model.md")`

## Schema And Validation

- Validate the shape consumed by the target system, not just JSON syntax.
- Keep `$schema`, `$id`, required keys, enum values, references, defaults, and
  additional-property rules explicit.
- Update schemas, fixtures, generated examples, migration data, and docs
  together when a JSON contract changes.

For JSON Schema, OpenAPI, custom validators, fixtures, and migration checks:
`get_skill_file(name="json", path="references/schema-and-validation.md")`

## Parsing And Serialization

- Use structured parser/serializer APIs instead of string splicing.
- Preserve stable formatting and ordering where diffs, lockfiles, signatures,
  snapshots, or reproducible builds depend on them.
- Round-trip through the same parser or serializer that the consuming system
  uses when precision, dates, binary data, or patch formats matter.

For parsing APIs, streaming, canonicalization, merge patches, and precision:
`get_skill_file(name="json", path="references/parsing-and-serialization.md")`

## Security And Secrets

- Never inline secrets, tokens, private keys, credentials, or sensitive endpoints.
- Treat untrusted JSON as hostile input: validate size, depth, schema, keys, and
  prototype-pollution-sensitive fields before merging.
- Review package metadata, scripts, registry URLs, plugin config, and generated
  artifacts for supply-chain or privilege changes.

For secret references, prototype pollution, package metadata, and untrusted
input:
`get_skill_file(name="json", path="references/security-and-secrets.md")`

## Testing

- Add or update focused validation for the consumer of the JSON, not only the
  JSON file itself.
- Use syntax lint, schema checks, parser round trips, fixture/golden tests,
  config loader tests, and targeted application tests that cover the changed
  behavior.
- Keep validation commands narrow enough to run locally and broad enough to
  prove the changed contract.

For command selection, schema tests, snapshot hygiene, and fixture validation:
`get_skill_file(name="json", path="references/testing.md")`

## Before You Finish

If you touched JSON: verify formatting/lint, parser/schema validation, focused
consumer tests, and any relevant generated-output or lockfile checks pass before
closing your work.
