# Targets and consumers

### Authoring Scope: Narrative Only — Never the Manifest

Planner authors **narrative sections only**: `# {Epic Title}`, `## Overview`,
`## Constraints`, `## P<N>: {Phase Name}`, `### N.M {Task Title}` deliverables
with `**Acceptance:**` blocks, `framing` / `verification` / `deferred` sections,
and the `## V1 Plan Changelog` rolling summary (per §2.23). Stop there.

The `## M1 Task Manifest` section is not part of the first-draft narrative
surface. It is written by the approving adversary or the interactive plan
coordinator after final user approval. If a draft includes a manifest, it must
pass expansion-mode validation.

Why this split: the planner's job is to fill holes in narrative; the
adversary's job is to commit to a typed, expansion-ready bridge between the
plan and the leaves. Mixing those concerns is what produced the long-context
drift §2.23 fixes. Leave the manifest to the adversary.

See `docs/contracts/plan-coverage.md` (§ "Task Manifest") for the schema and
the adversary-writes-on-approval contract.

### Table-Row Decomposition

Any `deliverable` section whose body uses a markdown table to enumerate work
items MUST emit one acceptance item per table data row with stable IDs, for
example `A7.4.1`, `A7.4.2`, and `A7.4.3`. Plan-adversary qualitatively rejects
deliverables with fewer acceptance items than table data rows. This rule closes
the missing-section failure mode from #12725.

### Target Inventory Block Format

Every deliverable declares the files and indexed symbols it changes in a
`Target:`/`Targets:` inventory before `**Acceptance:**`. Targets are the
canonical scope contract; acceptance artifact parsing stays unchanged.

Use one of these forms:

```markdown
Targets:
- `path/to/file.py::Class.method`
- `path/to/file.rs::Type::method`
- `path/to/file.py::*` — scope-reason: cross-cutting rewrite of every handler
- `path/to/new_file.py`
```

- Exact references must equal gcode's indexed `qualified_name` in the declared
  file. Parsing splits only the first `::`, so Rust `Type::method` remains one
  qualified name.
- A fresh indexed file containing symbols requires one or more exact references
  regardless of task category. A justified `::*` reference is the only
  file-wide alternative.
- Every `::*` carries `scope-reason: <non-empty explanation>` on the same line.
- A bare path is valid only when the fresh project index has no symbol-bearing
  record for that path, including a genuinely new file.
- Multiple exact symbols may target one file. Never mix exact references with
  `::*` for the same file.
- Never put gcode symbol UUIDs or line numbers in Targets. UUIDs depend on byte
  offsets; qualified names are the durable contract.

Resolve Targets before writing broader implementation prose:

1. Run `gcode search-symbol "<symbol>" path/to/file` for each existing target
   file.
2. Copy the exact displayed `qualified_name` with its file path into Targets.
3. Resolve every directly changed symbol first. Then use `gcode usages` or
   `gcode blast-radius` to discover consumers and add owned consumer targets.
4. Run `uv run gobby plans validate <plan-file>` after the final edit. Explicit
   project validation and expansion require an available fresh index.

The `target-coverage` lint fails the section when a concrete path appears in
the body after a change-intent verb (`add`, `create`, `delete`, `edit`,
`expose`, `extract`, `implement`, `modify`, `move`, `refactor`, `register`,
`remove`, `rename`, `replace`, `split`, `touch`, `update`, `wire`) or in a
`file:`/`behavior:` acceptance ref without a matching inventory entry.

The block is contiguous. **A blank line ends it**, so entries must directly
follow the `Targets:` line:

```markdown
Targets:
- `src/module/file.py::Example.validate`
- `tests/test_module.py::test_validate_empty`
```

Not this — the blank line ends the block, and the bullets are invisible to the
lint even though they read as an inventory:

```markdown
Targets:

- `src/module/file.py`
```

Matching is basename-aware in one direction: a mentioned path containing `/`
must match a target entry exactly, while a bare filename matches any target
sharing that basename. A bare extension such as `.tsx` is not a path and needs
no entry. The full rule lives in `docs/contracts/plan-coverage.md`, "Target
Inventory". Expansion preserves the complete Targets block in the section body
used as the leaf task description; no separate task field carries symbol scope.

### Whole-Plan Sweep After Findings

After any adversary finding, fix the cited instance and then sweep the whole
plan for the same finding class before resubmitting. If one missing `Target:`
file is found, scan every deliverable for body paths and acceptance `file:` /
`test:` refs missing from its Target/Targets inventory. If one missing consumer
file is found, use code-index usages/blast-radius for the changed symbol or
file and add all direct consumer files that the deliverable owns.

---
