> **Plan ID:** routing-bridge

## P1 Overview
`kind: framing`

This fixture exercises the manifest-routing bridge: the deliverable's prose
contains the word "frontend" so the legacy regex would route to
`frontend-developer`, but the manifest pins the section to a different agent.

## P2 Implementation
`kind: framing`

### 2.1 React UI Panel [category: code]
`kind: deliverable`

Build a React frontend panel rendering dispatcher state.

**Acceptance:**
- 2.1.1 - The React UI renders. file: `web/src/Panel.tsx`

## M1 Task Manifest
`kind: manifest`

```yaml
- title: "Build React UI Panel"
  category: code
  implementation_domain: backend
  task_type: feature
  depends_on: []
  validation_criteria: "web/src/Panel.tsx exists"
  labels:
    - "covers:routing-bridge:2.1:2.1.1"
  assigned_agent: backend-developer
  tdd: true
  source_section: "2.1"
```
