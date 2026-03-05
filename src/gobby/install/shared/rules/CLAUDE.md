# Rule Templates Reference

This directory contains 14 bundled rule groups. These are **templates** — they are synced to the `workflow_definitions` DB table on daemon start but have `enabled: false` by default. See `../CLAUDE.md` for the template vs active enforcement distinction.

## Rule Groups

| Group | Dir | Rules | Purpose |
|-------|-----|-------|---------|
| `worker-safety` | `worker-safety/` | 6 | Block git push (global + worker-scoped), force push, destructive git, agent spawn from merge, external GitHub issues |
| `tool-hygiene` | `tool-hygiene/` | 2 | Require `uv` for Python, track pending memory review |
| `progressive-discovery` | `progressive-discovery/` | 7 | Enforce MCP discovery order: list_servers → list_tools → get_schema → call_tool |
| `task-enforcement` | `task-enforcement/` | 6 | Block native task tools, require task before edit, track claims, require commits before close, block validation skip |
| `stop-gates` | `stop-gates/` | 2 | Require task close before stop, require error triage before stop |
| `plan-mode` | `plan-mode/` | 3 | Detect enter/exit plan mode, reset on session start |
| `memory-lifecycle` | `memory-lifecycle/` | 9 | Memory sync, recall, digest, capture, title generation, tracking reset |
| `context-handoff` | `context-handoff/` | 10 | Session summary injection (clear/compact/resume), error triage, task context, baseline dirty files, task sync import |
| `auto-task` | `auto-task/` | 3 | Autonomous task execution context, task continuation, notify tree complete |
| `messaging` | `messaging/` | 4 | P2P messaging: deliver pending, activate commands, tool restrictions, exit conditions |
| `pipeline-enforcement` | `pipeline-enforcement/` | 1 | Auto-run assigned pipeline on session start |
| `error-recovery` | `error-recovery/` | 1 | Inject recovery guidance after tool failures |
| `tdd-enforcement` | `tdd-enforcement/` | 2 | TDD one-shot Write nudge, track test file writes |
| `deprecated/` | `deprecated/` | — | Old rules excluded from sync |

## File Convention

Each group is a directory containing one or more YAML files. Each YAML file has:

```yaml
tags: [group-tag, category-tag]     # Tags for selector matching

rules:
  rule-name:
    description: "..."
    event: before_tool
    enabled: false                   # Templates default to disabled
    priority: 100
    when: "condition"
    effect:
      type: block
      ...
```

Multiple rules can live in one YAML file, or each rule can have its own file. The convention varies by group.

## Tags

Tags are used by agent `rule_selectors` to control which rules are active:

| Tag | Groups |
|-----|--------|
| `gobby` | All non-deprecated groups (the standard selector) |
| `enforcement` | worker-safety, task-enforcement, stop-gates, tdd-enforcement |
| `safety` | worker-safety |
| `discovery` | progressive-discovery |
| `memory` | memory-lifecycle |
| `context` | context-handoff |
| `messaging` | messaging |
| `pipeline` | pipeline-enforcement |

The default agent (`default.yaml`) uses `rule_selectors: {include: ["tag:gobby"]}` to load all standard rules.

## Guides

- [Rules](../../../docs/guides/rules.md) — Full rules reference
- [Variables](../../../docs/guides/variables.md) — Session variables used in conditions
- [Workflows Overview](../../../docs/guides/workflows-overview.md) — How rules fit the system
