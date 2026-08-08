> You are continuing a session under the `impeccable` skill; the design-context protocol and anti-pattern rules already apply.

# Doctor: inspect Impeccable drift

Report drift among `.impeccable.md`, `.impeccable/config*.json`, persisted
surface briefs, and the released script runtime. This is maintenance work; keep
design changes outside this flow.

Gobby owns skill and managed-CLI lifecycle. Use `gobby install` when the managed
CLI needs installation or repair. Released skill updates happen only through
Gobby's repository re-vendor workflow.

## 1. Run the released diagnostic

```bash
node <scripts_dir>/doctor.mjs --json
```

Resolve `<scripts_dir>` by calling `materialize_skill_scripts(name="impeccable")` on `gobby-skills`; it returns the absolute path of the skill's materialized `scripts/` directory. Export the returned `environment.PUPPETEER_CACHE_DIR` before any browser-engine invocation. If the tool or Node is unavailable, inspect `.impeccable.md`, config, and surface briefs manually.

Add `--target <path>` when the user named a workspace, route, or artifact. Report
`ruleRegistryAvailable: false` explicitly. An empty `findings` array is the good
outcome.

The released diagnostic may name upstream product/design artifacts. Interpret
those findings through Gobby's ownership model:

- Missing or stale design context routes to inline `teach` mode.
- Incumbent design-system drift routes to `document` by loading
  `references/document.md` through `get_skill_file`.
- Config and surface-brief schema findings remain mechanical doctor findings.
- Hook-manifest findings are informational because Gobby rules own edit-time
  enforcement.

## 2. Act by severity

- **`auto`**: run `node <scripts_dir>/doctor.mjs --fix` once, limited to config
  and surface-brief migrations, then report what changed.
- **`mention`**: state the finding and offered fix without changing design.
- **`route`**: name `teach` or `document` and the exact context gap. Run the
  routed flow only when the user requested that work.

Do not treat commit counts or inherited workspace context as proof of drift.
Compare the named source to `.impeccable.md` before making a truth claim.
