# YAML Testing

## Validation Layers

- Syntax/lint proves the file parses and follows style.
- Schema validation proves keys and values match the expected contract.
- Platform validation proves the owning system accepts the config.
- Focused consumer tests prove the changed behavior works.

Use as many layers as the change warrants. A low-risk comment-only edit may need
less; a CI, deployment, API, or security change needs platform validation.

## Common Commands

- Generic YAML: `yamllint`, prettier check, parser round-trip, duplicate-key
  checks, or repo wrappers.
- GitHub Actions: actionlint, workflow parser tests, expression checks, or
  targeted CI dry-run tooling when available.
- Kubernetes: `kubectl apply --dry-run=client/server`, kubeconform/kubeval,
  policy checks, Helm render plus manifest validation.
- Helm: `helm lint`, `helm template`, rendered snapshot tests, and schema checks
  for values files.
- Docker Compose: `docker compose config` and focused service smoke tests.
- OpenAPI: spec validation, generated client/server compatibility tests, and
  example validation.
- Ansible: `ansible-lint`, syntax checks, check mode, and focused role tests.

Prefer repo-local wrappers when present because they usually pin versions and
load local schemas.

## Focused Test Selection

- Run tests for the system that consumes the YAML. For example, a workflow edit
  needs workflow validation, while an app config edit needs the app config loader
  tests.
- Add fixtures for changed config contracts, including rejected invalid shapes
  when the validator is part of the behavior.
- Render template-based YAML before validating it. Check both source and rendered
  output when the source contains Helm, Jinja, or expression syntax.
- When changing generated YAML, validate the generator input and the generated
  output diff.

## Evidence

- Record exact commands and results.
- Explain why the commands cover the changed YAML owner and consumer.
- Call out any validation that cannot run locally, and provide the closest
  deterministic substitute.
