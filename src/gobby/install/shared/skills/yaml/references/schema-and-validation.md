# YAML Schema And Validation

## Validate More Than Syntax

- A YAML parser only proves that the document parses. It does not prove the
  target system accepts the shape, keys, values, permissions, or references.
- Find the schema source: JSON Schema, OpenAPI, Kubernetes CRD, Helm chart schema,
  CI workflow schema, Docker Compose spec, Ansible module docs, or local
  application validator.
- Prefer validation commands already wired into the repo over generic online
  validators.

## Contract Changes

- When adding or renaming keys, update schemas, examples, fixtures, generated
  documentation, tests, and migration notes together.
- Preserve required keys, enum values, defaults, deprecations, version fields,
  feature flags, and compatibility windows.
- Validate all consumers when the same YAML is read by multiple systems, such as
  app code and deployment tooling.

## Schema Techniques

- Use explicit types for ambiguous fields. Strings that look numeric or boolean
  should be modeled as strings if the domain expects identity, not arithmetic.
- Validate `additionalProperties`, nullable fields, default behavior, enum
  values, references, and version constraints.
- For Kubernetes and similar APIs, validate against the cluster/API version that
  will consume the manifest, not only a local parser.
- For OpenAPI, validate references, operation IDs, examples, request/response
  schemas, and generated client compatibility.

## Migrations

- Keep old and new keys compatible when rollout requires mixed versions.
- Add tests for both accepted and rejected config shapes when changing a public
  config contract.
- If removing support, search for in-repo examples, docs, and generated outputs
  that still use the old shape.
