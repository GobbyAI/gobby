---
name: prd
description: Internal methodology for the PRD discovery stage. Produces a concise Product Reference Document for implementation planning.
version: "1.0.0"
category: methodology
internal: true
triggers: prd, product requirements, acceptance criteria
metadata:
  gobby:
    audience: agent
    depth: 0
---

# prd - Product Reference Document Methodology

Use this skill when acting as the `product-manager` discovery-stage agent.

## Purpose

Turn upstream discovery into product-readable requirements for planning. The
PRD should define goal, users, scope, explicit non-scope, acceptance criteria,
and the planning handoff.

## Method

1. Read the task and all prior discovery marker blocks.
2. Treat upstream discovery as authoritative unless the task description
   contradicts it; record contradictions as open planning handoff notes.
3. Define users and workflows concretely enough for implementation planning.
4. Keep scope sharp. Move cleanup or speculative enhancements out of scope.
5. Write acceptance criteria that can become plan deliverables.

## Output

Write only this section shape:

```markdown
## Product Reference Document

### Goal
- <outcome>

### Users
- <user or system actor>

### Scope
- <included behavior>

### Out of Scope
- <excluded behavior>

### Acceptance Criteria
- <observable acceptance criterion>

### Planning Handoff
- <constraint, dependency, or note for planner>
```

Do not write an implementation plan or task manifest. Preserve uncertainty
explicitly instead of silently broadening scope.
