---
name: architecture
description: Internal methodology for the architecture discovery stage. Produces a concise Architecture Brief for product and planning handoff.
version: "1.0.0"
category: methodology
internal: true
triggers: architecture brief, technical design, interfaces
metadata:
  gobby:
    audience: agent
    depth: 0
---

# architecture - Architecture Brief Methodology

Use this skill when acting as the `architect` discovery-stage agent.

## Purpose

Translate ideation and research into technical direction. Capture the drivers,
decisions, components, interfaces, and trade-offs that planning must preserve.

## Method

1. Read the task and prior discovery marker blocks.
2. Identify architectural drivers: data shape, lifecycle, integration points,
   safety, performance, compatibility, and migration constraints.
3. Make decisions only where evidence supports them.
4. Describe the smallest useful component/interface boundaries.
5. Surface trade-offs and unresolved questions that planning must account for.

## Output

Write only this section shape:

```markdown
## Architecture Brief

### Drivers
- <technical driver>

### Decisions
- <decision and reason>

### Components
- <component or module responsibility>

### Interfaces
- <API, storage, MCP, CLI, or file contract>

### Trade-offs
- <trade-off>

### Open Questions
- <question or "None">
```

Do not write implementation tasks or PRD acceptance criteria. Keep the brief
actionable for the planner.
