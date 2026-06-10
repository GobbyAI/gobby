# YAML Security And Secrets

## Secrets

- Do not inline tokens, private keys, passwords, certificates, service account
  JSON, kubeconfigs, connection strings, or sensitive endpoints.
- Prefer secret references, environment injection, sealed/encrypted secret
  systems, or platform secret stores already used by the repo.
- Check that examples and tests use inert placeholders, not realistic secrets.

## Permissions

- Use least privilege for CI tokens, workflow permissions, Kubernetes RBAC,
  service accounts, cloud roles, and deployment credentials.
- Be careful with pull-request workflows, third-party actions, untrusted inputs,
  artifact downloads, cache keys, and shell interpolation.
- Pin actions, images, charts, and remote includes according to repo policy.
  Mutable references need a clear reason and validation path.

## Deployment Safety

- Preserve network policy, security context, privileged flags, host mounts,
  admission settings, resource limits, and environment separation.
- Validate changes that alter production rollout, identity, access, data
  retention, or external exposure.
- Keep rollback-sensitive fields documented: image tags, replicas, feature flags,
  migration toggles, and traffic routing.

## Review Checklist

- Search the diff for accidental secrets and broad permissions.
- Confirm any new secret reference exists in the target environment or has a
  documented provisioning path.
- Verify generated/rendered output does not leak secret values.
- Include security-relevant validation evidence in the task closeout when the
  YAML controls CI, deployment, identity, or network exposure.
