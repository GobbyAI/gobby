# Discover configuration schema

Load before choosing keys, interpreting metadata or diagnosing rejected values.
Fetch the schema for `gobby-config:get_config_schema`, then call it without
arguments. Its public registry schema describes canonical keys and patterned
keys; it is not an authorization to change every advertised setting.

Match the intended key to its registry entry and inspect type, default,
visibility, secrecy and activation metadata. Preserve canonical external names,
including `gobby-tasks` and `ai.embeddings.*`. Do not infer accepted keys from
Python attribute names, old YAML examples or an unrelated tool's parameters.

Use the nested values shape required by `patch_config_values`; its tool schema
governs the request envelope. Unknown keys and non-public keys are rejected.
Managed fields require the named lifecycle action even when publicly readable.
Bootstrap-only fields are outside the runtime registry.

If a key is absent, inspect its owning capability and installed version instead
of inventing a setter or writing a storage row. HTTP `GET /api/config/schema`
is the corresponding operator/client route. See
[runtime configuration](../../../../../../../../docs/guides/configuration.md#runtime-configuration).
