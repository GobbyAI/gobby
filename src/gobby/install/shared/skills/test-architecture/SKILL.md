---
name: test-architecture
description: Internal methodology for architect-owned test architecture. Produces structured testing strategy prose inside the architecture discovery marker.
version: "1.0.0"
category: methodology
internal: true
triggers: test architecture, test strategy, coverage risks
metadata:
  gobby:
    audience: agent
    depth: 0
---

# test-architecture - Test Strategy Methodology

Use this skill when acting as the `architect` discovery-stage agent.

## Purpose

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
