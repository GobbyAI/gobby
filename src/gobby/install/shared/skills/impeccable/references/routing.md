> You are continuing a session under the `impeccable` skill; the design-context protocol and anti-pattern rules already apply.

# No-argument routing: the context-aware menu

Read this when the user invokes impeccable with no argument. Present a
context-aware menu and wait for the user's choice; never auto-run a command.

If `.impeccable.md` is missing, lead with `teach` and explain that it creates the
project design contract. Still show the complete `## Sub-command Dispatch` menu
from `SKILL.md`.

When `.impeccable.md` exists, run the released signal collector once:

```bash
node <scripts_dir>/context-signals.mjs
```

Resolve `<scripts_dir>` by calling `materialize_skill_scripts(name="impeccable")` on `gobby-skills`; it returns the absolute path of the skill's materialized `scripts/` directory. Export the returned `environment.PUPPETEER_CACHE_DIR` before any browser-engine invocation. If the tool or Node is unavailable, skip detector runs and build the menu from repository evidence.

Use its code, critique, change, and dev-server signals. Treat legacy
product/design-file signals as advisory because `.impeccable.md` is Gobby's
authority. Lead with two or three high-value commands and one sentence of
evidence for each, followed by the complete dispatch table.

- No critique snapshot for a real surface: suggest `critique`.
- Material P0/P1 critique findings: suggest `polish`.
- One changed surface: scope `audit` or `polish` to those files.
- Running web dev server: `live` may be useful after that command is available.
- Native project: prefer native `audit` or `adapt`; browser tooling does not
  apply.

If `scan.targets` contains web files, run the bundled detector once:

```bash
node <scripts_dir>/detect.mjs --json <targets>
```

Fold verified findings into the recommendations. Detector failure never blocks
the menu. The table in `SKILL.md` remains the canonical command inventory.
