# YAML Configuration

## Identify The Owner

- Determine which system consumes the file before editing. A `.yaml` file can be
  plain data, application config, CI workflow, Kubernetes manifest, Helm values,
  OpenAPI schema, Docker Compose, Ansible, cloud-init, static-site config, or
  linter/tool config.
- Read nearby examples, schemas, docs, generator inputs, rendered outputs, and CI
  commands before changing structure.
- Preserve generated files unless the repo explicitly expects direct edits. If a
  file is generated, update the source and regenerate with the repo command.

## Parser And Version

- Prefer the parser/version already used by the repo. YAML 1.1 and YAML 1.2
  differ in implicit scalar typing, so do not assume every consumer handles
  booleans, nulls, dates, or numbers the same way.
- Keep document start/end markers, multi-document streams, indentation width,
  line endings, key order, comments, and trailing newline style consistent with
  the local file.
- Treat application-specific extensions as part of the format: GitHub Actions
  expressions, Helm templates, CloudFormation intrinsics, Kubernetes resource
  fields, and Ansible variables are not generic YAML.

## Tooling

- Use existing config first: `.yamllint`, prettier config, editorconfig, schema
  mapping, CI scripts, Makefile targets, package scripts, or repo wrappers.
- Prefer deterministic tools and pinned versions. Record tool output only when it
  changes source or generated artifacts intentionally.
- Do not add a new formatter or schema dependency unless it is needed for the
  changed behavior and fits the repo's dependency policy.

## Change Discipline

- Keep semantic changes separate from mechanical formatting when possible.
- Preserve comments that document operational intent, owners, runbooks, or
  migration state.
- Update linked documentation, examples, fixtures, snapshots, rendered manifests,
  and schema files when the YAML contract changes.
- When a config file controls production behavior, identify the rollback or
  compatibility concern before changing defaults.
