## Research Findings

Task #25809 — Research: Gauntlet Loop vs Gobby's developer/reviewer and attestation process.
Researcher: gobby#20446. Date: 2026-11-06, America/Chicago.

### Research Questions

- What do the two upstream implementations prescribe, including stopping and cost?
- What does Gobby already provide, and where would integration conflict with delivery controls?
- Should Gobby adopt ideas, ignore the method, or run a thin experiment?

### Domain Context

- **Recommendation: thin-experiment.** Gobby already has the builder/reviewer/delivery structure. Test external artifact comparison for quality that acceptance tests miss. No new scheduler, pipeline, seat or landing mechanism is justified yet.
- Read-only research; no upstream installer or loop was executed and no implementation changed.
- Triage relayed the Orchestrator's clarification on 2026-11-06: “build-review-promote” means dev-team itself, with exact-SHA code/security review, authorized landing, then closure. There is no separate factory artifact. The older sprint proposal below is historical context.

### Evidence & Sources

#### What Gauntlet Loop does

**Original: prompt generation.** `robonuggets/gauntlet-loop`, commit
`9b1975a1b8f01981f3f1e6b667ad3aaf907178ea`, contains one skill and no execution
scripts. Its [SKILL.md](https://github.com/robonuggets/gauntlet-loop/blob/9b1975a1b8f01981f3f1e6b667ad3aaf907178ea/.claude/skills/gauntlet-loop/SKILL.md),
“Flow,” “The bar is the whole trick,” and “Prompt template,” offers references for
the user to choose, then emits a short prompt for a fresh session. References must
be specific, obtainable and suitable for comparison. The generated instructions
split the goal into judgeable parts, use a builder and a separate fresh-context
critic for each, inspect actual output, hide authorship in A/B comparisons and
return the largest gap. The exit is a critic win or user stop. A budget appears
only when the user supplies one; there is no default ceiling. This is prompt-level
orchestration, without executable enforcement.

**Port: execution guidance.** `emernuness/gauntlet-loop-skill`, commit
`135d6c9c41981e68210c8c6da28911d817a3aac3`, changes that contract in
[gauntlet-loop/SKILL.md](https://github.com/emernuness/gauntlet-loop-skill/blob/135d6c9c41981e68210c8c6da28911d817a3aac3/gauntlet-loop/SKILL.md),
“Execution contract” and runbook steps 2–9. It chooses the reference and budget,
executes immediately, routes workers and records progress in a file. It allows an
unreachable reference and raises a bar cleared on round one. Its explicit run
stops are user satisfaction, negligible gains or budget exhaustion, so a win is
not a guaranteed termination condition. Three rounds preserving the same gap
trigger lead reassessment. It adds a whole-artifact smoothing pass. The two repos
therefore have different stopping and cost policies. The port's silence and
automatic decisions would require adaptation to Gobby's coordination boundaries.

**Critic mechanics.** The port's
[references/critic-protocol.md](https://github.com/emernuness/gauntlet-loop-skill/blob/135d6c9c41981e68210c8c6da28911d817a3aac3/gauntlet-loop/references/critic-protocol.md)
requires neutral filenames, randomized order, matching comparison conditions,
inspection evidence and a binary winner; ties lose. The critic gets the goal,
constraints, reference, artifact and previous critique, excluding the builder's
explanations and effort. It warns that changing criteria or rejecting personal
preferences stalls a loop. Its same-context fallback simulates independence and
expects leniency. Gobby should retain actual independent review.

**Cost.** The port's
[references/model-routing.md](https://github.com/emernuness/gauntlet-loop-skill/blob/135d6c9c41981e68210c8c6da28911d817a3aac3/gauntlet-loop/references/model-routing.md)
uses commands for deterministic checks, small workers for specified edits, midrange
workers for ordinary building and strong models for judgment/novelty. It separates
model selection from reasoning effort. Suggested spend is at most 10% setup, about
55% building, 25% protected critic reserve and 10% smoothing/closure. These are
guidelines, not a metered limiter. For P pieces and R rounds the simple pair
pattern takes approximately 2PR worker invocations before setup and integration:
8 pieces × 4 rounds means 64. Actual dollars depend on tokens, prices, caching and
failed rounds. Neither repo establishes measured savings or quality gains against
Gobby. Repeated losses change effort/model, and three same-gap losses require
structural reassessment.

**Scripts read.** The original's recursive Git tree has no scripts. The port's
[install.sh](https://github.com/emernuness/gauntlet-loop-skill/blob/135d6c9c41981e68210c8c6da28911d817a3aac3/install.sh)
copies the skill into harness directories, detects existing installs and optionally
replaces them. [scripts/build-zip.sh](https://github.com/emernuness/gauntlet-loop-skill/blob/135d6c9c41981e68210c8c6da28911d817a3aac3/scripts/build-zip.sh)
packages the folder. Neither executes critics, meters spend, enforces isolation,
transfers claims or lands code. Additional files read at the same commit:
[references/harness-setup.md](https://github.com/emernuness/gauntlet-loop-skill/blob/135d6c9c41981e68210c8c6da28911d817a3aac3/gauntlet-loop/references/harness-setup.md),
[references/quality-bars.md](https://github.com/emernuness/gauntlet-loop-skill/blob/135d6c9c41981e68210c8c6da28911d817a3aac3/gauntlet-loop/references/quality-bars.md)
and [assets/delegation-templates.md](https://github.com/emernuness/gauntlet-loop-skill/blob/135d6c9c41981e68210c8c6da28911d817a3aac3/gauntlet-loop/assets/delegation-templates.md).
These supply artifact-inspection and delegation examples. Their provider capability
and ranking claims were not independently tested here.

#### Mapping to Gobby

| Gauntlet mechanism | Gobby evidence and implication |
| --- | --- |
| Builder plus critic | Developer produces evidence; code-reviewer independently evaluates the exact candidate and every criterion. Already present in `agents/developer.yaml`, `agents/code-reviewer.yaml` and G1. |
| Blind critic | Gobby reviewers receive task, diff and authenticated evidence. They are independent authors but know candidate identity. A separate artifact A/B pass could reduce presentation bias; code/security review still needs provenance and context. |
| External quality bar | Validation criteria establish obligations. A frozen reference can supplement them; it cannot replace correctness or security. Numeric backend bars already fit normal criteria. |
| Split into small pieces | Tasks/leaves and admitted team worktrees provide owned boundaries. Free fan-out must respect task/path authority. A local worker does not acquire independent claim or review authority by being spawned. |
| Repeat on loss | Claim flow returns rejection to implementation, followed by fresh evidence. It has no universal N-round exit. Project policy replaces a developer after two rejected candidates on the same task: an escalation threshold, not permission to ship failed criteria. |
| Single largest gap | Useful prioritization. Gobby must still list all blocking findings and cover every criterion; reporting only one could conceal defects. |
| Cost/model routing | Project policy already supplies role defaults; the pipeline accepts launch overrides. Borrow protected review capacity and evidence-driven escalation without bypassing admission, denylist, load holds or launch authority. |
| Smoothing | Integration and post-land validation already exist. Smoothing code changes require an authorized developer claim, a new candidate and fresh review. A win on old screenshots cannot approve a new SHA. |
| Stop and deliver | `land_commit` requires a linked full SHA, capability and scope; selected rules enforce required review/security and landing freezes. Lead validates the integrated SHA and closes. Budget exhaustion or a visual win cannot substitute for gates. |

Installed registry reads on 2026-11-06 confirmed enabled `dev-team` version 7.0.0,
enabled `code-reviewer` version 3.1.0 with independent-review/validation authority,
and enabled `closure-requires-independent-review`. These are configuration
observations, not a landing test. The rule inspector reports `reason` unavailable
in its inspection context; this alone does not prove its event-scoped expression
broken. `project_policy.role_guidance.dev-team-lead` supplied the two-rejection
replacement policy. The `gobby-tasks-ops:land_commit` schema was read, never invoked.

#### Integration fit

**Build-review-promote.** G1–G3 establish the claim/evidence flow and independent
candidate review. Gauntlet can contribute additional quality evidence inside it.
A visual preference win answers a different question from whether a commit meets
the task and can safely land. No new engine is needed to test comparison prompts.

**Historical sprint.** `.gobby/plans/completed/task-25352.md:19-39,95-124,138-153`
describes drain to 0.5.0 plus wiki, local ultracode building, independent batch
review of exact commits, a ledger, then flip/smoke. It proposed explicit claim/TDD
ceremony waivers while preserving criteria and close gates. Those proposal
provisions do not authorize a new skill to bypass today's controls. The useful
overlap is batched work, inspectable evidence, independent review and controlled
promotion (G4).

**Ultracode forward plan.** The approved map has 133 dependency-ordered chunks,
self-contained briefs, checkpoint reviews and controlled cutovers. Its rulings use
local default-agent chunk sessions, per-chunk adversarial work and landing on
0.9.0. Flexible implementation already fits inside a decided contract. Gauntlet's
emergent decomposition must not silently rewrite chunk ownership, parity
obligations, dependency order or checkpoints (G5–G6).

**Worktree drain.** Map approval waits until worktrees other than 0.5.0 and wiki
are resolved and the tmux purge completes (G6). This is a start gate, not a ban on
later task worktrees. New broad fan-out would compete with the drain. Prefer an
already-authorized artifact task after the gate, or reference-only evaluation
creating no extra worktree. This report does not assert that the gate has cleared.

**Fit judgment.** Freeze reference bytes and scenarios before building. Keep
deterministic tests for pass/fail properties and use blind judgment for residual
comparative quality. Never raise acceptance merely because the first candidate
wins. Preserve all blocking findings and delivery controls.

#### Recommendation

**thin-experiment**, with the operator decision routed through the Assistant.
This report does not launch or authorize the experiment.

- Use three already-authorized, low-risk UI or documentation briefs after the
  drain permits it. For each brief, create two variants from the same starting
  artifact: normal review versus normal review plus blind reference comparison.
  Randomize assignment to comparable builders and keep each variant's history
  separate. The identical brief and initial state make the pair comparable;
  treat this as a feasibility pilot.
- Freeze goals, constraints, reference hashes, viewport/input scenarios, comparison
  question and allowance before work. Both groups retain mandatory criteria,
  including accessibility, security and correctness. Use existing authorized seats
  and task scopes; no new orchestration mechanism.
- Give a fresh comparison context neutral A/B artifacts in randomized order,
  excluding builder narrative. Record artifact hashes, SHA, verdict, largest gap,
  elapsed time and tokens/actual cost when available. Ordinary review retains
  provenance, all blockers and exact-SHA approval.
- Bound each piece at the lower of 90 minutes or twice its matched baseline token
  allowance, reserving 25% for judgment/final assessment. Reassess a repeated gap
  after two losses; retain Gobby's developer replacement policy for rejected
  candidates. Budget or plateau stops leave unmet work open. These limits
  deliberately differ from the original uncapped protocol.
- Have an independent final assessor compare treatment and baseline artifacts
  blind. Measure preference wins, unmet criteria, regressions, rounds, tokens,
  time and total review effort. Repeat one pair with order swapped to detect
  positional instability. Record recognizable references as a blinding limitation.
- Continue only if at least two of three treatment pieces beat their matched
  baseline, no mandatory criterion regresses, and median total tokens/time remain
  within 2× baseline. These are proposed screening thresholds, not proven economic
  targets. A pass justifies a larger trial; failure ends the pilot.

Test frozen external references, real-artifact inspection, fresh comparison context
and visible cost/gap history. Preserve claim_flow, independent review, security,
landing receipts and authorized closure. Exclude the port's broad auto-trigger,
silent execution, automatic product decisions, moving bar and simulated critic
independence.

**Repository evidence register.** Gcode evidence was read at HEAD
`f8cde0292e834c53307f49e4217e71e05f712100`; the ranges below were complete.
Hashes are `excerpt_hash`. External upstream bytes were fetched at the pinned
commits above on 2026-11-06; Gobby's local index does not cover those repos.

| ID | Path and range | Verified line anchor | Excerpt hash |
| --- | --- | --- | --- |
| G1 | `src/gobby/install/shared/workflows/pipelines/dev-team.yaml:257-301` | `276: on_rejected: implement`; `280: - validation`; `281: - closing` | `a780cd40f4dcba587ae825f0c38122fb08cece6d3690de3c32f8e8467f62c156` |
| G2 | `src/gobby/tasks/claim_flow.py:314-386` | line 358 requires one reference across done_when evidence; line 381 calls claim_task_for_agent | `ae9417076ada7bc0f80e8d74281b3cb571d733a718b59f50780f772b0dc9eb1e` |
| G3 | `src/gobby/install/shared/workflows/rules/task-closure/independent-review.yaml:1-31` | lines 11–19 check independent author and candidate/landing receipt; line 24 declares landing-requires-independent-review | `c8b64d905c71036b800a45600ec10259320491a520ef653ff73ec6b453e38235` |
| G4 | `.gobby/plans/completed/task-25352.md:1-160` | lines 19–39 set drain/build/review/flip/smoke; lines 106–110 preserve criteria and exact-commit review | `cd274fec1a75074717370bd188bc8884e4c757a3bcd929cbc33f1eb3c3b1b8ca` |
| G5 | `.gobby/plans/research/1.0-ultracode-chunk-map-2026-11-03/rulings.md:1-38` | lines 9–15 specify local default sessions, 0.9.0 and adversarial workflow | `03d58e82b417ddc12a1c6cf0550e4fe4e580122f2646032c2d388c2e44f3457f` |
| G6 | `.gobby/plans/research/1.0-ultracode-chunk-map-2026-11-03.md:1-25` | line 8 records worktree/purge approval conditions; line 10 records 133 chunks | `11b1e53d58eff3febed3cac973eb3dba07fc8c8a6fb0797fade03561081f41fe` |

Additional configuration sources read: `src/gobby/install/shared/workflows/agents/developer.yaml`
(Role, Evidence and claim flow, Completion), `src/gobby/install/shared/workflows/agents/code-reviewer.yaml`
(Verdict and claim flow), `src/gobby/install/shared/workflows/agents/dev-team-lead.yaml`
(Validate and complete), plus the installed tools/policy observations above. These
declare the process; runtime enforcement was not exercised.

**Validation criteria coverage.** Criterion 1: all four requested named sections
are present. Criterion 2: upstream file links pin commits; Gobby claims cite paths,
verified ranges and registry observations. Criterion 3: thin-experiment is explicit
with measurements and screening thresholds. Criterion 4: commit only this report
on 0.5.0, link its SHA and deliver path/SHA/summary through Triage to the
Orchestrator; completion evidence and independent approval follow the report.

### Risks

- Binary choices remain stochastic and may reward superficial similarity. Neutral
  filenames cannot hide every recognizable reference. Hashes make inputs
  reproducible, not judgments deterministic.
- Unreachable/moving bars can sustain endless improvement; plateau is subjective.
  Budget limits spend but does not establish correctness.
- These repos provide instructions and examples, not controlled evidence that the
  method outperforms Gobby. Public demonstrations do not establish causal benefit
  or cost efficiency. No new fleet mechanism is justified yet.
- A small artifact pilot cannot establish suitability for daemon lifecycle,
  security, Rust parity or destructive cutovers.
- Historical proposals and forward-plan approval do not establish live state.
  Installed definitions and actual admission remain authoritative.
