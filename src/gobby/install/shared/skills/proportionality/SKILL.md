---
name: proportionality
description: Judge whether mechanism is proportionate to the goal. Flag complexity with no concrete consumer or stated requirement — speculative abstraction, indirection without payoff, single-value config, framework overkill. Ambition and size are never findings. Shared by plan, epic, and leaf review.
version: "1.0.0"
category: core
internal: true
triggers: over-engineering, proportionality, rube goldberg, yagni, premature abstraction, gold-plating
metadata:
  gobby:
    audience: all
    depth: 0
---

# proportionality — Anti-Rube-Goldberg Review Methodology

> Internal methodology skill; loaded with `get_skill(name="proportionality")` by
> `plan-adversary` (plan altitude), `holistic-reviewer` (epic altitude), and
> `qa-reviewer` (leaf altitude). Not a user-facing command.

This skill is the single source of truth for **one question, asked at every
review altitude**:

> Is the mechanism proportionate to the goal it serves?

It is altitude-agnostic on purpose. The same criterion serves a whole plan, a
whole epic, and a single leaf — only the *unit under review* changes. Forking a
per-surface copy would itself be the drift this skill exists to prevent.

## The criterion: justification, not minimization

**This is a justification test, never a minimization test.** You are not asking
"is this the smallest possible thing?" You are asking "does every piece of
machinery earn its place against a concrete consumer or a stated requirement in
the work under review?"

Hold both halves at once:

- **Ambition is welcome.** Size, scope, a large or complex epic, net-new
  capability, bold creative direction — these are **never findings on their
  own.** A 40-task epic that every task justifies is in perfect proportion. Do
  not punish reach.
- **Mechanism must be earned.** Flag only machinery with **no concrete consumer
  or stated requirement** *in the work being reviewed* — abstraction built for
  a caller that does not exist, indirection that buys nothing, a knob with one
  setting.

The failure mode you are hunting is the **Rube Goldberg machine**: elaborate
mechanism accomplishing a simple goal. Not "too big." **Too indirect for what
it does.**

### Justified complexity is retained, not flagged

Keep — never flag — complexity that serves a real need:

- Error handling for failure modes that actually occur.
- Structure that a stated requirement demands (a real second consumer, a real
  extension point named in the plan/epic).
- Concurrency, retries, validation, or observability tied to a real operational
  requirement.
- Extensibility that costs essentially nothing and has a named future consumer.

If you cannot name what is *gained* by removing the mechanism, do not flag it.

## Anti-pattern catalog

Each entry is mechanism that is suspect **until a concrete consumer is named**.
Presence is a prompt to apply the justification test, not an automatic finding.

1. **Premature abstraction.** A base class, interface, protocol, or generic
   layer with exactly one implementation and no second one in scope. The rule
   of three exists for a reason: abstract on the *third* occurrence, not the
   first.
2. **Abstraction for one consumer.** A registry, manager, service, factory, or
   strategy layer wrapping a single concrete case. A plain function or direct
   call would do the same work with fewer hops.
3. **Indirection without payoff.** Layers, wrappers, adapters, or event hops
   that add call-stack depth without adding capability, testability, or a
   boundary anyone needs. Count the hops from intent to effect.
4. **Config overdose.** Flags, options, profile fields, or environment knobs
   that ship with exactly one value and no requirement for a second. A constant
   is honest; a single-value knob is speculative surface.
5. **DRY for divergent cases.** Forcing two things that merely *look* alike into
   one shared path when their reasons-to-change differ. Coincidental duplication
   is cheaper than the wrong abstraction. Wait until the shape is proven.
6. **Framework overkill.** Building a plugin system, rule engine, mini-DSL, or
   generic pipeline where a direct implementation (or an existing Gobby utility)
   covers every case in scope. A new dependency where the codebase already has
   the tool is the same smell.
7. **Mocking / ceremony explosion.** Test or setup scaffolding far heavier than
   the behavior it pins — deep mock trees, fixtures-for-fixtures, indirection
   added "to make it testable" when the direct form was already testable.

## The justification test

For each suspect piece of mechanism, ask four questions:

1. **Concrete consumer?** Is there at least one *real* consumer of this
   mechanism in the work under review — not a hypothetical future one?
2. **Direct approach insufficient?** Is the simplest direct approach (a
   function, a constant, an inline call) *provably* unable to meet a stated
   requirement?
3. **Hop budget?** Does intent reach effect in ≤2–3 indirection hops?
4. **Explainable without the future?** Can you justify it using only what this
   plan/epic/leaf actually requires, with no appeal to "we might later…"?

If **any** answer is "no" → it is a finding. **Name the simpler alternative
explicitly** ("replace the `FooRegistry` with a module-level dict", "drop the
`enable_x` flag and inline the behavior", "delete the base class and keep the
one concrete class"). A proportionality finding that does not name the simpler
form is incomplete.

## Reporting a finding

- **Severity is about structure, not size.** Reserve blocking severity for
  structural Rube Goldbergs that should be simplified *before* the work
  proceeds (e.g., a speculative subsystem the rest of the work would build on).
  Use nit/advisory severity for ceremony and one-off knobs that are cheap to
  fix in place.
- **Always name the consumer that is missing** and **the simpler form that
  replaces the mechanism.** "This is over-engineered" is not a finding;
  "`PlanEnhancerStrategyRegistry` has one strategy and no second is in scope —
  inline it as a function" is.
- **When in doubt, do not flag.** A false over-engineering finding discourages
  legitimate ambition, which is the exact harm to avoid. Flag only what you can
  justify removing.

## Altitude notes

The criterion is identical at every altitude; the unit under review changes.

- **Plan altitude (`plan-adversary`).** Unit = a deliverable section. Flag a
  deliverable that builds a subsystem/registry/framework/abstraction with no
  consumer named anywhere in the plan, or config/flags/profile fields with a
  single value. Feeds the `over-engineering` finding category in `plan-review`.
  Ambitious-but-justified epics produce zero proportionality findings.
- **Epic altitude (`holistic-reviewer`).** Unit = the whole epic across its
  leaves. Watch for cross-leaf duplication that should have been one shared
  thing, frameworks introduced by one leaf and used by none, and product
  behavior no plan section asked for. This is the same criterion the
  `yagni`/proportionality dimension carries.
- **Leaf altitude (`qa-reviewer`).** Unit = a single leaf's diff. Flag a leaf
  that introduces an abstraction, config knob, or indirection layer
  disproportionate to its single task, and name the simpler form. This is a
  `code_quality`-tier observation; it does **not** gate `spec_compliance` — a
  correct leaf that is mildly over-built is a quality note, not a rejection.

## Boundaries

- Proportionality is one criterion shared by three reviewers. Do not fork a
  per-surface copy — that fork is the drift this skill prevents.
- This skill judges *mechanism vs. goal*. It never judges ambition, size, or
  net-new scope, and it never weakens correctness, security, or required
  testing — those gates belong to the host review (`plan-review`,
  `holistic-review`, the QA spec-compliance tier) and always win.
- Default to *not* flagging when justification is plausible. Under-flagging
  protects creativity; over-flagging punishes it.
