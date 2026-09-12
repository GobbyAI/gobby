# Preserve section context (illustrative validation fixture)

**Plan ID:** research-context-example

## P1: Expansion
`kind: framing`

### 1.1 Preserve research in leaf descriptions [category: code]
`kind: deliverable`

Targets:
- `src/gobby/tasks/expansion/_contract.py::_build_contract_entry_work_task`
- `tests/skills/test_plan_research_context.py::test_example_preserves_research_in_leaf`

**Research context:**

Observed: `src/gobby/tasks/expansion/_contract.py::_build_contract_entry_work_task`
constructs the leaf description from the section body, then appends acceptance
items. Approximate line 244 is a navigation hint only.

Read-only helper: `src/gobby/tasks/expansion/_common.py::_contract_section_body`
omits the kind marker and stops at Acceptance, so research must precede it.
The caller `src/gobby/tasks/expansion/_contract.py::compile_plan_to_spec` emits
one leaf per manifest entry. The existing fixture factory
`tests/plans/test_symbol_targets.py::_write_plan` illustrates canonical Targets;
the focused test uses this Markdown fixture directly.

Approach: keep research as section prose and exercise the existing description
builder. A new task field or parser rule is rejected because the section body
already carries the information. No new production symbols are proposed.

Observed verification: source inspection confirms the helper's Acceptance
boundary; no test run is claimed by this fixture. Planned check:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/skills/test_plan_research_context.py -q`.
Expected: the canonical inventory contains only the two Targets above and the
leaf description contains this complete Research context, including read-only
references and approximate navigation hints.

**Acceptance:**

- 1.1.1 - Research remains in the leaf description. test: `tests/skills/test_plan_research_context.py::test_example_preserves_research_in_leaf`.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Preserve research in leaf descriptions
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: The complete research block survives in the leaf description.
  labels:
    - covers:research-context-example:1.1:1.1.1
  implementation_domain: backend
  tdd: false
  source_section: '1.1'
```
