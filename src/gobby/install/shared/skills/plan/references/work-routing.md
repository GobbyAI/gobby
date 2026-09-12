# Work routing

## Work Routing

### Atomic implementation task

Choose this route only when investigation finds one independently closeable
deliverable expected to fit one focused agent session. Write a task-ready contract
with:

- one outcome and concrete scope, including known files or subsystems;
- observable acceptance and validation criteria;
- task type, category, and implementation domain where required;
- dependencies, risks, and explicit exclusions.
- proportional Research context: observed behavior and file-qualified symbols,
  relevant consumers/helpers/fixtures, chosen approach and consequential rejected
  alternatives, and focused commands with observed versus planned outcomes.
  Label new symbols and optional approximate line hints; keep hints outside Targets.

Load the existing `tasks` workflow and hand the contract to that workflow as the
real implementation task. Create no plan file, plan registry row, manifest, or
planning task for this route. Do not load `plan-draft`. If a real implementation
task already exists, refine or claim that task instead of duplicating it.

Before handoff, run a mechanism audit: identify every new subsystem, dependency,
abstraction, configuration surface, and paid-operation loop. Remove each mechanism
that is unnecessary for complete acceptance coverage. Preserve mechanisms required
for correctness, security, or an explicit acceptance case.

### Dependent-deliverables plan

Choose the plan route when two or more independently closeable deliverables have
ordering, handoff, or validation dependencies. Route bugs, maintenance, features,
refactors, migrations, and documentation by this same dependency boundary. Work
kind, breadth, risk, and elapsed-time estimates inform the plan's rigor; none
replaces the deliverable graph.

Load `plan-draft`, then use **Plan Drafting and Staging**. Do not create a planning
task merely to make the request look plan-shaped.
