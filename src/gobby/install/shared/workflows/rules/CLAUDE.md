# Rule Templates Reference

This directory contains bundled rule groups. These are **templates** — they are synced to the `rule_definitions` DB table on daemon start with `enabled: true` by default. Existing installed bundled rows are refreshed when template content changes while preserving the user's enabled toggle. Installed bundled rows whose templates were removed are soft-deleted as orphans. See `../CLAUDE.md` for the template vs active enforcement distinction.

## Rule Groups

| Group | Dir | Rules | Purpose |
|-------|-----|-------|---------|
| `worker-safety` | `worker-safety/` | 55 | Block git push (global + worker-scoped), force push, destructive git/shell, unmanaged worktree/clone commands, bash sleep, agent spawn from merge, external GitHub issues, package install/publish, remote-script exec, full test suite, daemon management, data exfiltration (curl/wget upload, scp/sftp, secret-path reads) |
| `tool-hygiene` | `tool-hygiene/` | 4 | Require `uv` and route memory operations through Gobby |
| `progressive-discovery` | `progressive-discovery/` | 5 | Require a current-context schema lease before ordinary MCP calls; track optional inventory discovery |
| `task-enforcement` | `task-enforcement/` | 20 | Require claimed tasks and lifecycle skills, protect shared-worktree edits and commits, and enforce valid task transitions |
| `stop-gates` | `stop-gates/` | 6 | Require workflow completion and enforce the Found Work ladder before turn end |
| `plan-mode` | `plan-mode/` | 6 | Track plan-mode entry and exit, block edits, teach plan navigation, and reset state |
| `memory-lifecycle` | `memory-lifecycle/` | 14 | Recall, digest, layered guidance, post-close review, plan-memory guards, turn sequencing, and tracking reset |
| `context-handoff` | `context-handoff/` | 10 | Compact/resume handoffs, task context, user profile, wiki context, and pressure nudges |
| `auto-task` | `auto-task/` | 3 | Autonomous task execution context, task continuation, notify tree complete |
| `build-coordinator` | `build-coordinator/` | 1 | Require build-coordinator guidance for Gobby build work |
| `code-index` | `code-index/` | 5 | Require code-index guidance and prefer `gcode` for search and source navigation |
| `monolith-enforcement` | `monolith-enforcement/` | 4 | Require same-session decomposition before writes, commits, task transitions, and turn end |
| `pipeline-enforcement` | `pipeline-enforcement/` | 1 | Auto-run assigned pipeline on session start |
| `error-recovery` | `error-recovery/` | 1 | Inject recovery guidance after tool failures |
| `tdd-enforcement` | `tdd-enforcement/` | 2 | TDD one-shot Write nudge, track test file writes |
| `skill-discovery` | `skill-discovery/` | 25 | Require language skills on first file write, require the impeccable design contract on first UI file write, require a plan skill on plan-artifact writes, discover skill hubs, and reset loading tracking |
| `brevity` | `brevity/` | 6 | Load brevity on turn start, opt-out phrases, drift detection and next-turn feedback, per-turn reinforcement |
| `restraint` | `restraint/` | 3 | Block first code write/edit until restraint is loaded, opt-out phrases, per-turn reinforcement |
| `review-learning` | `review-learning/` | 5 | Inject confirmed planning and review lessons into matching work |
| `reviewer-lifecycle` | `reviewer-lifecycle/` | 3 | Track reviewer validation and require a terminal review verdict |

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

For lifecycle authoring, prefer semantic workflow events such as `turn_start`
and `turn_end`. Raw events such as `before_agent`, `after_agent`, and `stop`
are escape hatches for provider-specific detail.

## Tags

Tags serve two purposes:

| Tag | Meaning |
|-----|---------|
| `gobby` | **Provenance** — rule ships with Gobby. All bundled rules get this tag. |
| `default` | **Audience** — rule applies to the interactive session (`default` agent). |
| Group tags | **Identity** — rule belongs to a functional group. Workers cherry-pick these. |

The `default` agent uses `rule_selectors: {include: ["tag:default"]}` to load interactive-session rules. Worker agents select the group tags needed for their role.

`pipeline-enforcement` omits `default` and is selected explicitly. `worker-safety` combines default-tagged interactive protections with worker protections selected by group tag.

### Group Tags

Each group uses its directory name as an identity tag. Cross-cutting tags such as
`enforcement`, `lifecycle`, `context`, and `memory` may span several groups.

## Guides

- [Rules](../../../docs/guides/rules.md) — Full rules reference
- [Variables](../../../docs/guides/variables.md) — Session variables used in conditions
- [Workflows Overview](../../../docs/guides/workflows-overview.md) — How rules fit the system
