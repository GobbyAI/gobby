# Lightweight planning

## Lightweight Workflow

Load `plan-draft` and use its Plan-Coverage Contract as formatting guidance:

```text
get_skill(name="plan-draft") on gobby-skills
```

Write the decision-complete plan to `.gobby/plans/<slug>.md`. Keep concrete
deliverables, dependencies, file or subsystem targets, validation, risks, and
explicit out-of-scope boundaries, then run base validation:

```bash
uv run gobby plans validate <plan-file>
```

Lightweight skips enhancement and adversarial review by default. After the
validated draft, use the checkpoint menu below. `continue interactively`
continues plan refinement and returns to the same menu after validation. The
user may explicitly opt into either Full phase later without redrafting.
