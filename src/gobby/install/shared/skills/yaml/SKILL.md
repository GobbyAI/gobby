---
name: yaml
description: "Enforces default YAML coding standards for agents writing or refactoring YAML: parser-aware syntax, schema validation, anchors and tags, platform configuration boundaries, testing, and secret handling. Use before editing YAML unless the repo provides stricter local rules."
version: "1.1.0"
category: development
triggers: yaml, yml, yaml-1.2, yamllint, json-schema, github-actions, kubernetes, docker-compose, ansible, helm, openapi, cloud-config
sources:
  - "Primary: YAML 1.2.2 specification from yaml.org, adapted for Gobby's bundled language-skill layout."
  - "Secondary: yamllint configuration/rules documentation and GitHub Actions workflow syntax documentation for common YAML validation and CI boundary behavior."
---

# YAML

Identify the consuming parser, schema, template layer, and platform before editing.

## Tooling

- Use repository formatting or lint, parser round-trip, applicable schema, platform
  render/dry-run, and focused consumer tests.
- Preserve generated YAML through its owning source and tool.

## Configuration

- Match parser version, YAML 1.1/1.2 behavior, indentation, document markers,
  schema entry point, and generated-file policy.
- Diagnostic hook: treat parser, schema, and platform errors as consumer evidence;
  avoid random quoting, reshaping, or validator suppression before locating the dialect.

For parsers, tools, schemas, and generated files:
`get_skill_file(name="yaml", path="references/configuration.md")`

## Syntax And Types

- Preserve mapping/sequence shape, scalar style, comments, ordering where observed,
  null semantics, and duplicate-key policy.
- Quote ambiguous booleans, dates, leading-zero numbers, globs, and expressions
  according to the actual parser and platform.

For YAML syntax and scalar behavior:
`get_skill_file(name="yaml", path="references/syntax-and-types.md")`

## Schema And Validation

- Validate the shape consumed by the target system, including required keys, enums,
  references, versions, and compatibility fields.
- Update dependent fixtures, examples, generated outputs, and migrations together.

For schemas and migrations:
`get_skill_file(name="yaml", path="references/schema-and-validation.md")`

## Anchors Tags And Templates

- Preserve alias identity, merge precedence, custom-tag semantics, and template
  evaluation order.
- Render Helm, Jinja, or expression layers before validating nested YAML.

For anchors, tags, and templates:
`get_skill_file(name="yaml", path="references/anchors-tags-and-templates.md")`

## CI And Platform Boundaries

- Preserve workflow triggers, permissions, environments, service names, resource
  limits, rollout semantics, and reusable-fragment ownership.

For Actions, Kubernetes, Helm, Compose, Ansible, and OpenAPI:
`get_skill_file(name="yaml", path="references/ci-and-platform-boundaries.md")`

## Security And Secrets

- Use platform secret references and least-privilege permissions; inspect pull-request
  trust, image/action pinning, and template injection boundaries.

For secrets and supply-chain risks:
`get_skill_file(name="yaml", path="references/security-and-secrets.md")`

## Testing

- Exercise the real consumer with syntax, schema, render, diff, dry-run, and
  platform checks selected for the changed file.

For validation commands:
`get_skill_file(name="yaml", path="references/testing.md")`
