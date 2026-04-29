> **Plan ID:** expansion-compile-regression

## P1 Backend Foundation
`kind: framing`

### 1.1 Dispatcher Schema [category: code]
`kind: deliverable`

Add the storage shape that the dispatcher uses to find work and persist state.

**Acceptance:**
- 1.1.1 - Dispatcher state is stored in a narrow schema. file: `src/gobby/dispatch/schema.py`
- 1.1.2 - Schema behavior is covered by unit tests. test: `tests/dispatch/test_schema.py`

### 1.2 Mutex Lease [category: code] (depends: 1.1)
`kind: deliverable`

Implement the lease helper that protects each task from duplicate dispatch.

**Acceptance:**
- 1.2.1 - Lease acquisition is atomic. file: `src/gobby/dispatch/mutex.py`
- 1.2.2 - Expired leases are recovered. test: `tests/dispatch/test_mutex.py`

### 1.3a Operator Runbook [category: docs] (depends: 1.2)
`kind: deliverable`

Document how operators diagnose dispatcher stalls.

**Acceptance:**
- 1.3a.1 - The runbook lists recovery commands. file: `docs/dispatcher-runbook.md`

## P2 User Surfaces
`kind: framing`

### 2.1 Dispatcher React UI [category: code] (depends: 1.3a)
`kind: deliverable`

Build a frontend panel that shows dispatcher state in the browser.

**Acceptance:**
- 2.1.1 - The React UI renders active leases. file: `web/src/components/DispatcherPanel.tsx`
- 2.1.2 - Browser interactions are tested. test: `web/src/components/DispatcherPanel.test.tsx`

### 2.2 CLI Status Command [category: config] (depends: 1.2)
`kind: deliverable`

Wire the CLI command that prints dispatcher status.

**Acceptance:**
- 2.2.1 - CLI configuration exposes dispatcher status. file: `src/gobby/cli/dispatch.py`

## P3 Verification
`kind: framing`

### 3.1 End-to-End Coverage [category: test] (depends: 2.1, 2.2)
`kind: deliverable`

Add an end-to-end regression around the full dispatch cycle.

**Acceptance:**
- 3.1.1 - The dispatch cycle is covered end to end. test: `tests/e2e/test_dispatcher_cycle.py`

## M1 Task Manifest
`kind: manifest`

```yaml
- title: "Dispatcher Schema"
  category: code
  task_type: task
  depends_on: []
  validation_criteria: "Dispatcher schema acceptance is satisfied."
  labels:
    - "covers:expansion-compile-regression:1.1:1.1.1"
    - "covers:expansion-compile-regression:1.1:1.1.2"
  assigned_agent: backend-developer
  tdd: true
  source_section: "1.1"
- title: "Mutex Lease"
  category: code
  task_type: task
  depends_on:
    - "1.1"
  validation_criteria: "Mutex lease acceptance is satisfied."
  labels:
    - "covers:expansion-compile-regression:1.2:1.2.1"
    - "covers:expansion-compile-regression:1.2:1.2.2"
  assigned_agent: backend-developer
  tdd: true
  source_section: "1.2"
- title: "Operator Runbook"
  category: docs
  task_type: task
  depends_on:
    - "1.2"
  validation_criteria: "Operator runbook acceptance is satisfied."
  labels:
    - "covers:expansion-compile-regression:1.3a:1.3a.1"
  assigned_agent: backend-developer
  tdd: false
  source_section: "1.3a"
- title: "Dispatcher React UI"
  category: code
  task_type: task
  depends_on:
    - "1.3a"
  validation_criteria: "Dispatcher React UI acceptance is satisfied."
  labels:
    - "covers:expansion-compile-regression:2.1:2.1.1"
    - "covers:expansion-compile-regression:2.1:2.1.2"
  assigned_agent: frontend-developer
  tdd: true
  source_section: "2.1"
- title: "CLI Status Command"
  category: config
  task_type: task
  depends_on:
    - "1.2"
  validation_criteria: "CLI status command acceptance is satisfied."
  labels:
    - "covers:expansion-compile-regression:2.2:2.2.1"
  assigned_agent: backend-developer
  tdd: true
  source_section: "2.2"
- title: "End-to-End Coverage"
  category: test
  task_type: task
  depends_on:
    - "2.1"
    - "2.2"
  validation_criteria: "End-to-end dispatch acceptance is satisfied."
  labels:
    - "covers:expansion-compile-regression:3.1:3.1.1"
  assigned_agent: backend-developer
  tdd: true
  source_section: "3.1"
```
