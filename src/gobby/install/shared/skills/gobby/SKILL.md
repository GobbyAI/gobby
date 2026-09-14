---
name: gobby
description: "Router contract for provider-aware Gobby help and installed skill dispatch."
version: "3.1.0"
category: core
triggers: help
metadata:
  gobby:
    audience: all
---

# Gobby Router

Gobby skill routing is provider-dependent. Codex uses `$gobby`; providers with
an installed slash router use `/gobby`. The router advertises installed skills
and catalog capabilities on bare help requests and routes explicit loads through
`gobby-skills`.

## Help Requests

For `$gobby`, `$gobby help` (Codex), `/gobby`, or `/gobby help` (other
providers), display the daemon-supplied help menu immediately and finish.
Make zero tool calls, including on the first turn. Do not load this router,
the catalog, other skills, or references to answer help. Defer housekeeping
and bootstrap instructions to the next work request. Menus never execute
listed operations.

If the daemon supplies an overflow message, display it and finish. If no menu
is supplied, say "Gobby help is unavailable." and finish. Neither case starts
discovery, diagnostics, or an automatic recovery chain.

Every displayed command uses the active provider's prefix: `$gobby` for Codex,
`/gobby` for other supported providers.

## Explicit Loading

For a selected capability or reference, use the daemon-supplied route. If route
metadata is missing, retrieve `catalog.json` with
`get_skill_file(name="gobby", path="catalog.json")` on `gobby-skills` after
leasing its schema. This discovery applies only to explicit loading requests.
The catalog is authoritative; never duplicate its inventory in this router.

Load each selected overview or topic with
`get_skill_file(name="gobby", path="<catalog path>")`, schema first when
unleased. Follow every `page.next_cursor` using only `cursor` until null.
A menu, catalog, partial page, failed load, or router does not satisfy a
reference requirement. Tool schemas remain authoritative for parameters.

## Routing

| Request after the trigger | Action |
| --- | --- |
| `<capability>` | Load its catalog overview |
| `<capability> references` | Show topic descriptions, loading conditions and exact `<trigger> <capability> references <topic>` examples |
| `<capability> references <topic>` | Load exactly that topic |
| `<capability> <request>` | Load the overview and each topic whose loading condition applies, then handle the original request |
| `<skill> [args]` | Resolve the installed standalone skill and load it |
| `skill <skill> [args]` | Explicit standalone dispatch, including capability-name collisions |
| Unknown name or topic | Show available choices; perform no operation |

Capability names take precedence. Singular `skill` escapes to standalone
resolution; plural `skills` is the skill-management capability. Honor project
overrides and existing skill-name resolution. These provider forms are supported:

```text
$gobby <skill> [args]
$gobby skill <skill> [args]
/gobby <skill> [args]
/gobby skill <skill> [args]
/gobby:<skill> [args]
```

Standalone dispatch uses `get_skill(name="<skill>")`. If a leading argument
selects a declared skill level, pass it as `level` to that load. Preserve the
remaining arguments and complete every content page before continuing.
The router does not inline skill bodies. Trailing command arguments remain in the original user prompt
and must not be duplicated into `<gobby-context>`. Native Skill calls preserve
their arguments in the returned directive because the blocked call will not run.
