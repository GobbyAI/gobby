# `--color-agent` Audit - 2026-05-02

## Scope

- Source scope: `web/src`
- File globs: `*.tsx`, `*.ts`, `*.css`
- Reference command: `rg -n --column --glob '*.tsx' --glob '*.ts' --glob '*.css' -- '--color-agent' web/src`
- Count command: `rg -o --glob '*.tsx' --glob '*.ts' --glob '*.css' -- '--color-agent(-bg|-soft)?' web/src | wc -l`
- Result: 37 token occurrences across 14 files.
- `rg -n` prints 31 matching lines because some lines contain two token occurrences.

## Token Definitions

These six occurrences are token definitions, not UI usage.

| File | Lines | Occurrences | Tokens |
| --- | ---: | ---: | --- |
| `web/src/styles/index.css` | 69-71 | 3 | `--color-agent`, `--color-agent-bg`, `--color-agent-soft` |
| `web/src/styles/index.css` | 135-137 | 3 | `--color-agent`, `--color-agent-bg`, `--color-agent-soft` |

## Classification Summary

| Classification | Occurrences | Notes |
| --- | ---: | --- |
| Stateful | 0 | No direct process, lifecycle, task-state, or health-state mappings found. |
| Live taxonomic usage | 25 | Category, type, source, graph, chart-series, or product-area mappings. |
| Ambiguous selected-state usage | 1 | `MemoryFilters.tsx:27` styles active filter chips. |
| Orphan or non-live usage | 5 | Static map or CSS selectors with no current source usage found. |
| Token definitions | 6 | Theme token definitions in `index.css`. |
| Dead fallback usage | 0 | Exact `var(--accent, var(--color-agent))` search returned no matches. |

## Live Taxonomic Groups

| Group | References | Occurrences | Meaning |
| --- | --- | ---: | --- |
| Cron | `web/src/components/CronJobsPage.tsx:57` | 2 | `pipeline` cron action badge. |
| MCP | `web/src/components/mcp/McpPage.tsx:57` | 2 | `websocket` transport badge. |
| Dashboard | `web/src/components/dashboard/MetricsChartsCard.tsx:165` | 2 | `mcpCalls` area chart series. |
| Memory | `web/src/components/memory/MemoryFilters.tsx:19`, `web/src/components/memory/MemoryTable.tsx:37` | 2 | `preference` memory type marker. |
| Knowledge graph | `web/src/components/memory/KnowledgeGraph.tsx:61`, `web/src/components/memory/KnowledgeGraph.tsx:66` | 2 | `class` and `test` entity types. |
| Code graph | `web/src/components/code-graph/CodeGraphExplorer.tsx:81`, `web/src/components/code-graph/CodeGraphExplorer.tsx:86`, `web/src/components/code-graph/CodeGraphExplorer.tsx:89`, `web/src/components/code-graph/CodeGraphExplorer.tsx:95` | 4 | `folder`, `module`, `type` node colors and `CALLS` edge color. |
| Workflows | `web/src/components/workflows/PipelineEditor.tsx:51`, `web/src/components/workflows/WorkflowsPage.css:341-342`, `web/src/components/workflows/WorkflowsPage.css:351-352` | 6 | Pipeline editor badge, pipeline workflow card type, and agent workflow card type. |
| Skills | `web/src/components/skills/styles.ts:7` | 2 | `github` skill source badge. |
| Tasks | `web/src/components/tasks/task-execution.css:106-107` | 2 | `epic` task type chip. |
| Chat | `web/src/components/chat/styles/sessions-tab.css:131` | 1 | `codex` session source dot. |

## Ambiguous Usage

| Reference | Occurrences | Reason |
| --- | ---: | --- |
| `web/src/components/memory/MemoryFilters.tsx:27` | 1 | `CHIP_ACTIVE_CLS` styles selected filter state shared by memory type chips and the recent-only chip. It is selected-state styling, not a stable category or lifecycle state. |

## Orphan Or Non-Live Usage

| Reference | Occurrences | Finding |
| --- | ---: | --- |
| `web/src/components/tasks/TaskBadges.tsx:44` | 2 | `TYPE_STYLES.epic` is declared and exported, but `rg` finds no current `web/src` usage outside the declaration/export. `TypeBadge` renders `chip chip--type-${type}` classes, with live epic styling in `task-execution.css:106-107`. |
| `web/src/styles/index.css:500` | 1 | `.session-dot.agent` is defined, but `rg` finds no current `web/src` usage of that selector. |
| `web/src/styles/index.css:541-542` | 2 | `.agent-badge` is defined, but `rg` finds no current `web/src` usage of that selector. |

## Dead Fallback Check

Command:

```sh
rg -n --column --glob '*.tsx' --glob '*.ts' --glob '*.css' 'var\(--accent, var\(--color-agent\)\)' web/src
```

Result: no matches.
