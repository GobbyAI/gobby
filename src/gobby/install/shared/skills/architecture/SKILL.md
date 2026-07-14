---
name: architecture
description: Internal methodology for the architecture discovery stage. Produces a concise Architecture Brief and Test Architecture strategy for product and planning handoff.
version: "1.1.0"
category: methodology
internal: true
triggers: architecture brief, technical design, interfaces, test architecture, test strategy, coverage risks
metadata:
  gobby:
    audience: all
---

# architecture - Architecture Brief & Test Strategy Methodology

Use this skill when acting as the `architect` discovery-stage agent. It covers
both marker sections the architecture stage owns: `## Architecture Brief` and
`## Test Architecture`.

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

---

# Test Architecture

Capture the testing strategy needed to guide downstream planning, expansion,
implementation, and QA. Focus on risks, coverage boundaries, and infrastructure
needs. Do not create task sections.

## Method

1. Read the assigned task, upstream discovery marker blocks, and the draft
   Architecture Brief.
2. Identify coverage risks by interface, integration point, persistence boundary,
   migration, and user workflow.
3. Separate test cases from test infrastructure. Test cases belong in downstream
   code leaves' TDD work; infrastructure needs may become standalone
   [category: test] leaves.
4. Prefer unit coverage when it is sufficient. Add broader test recommendations
   only when the risk crosses boundaries.

## Output

Write only this section shape:

```markdown
## Test Architecture

### Integration
- **<plan-section or surface>**: <what to verify>

### E2E
- **<plan-section or workflow>**: <full-flow scenario>

### Regression
- **<plan-section or bug class>**: <bug class to defend against>

### Contract
- **<surface>**: <surfaces that must agree>

### Infrastructure
- **<infra need>**: <new fixture, helper module, harness>
```

If unit tests are enough, write:

```markdown
## Test Architecture

Unit tests sufficient - no additional test types recommended.
```

Do NOT write `### N.N` task sections or PRD acceptance criteria.
