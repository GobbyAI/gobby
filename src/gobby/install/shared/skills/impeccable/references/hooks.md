> You are continuing a session under the `impeccable` skill; the design-context protocol and anti-pattern rules already apply.

# Vendored hook scripts

The released script tree includes Impeccable hook helpers for compatibility and
inspection. Gobby rules own edit-time enforcement. This reference documents the
available scripts; it does not install, enable, disable, or repair provider hook
manifests.

Paths below are relative to the `scripts_dir` returned by
`materialize_skill_scripts(name="impeccable")` on `gobby-skills`:

| Script | Released purpose |
|---|---|
| `hook.mjs` | Post-edit detector entry point and end-of-session deep pass. |
| `hook-before-edit.mjs` | Pre-edit detector entry point used by blocking harness hooks. |
| `hook-admin.mjs` | Validated config, status, and ignore-management helper. |
| `hook-lib.mjs` | Shared hook matching, config, cache, and detector utilities. |

## Gobby integration boundary

- Gobby's installed rules decide when edits are checked and whether a finding
  blocks, warns, or injects context.
- This flow never writes `.claude/settings*.json`, `.codex/hooks.json`,
  `.cursor/hooks.json`, or `.github/hooks/*.json`.
- Project detector policy remains in `.impeccable/config.json`; private local
  overrides may live in `.impeccable/config.local.json`.
- Managed CLI setup and repair run through `gobby install`.

Use `get_skill_file` to inspect a released script when diagnosing its behavior.
Changes to enforcement belong in Gobby's rules engine. Changes to released
script bytes belong in the repository-only re-vendor workflow.
