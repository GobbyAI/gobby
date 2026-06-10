---
name: yaml
description: "Enforces default YAML coding standards for agents writing or refactoring YAML: parser-aware syntax, schema validation, anchors and tags, platform configuration boundaries, testing, and secret handling. Use before editing YAML unless the repo provides stricter local rules."
version: "1.0.0"
category: development
triggers: yaml, yml, yaml-1.2, yamllint, json-schema, github-actions, kubernetes, docker-compose, ansible, helm, openapi, cloud-config
sources:
  - "Primary: YAML 1.2.2 specification from yaml.org, adapted for Gobby's bundled language-skill layout."
  - "Secondary: yamllint configuration/rules documentation and GitHub Actions workflow syntax documentation for common YAML validation and CI boundary behavior."
---

# YAML

Default coding standards for YAML. Repo conventions and configured tooling take
precedence. If `.yamllint`, JSON Schema, OpenAPI, Kubernetes, Helm, Ansible,
GitHub Actions, CI, cloud-init, Docker Compose, or platform docs are stricter,
follow the repo.

## Tooling

Run the repo's configured format, lint, schema validation, platform validation,
and focused tests before finishing. If none are configured, use the local YAML
context:

- Format/lint: yamllint, prettier, editorconfig, or repo wrappers
- Parse/schema: parser round-trip, JSON Schema, OpenAPI, Kubernetes, Helm,
  Compose, Ansible, CI, or application-specific schema checks
- Platform checks: workflow dry runs, chart rendering, Kubernetes server/client
  validation, actionlint, compose config, or domain CLI validation
- Tests: focused tests for the system that consumes the changed YAML
- Packages: preserve generated YAML, lockfiles, vendored charts, schemas, and
  pinned tool versions

Do not treat YAML as plain text. Do not relax lint rules, schema strictness,
permissions, generated-file boundaries, or secret handling to make a quick edit
pass.

## Configuration

- Identify the owning platform before editing. A YAML file may be plain data,
  GitHub Actions, Kubernetes, Helm, OpenAPI, Docker Compose, Ansible, cloud-init,
  static-site config, linter config, or app-specific config.
- Match the repo's parser version, style, indentation, document markers, schema
  dialect, line wrapping, key order, and generated-file policy.
- Prefer local schemas, examples, generated outputs, and validation commands
  already in use before adding new tooling.

For parser/version choices, lint rules, generated files, schemas, and config
ownership:
`get_skill_file(name="yaml", path="references/configuration.md")`

## Syntax And Types

- Preserve indentation, sequence/mapping shape, block scalar style, quoting, and
  document boundaries intentionally.
- Quote ambiguous scalars such as booleans, dates, leading-zero numbers, glob
  patterns, colon-containing values, and strings that a target parser could
  coerce.
- Detect duplicate keys and parser-specific behavior before changing values.

For indentation, scalars, quoting, duplicate keys, nulls, and YAML 1.1/1.2
differences:
`get_skill_file(name="yaml", path="references/syntax-and-types.md")`

## Schema And Validation

- Validate the shape consumed by the target system, not just YAML syntax.
- Keep required keys, enum values, references, version fields, and compatibility
  rules explicit.
- Update schemas, fixtures, generated examples, and docs together when a config
  contract changes.

For JSON Schema, OpenAPI, Kubernetes schemas, custom validators, and migration
checks:
`get_skill_file(name="yaml", path="references/schema-and-validation.md")`

## Anchors Tags And Templates

- Treat anchors, aliases, merge keys, custom tags, Helm templates, Jinja, and
  expression languages as executable structure.
- Preserve alias identity, override precedence, tag semantics, and template
  delimiters when refactoring.
- Render or expand templates before claiming that nested YAML is valid.

For anchors, aliases, tags, merge keys, Helm/Jinja templates, and expression
boundaries:
`get_skill_file(name="yaml", path="references/anchors-tags-and-templates.md")`

## CI And Platform Boundaries

- Validate platform-specific behavior for workflows, deployment manifests,
  charts, compose files, playbooks, and cloud config.
- Preserve permissions, triggers, environments, service names, resource limits,
  probes, rollout strategy, variable interpolation, and include/import behavior.
- Keep reusable fragments and generated manifests aligned with source inputs.

For CI/CD, GitHub Actions, Kubernetes, Helm, Docker Compose, Ansible, OpenAPI,
and cloud config:
`get_skill_file(name="yaml", path="references/ci-and-platform-boundaries.md")`

## Security And Secrets

- Never inline secrets, tokens, private keys, credentials, or sensitive endpoints.
- Review permissions, trust boundaries, pull-request triggers, image tags,
  admission settings, environment injection, and secret references.
- Prefer least privilege and pinned immutable inputs where the platform supports
  them.

For secret references, CI permissions, supply chain risk, deployment settings,
and sensitive config:
`get_skill_file(name="yaml", path="references/security-and-secrets.md")`

## Testing

- Add or update focused validation for the consumer of the YAML, not only the
  YAML file itself.
- Use syntax lint, schema checks, platform dry runs, render/diff checks, and
  targeted application tests that cover the changed behavior.
- Keep validation commands narrow enough to run locally and broad enough to prove
  the changed contract.

For command selection, schema tests, render tests, and platform-specific checks:
`get_skill_file(name="yaml", path="references/testing.md")`

## Before You Finish

If you touched YAML: verify formatting/lint, parser/schema validation, focused
consumer tests, and any relevant platform dry run or rendered-output check pass
before closing your work.
