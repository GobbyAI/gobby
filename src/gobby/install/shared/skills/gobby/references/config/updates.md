# Update configuration

Load before persistent runtime changes, removals or recovery from write failures.
Use `gobby-config:patch_config_values` after loading its current schema. Confirm
the requested keys and scope, read the public schema and values, then compose
one patch with the observed `expected_revision`, nested `values`, and canonical
dotted `unset` keys. Unsetting restores the registry default; JSON null is a
value subject to field validation, not a universal delete instruction.

The service validates the candidate and commits through one compare-and-swap.
Inspect `committed`, `revision`, `changed_keys`, `apply_status`,
`pending_restart_keys` and `failed_live_keys`; read values again to verify the
intended effect. Do not use direct SQL or bypass the public service.

- `revision_conflict`: refresh and recompose against current state; do not
  blindly replay the old patch or substitute a guessed revision.
- `validation_error`: correct the reported path/type/constraint before retrying.
- `managed_activation_required`: use the owning lifecycle, not the generic setter.
- `failed_live` or `reconcile_failed` with `committed: true`: persistence succeeded.
  Inspect active state and diagnostics; do not treat this as rollback.
- `persistence_indeterminate`: establish persisted state before considering retry.

Secret-designated public values accept plaintext strings through encrypted
storage; they reject `$secret:NAME` as plaintext. Masks preserve existing secrets.
Never log submitted payloads. Structured secret fields have schema-specific
identity and preservation rules; do not assume scalar behavior applies to them.

Operator/client YAML export and replacement use `/api/config/template` or the
`export`/`import` aliases. Replacement takes `content` and `expected_revision`,
replaces the daemon namespace, preserves supplemental namespaces and masks,
and resets omitted ordinary overrides. This is broader than a patch.
YAML secret references must already resolve in the named secret store; scalar
values patches instead accept plaintext input. Managed
embedding changes still require the embedding lifecycle. See
[HTTP configuration](../../../../../../../../docs/guides/configuration.md#http-configuration-api) and
[recovery](../../../../../../../../docs/guides/configuration.md#runtime-setting-does-not-stick).

Other operator/client routes have separate owners: `/api/config/prompts` lists
prompts; GET/PUT/DELETE `/api/config/prompts/{path}` reads, overrides, or reverts
them in the daemon's project scope. Deleting an override restores bundled
behavior. `/api/config/secrets` lists metadata or saves a named credential;
DELETE `/api/config/secrets/{name}` rejects still-referenced credentials with
409. Secret mutations require authentication and never return plaintext.
`POST /api/config/validation-detection/preview` tests matcher composition without
saving it. Consult current HTTP request models before using these surfaces;
they do not inherit the generic patch's revision protocol automatically.
