# Template Library

Everything in this directory is a **template**, not active enforcement.

- Templates are bundled with Gobby and synced to the `workflow_definitions` DB table by
  the sync modules (`sync_rules.py`, `sync_pipelines.py`, etc.)
- Templates have `enabled: true` by default — the template's `enabled` value is used
  directly when creating the installed DB row on first sync
- Existing DB rows are never overwritten by sync — drift is detected via hash comparison
- The `deprecated/` subdirectories are excluded from sync entirely
- The database is the source of truth for what's active, not these YAML files

## Configurability Convention

Bundled templates are Gobby-owned, immutable inputs. They are tagged as Gobby templates
(`source: gobby`/`tags: [gobby]`) when synced into the database, and tooling must not
mutate those installed rows in place.

User customization happens by cloning a bundled template into the matching project-local
path under `.gobby/install/<kind>/<name>/`. If the user copy keeps the same identifier as
a bundled template, it must include an explicit `override: true` label. Loading fails loud
when a project-local copy shadows a bundled template without that label, so accidental
shadowing cannot silently change runtime behavior.
