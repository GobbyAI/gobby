# Rule Templates Reference

This directory contains 16 bundled rule groups. These are **templates** — they are synced to the `workflow_definitions` DB table on daemon start with `enabled: true` by default. Existing DB rows are never overwritten; drift is detected via hash comparison. See `../CLAUDE.md` for the template vs active enforcement distinction.

## Rule Groups

| Group | Dir | Rules | Purpose |
|-------|-----|-------|---------|
| `worker-safety` | `worker-safety/` | 7 | Block git push (global + worker-scoped), force push, destructive git, bash sleep, agent spawn from merge, external GitHub issues |
| `tool-hygiene` | `tool-hygiene/` | 2 | Require `uv` for Python, track pending memory review |
| `progressive-discovery` | `progressive-discovery/` | 7 | Enforce MCP discovery order: list_servers → list_tools → get_schema → call_tool |
| `task-enforcement` | `task-enforcement/` | 8 | Block native task tools, require task before edit, track claims, require commits before status, block validation skip, block needs_review and review_approved for interactive, require error triage before status |
| `stop-gates` | `stop-gates/` | 2 | Require task close and epic tree close before stop |
| `plan-mode` | `plan-mode/` | 3 | Detect enter/exit plan mode, reset on session start |
| `memory-lifecycle` | `memory-lifecycle/` | 6 | Memory recall, digest, capture, title generation, tracking reset |
| `context-handoff` | `context-handoff/` | 7 | Session summary injection (clear/compact/resume), task context, baseline dirty files |
| `auto-task` | `auto-task/` | 3 | Autonomous task execution context, task continuation, notify tree complete |
| `messaging` | `messaging/` | 4 | P2P messaging: deliver pending, activate commands, tool restrictions, exit conditions |
| `pipeline-enforcement` | `pipeline-enforcement/` | 1 | Auto-run assigned pipeline on session start |
| `error-recovery` | `error-recovery/` | 1 | Inject recovery guidance after tool failures |
| `tdd-enforcement` | `tdd-enforcement/` | 2 | TDD one-shot Write nudge, track test file writes |
| `skill-discovery` | `skill-discovery/` | 3 | Inject language skills on first file read, reset injection tracking on context loss |
| `context7` | `context7/` | 1 | Block first code file write/edit, inject context7 docs skill |
| `deprecated/` | `deprecated/` | — | Old rules excluded from sync |

## File Convention

Each group is a directory containing one or more YAML files. Each YAML file has:

```yaml
tags: [group-tag, category-tag]     # Tags for selector matching

rules:
  rule-name:
    description: "..."
    event: before_tool
    enabled: true                    # Templates default to enabled
    priority: 100
    when: "condition"
    effect:
      type: block
      ...
```

Multiple rules can live in one YAML file, or each rule can have its own file. The convention varies by group.

## Tags

Tags serve two purposes:

| Tag | Meaning |
|-----|---------|
| `gobby` | **Provenance** — rule ships with gobby. All non-deprecated rules get this tag. |
| `default` | **Audience** — rule applies to the interactive session (default/default-web-chat agents). |
| Group tags | **Identity** — rule belongs to a functional group. Workers cherry-pick these. |

The `default` agent uses `rule_selectors: {include: ["tag:default"]}` to load all interactive-session rules. Worker agents (e.g., `pipeline-worker.yaml`) select specific group tags instead.

Rules in `worker-safety` and `pipeline-enforcement` have `gobby` but NOT `default` — they are only loaded by agents that explicitly select their group tags.

### Group Tags

| Tag | Groups |
|-----|--------|
| `enforcement` | worker-safety, task-enforcement, stop-gates, tdd-enforcement |
| `safety` | worker-safety |
| `discovery` | progressive-discovery |
| `memory` | memory-lifecycle |
| `context` | context-handoff |
| `messaging` | messaging |
| `pipeline` | pipeline-enforcement |
| `skill-discovery` | skill-discovery |

## Guides

- [Rules](../../../docs/guides/rules.md) — Full rules reference
- [Variables](../../../docs/guides/variables.md) — Session variables used in conditions
- [Workflows Overview](../../../docs/guides/workflows-overview.md) — How rules fit the system
