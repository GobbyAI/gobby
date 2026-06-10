# YAML CI And Platform Boundaries

## GitHub Actions And CI

- Preserve workflow triggers, branch filters, path filters, concurrency groups,
  environment protections, job dependencies, matrix expansion, permissions, and
  reusable workflow inputs.
- Validate expressions separately from YAML parsing. `${{ ... }}` syntax can be
  valid YAML but invalid workflow logic.
- Avoid broadening pull-request, token, package, deployment, or OIDC permissions
  unless the task explicitly requires it and validation covers the behavior.

## Kubernetes And Helm

- Preserve API versions, namespaces, labels/selectors, resource requests/limits,
  probes, security context, service account bindings, rollout strategy, and
  owner labels.
- Render Helm templates with representative values and validate rendered
  manifests. Source templates alone are not enough.
- Check CRD versions and cluster compatibility when changing custom resources.

## Docker Compose And Cloud Config

- Validate service names, networks, volumes, environment interpolation, health
  checks, image tags, profiles, and dependency ordering.
- Avoid unpinned `latest` tags or mutable references for production paths unless
  the repo already uses that policy.
- For cloud-init and platform config, preserve ordering and idempotence. Some
  sections run once, some run on every boot, and some depend on previous output.

## Ansible And Other Config

- Preserve module-specific field names, variable precedence, inventory/group
  behavior, handlers, tags, and idempotence.
- Validate playbooks or config through the owning tool when possible.
- Keep local conventions for comments, key order, and grouped settings because
  they often map to operational review workflows.
