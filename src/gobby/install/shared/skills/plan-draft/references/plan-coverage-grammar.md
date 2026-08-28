# Plan-Coverage grammar

## Plan-Coverage Contract Grammar

New epic plans that will be expanded into implementation leaves MUST follow the
typed Plan-Coverage Contract. The contract lets the parser, expansion QA, and
close-time coverage gate prove that every accepted item becomes covered work.

The canonical heading regex is pinned verbatim here and in the parser:

```regex
^#{2,6}\s+(?:§\s*)?(?P<section_id>(?:\d+[a-z]?|[A-Z]+[0-9]+[a-z]?)(?:\.(?:\d+[a-z]?|[A-Z]+[0-9]+[a-z]?))*)(?=\s|[).:-]|$)
```

Every section heading at level `##` through `######` carries a first non-blank
front-matter line:

```markdown
`kind: deliverable | framing | verification | deferred`
```

Section kind rules:

- `deliverable` sections describe concrete implementation work and MUST carry
  an `**Acceptance:**` block with one or more numbered acceptance items.
- `framing` sections provide context, scope, or non-goals and carry no
  acceptance items.
- `verification` sections summarize end-to-end acceptance and carry no
  acceptance items of their own.
- `deferred` sections are out-of-scope for this epic and MUST carry a typed
  deferral object pointing at a real open task.

Acceptance item IDs are formed by appending `.<n>` to the section's own ID.
The section ID is the prefix verbatim — **no synthetic letter is added** for
purely numeric sections. The parser enforces this with
`item_id.startswith(f"{section_id}.")` in `_build_acceptance_item`
(`src/gobby/plans/parser.py`).

- Section `A1` (letter-prefixed) emits `A1.1`, `A1.2`, … The `A` is part of
  the section ID, not a prefix added to items.
- Section `A1.7` emits `A1.7.1`, `A1.7.2`, …
- Section `1.1` (purely numeric) emits `1.1.1`, `1.1.2`, … Items like
  `A1.1.1` for section `1.1` are rejected by the parser because `A1.1.1`
  does not start with `1.1.`.

When the contract uses the shorthand `A<section>.<n>`, read it as
"section ID dot n" — for numeric sections the result has no `A`.

```markdown
**Acceptance:**  (under section `A1`, letter-prefixed)

- A1.1 - <prose>. file: `src/module.py`.
- A1.2 - <prose>. symbol: `gobby.module.SomeType`.
- A1.3 - <prose>. test: `tests/test_module.py::test_behavior`.
- A1.4 - <prose>. behavior: "documented behavior" in `docs/contract.md`.
```

```markdown
**Acceptance:**  (under section `1.1`, purely numeric)

- 1.1.1 - <prose>. file: `src/module.py`.
- 1.1.2 - <prose>. symbol: `gobby.module.SomeType`.
```

Each acceptance item MUST name at least one concrete artifact reference. Valid
artifact kinds are `file`, `symbol`, `test`, and `behavior`. The parser uses
the first artifact reference as the canonical coverage artifact; additional
references in the same item are informational.

Deferred sections carry this typed deferral object:

```yaml
deferral:
  task_ref: "#12345"
  reason: "Why this work is outside the current epic."
  owner: "team-or-agent"
  original_acceptance_items:
    - A7.3
```

The referenced task must be open, and it must carry provenance label
`deferred-from:<plan-id>:<section-id>`. A closed task fails the gate.

Work gated on anything outside the plan — another epic, plan, or task — MUST be
a `kind: deferred` section, never a prose blocker or a manifest dependency:
manifest `depends_on` cannot encode external tasks, so prose-only sequencing is
unenforceable. The deferral task is parented under the plan's own epic as tail
work and carries the real edges (`blocked-by` on each external prerequisite plus
any internal-leaf dependencies). Create it at expansion or finalization, never
mid-draft or mid-review; a dangling `task_ref` in an unfinalized plan is
expected and passes base validation. Full rules:
`docs/contracts/plan-coverage.md` §Deferrals.

Expansion leaves MUST emit structured coverage records in this exact shape:

```text
covers:<plan-id>:<section-id>:<item-id>
```

Free-form `plan-ref:` labels are not honored by the coverage contract.
