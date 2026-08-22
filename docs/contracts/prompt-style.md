# Prompt Style Contract

The style contract for every LLM-facing instruction surface in Gobby: agent
definition YAMLs (`prompts.persona`/`prompts.agent`/`status_message`), prompt
templates under `src/gobby/install/shared/prompts/` and `src/gobby/tasks/prompts/`,
config-embedded prompt strings, and skill bodies. Apply it when authoring or
reviewing any of these.

Why it exists: Claude 5-era models (Opus 4.5+, Fable 5) treat instructions as
signal, not noise. Aggressive emphasis (ALL-CAPS `NEVER`/`ALWAYS`, stacked
`CRITICAL`) causes overtriggering — the model over-applies the emphasized rule at
the expense of the task. Verification and anti-laziness exhortations ("be
thorough", "double-check your work") cause over-verification loops. Persona
flattery ("you are a senior architect") adds tokens without capability. Bare
negations underperform imperatives that carry their reason. The contract turns
those findings into review rules.

## Rules

1. **Plain imperatives with reasons.** State what to do and why in one breath:
   "Use `cargo test -p <package>` — the full workspace suite takes 30+ minutes."
   The reason is what lets the model generalize correctly to cases the rule
   didn't anticipate.
2. **Positive phrasing.** Convert prohibitions into the action wanted instead.
   "Do NOT call close_task" becomes "Leave task closure to the orchestrator" —
   same constraint, plus the model now knows what the correct move is. Keep a
   prohibition only when no positive form exists, and attach its reason.
3. **Emphasis budget: at most 3 bolded rules per file, reserved for
   destructive-risk rules.** Bold is a scarce resource; spend it where a miss
   destroys work (data loss, foreign uncommitted files, full-suite runs).
   Everything else is prose. No ALL-CAPS emphasis words.
4. **Keep prompt surfaces independent.** `prompts.persona` carries concise
   interactive domain guidance and working style. `prompts.agent` carries
   automated-run instructions, assigned-task lifecycle, stage transitions,
   messaging, and `end_agent_run`. Experience flattery ("you are a senior X
   with 10 years…") contributes no capability on either surface. The Gobby
   product voice is deliberate identity and lives only in the `default` and
   `comms-agent` definitions and the chat assembly path.
5. **No verification or anti-laziness exhortations.** Drop "be thorough", "be
   critical", "do not approve without a re-check pass", "make sure you
   really…". State the check itself once; the model runs it. Adversarial *role
   framing* (skepticism is the reviewer's job) stays — exhortation stacking on
   top of it goes.
6. **Machine-checkable gates stay verbatim.** Exact commands, tool names, JSON
   output contracts, marker strings, and lifecycle call sequences are parsed by
   code or matched by rules. Rewriting them is a behavior change, not a style
   change. When in doubt whether a string is load-bearing, it is.
7. **Collapse enumerations that restate model defaults.** A numbered list
   telling the model to read the task, understand it, then implement it is
   steering the model already does. Keep enumerations only where the order or
   the content is genuinely non-obvious (lifecycle sequences, output schemas).

## Before / after examples

All examples are taken from the fleet as it existed before this contract.

### 1. Negation list → reason-backed imperatives (`agents/analyst.yaml`)

Before:

```text
CRITICAL RULES:
- Do NOT call close_task.
- Do NOT call reopen_task or de_escalate_task.
- Do NOT call complete_stage.
- Do NOT spawn other agents.
- Always use `uv run` for Python commands.
```

After:

```text
Boundaries:
- Your deliverable is the research brief; task transitions (close_task,
  reopen_task, de_escalate_task, complete_stage) belong to the dispatcher,
  which validates your marker block and completes the stage.
- Work alone — spawning agents from a research stage multiplies cost without
  adding evidence.
- Run Python through `uv run`; the system Python lacks the project deps.
```

Same constraints, no caps header, and each rule now explains itself — the model
can apply the boundary correctly in situations the list never enumerated.

### 2. Exhortation stack → the check itself (`agents/plan-adversary.yaml`)

Before:

```text
Follow `plan-review`'s heuristics EXACTLY — attitude, method, traceability,
... Do not manufacture findings. Do not approve without a re-check pass.
```

After:

```text
Follow `plan-review`'s heuristics — attitude, method, traceability, ...
Every finding cites the plan section that triggered it. Before an approval
verdict, re-read the sections your findings touched and confirm each repair
landed.
```

The adversarial role framing stays (skepticism is the job). The caps emphasis
and the bare "re-check pass" exhortation become the concrete check the reviewer
actually runs.

### 3. Persona opener → task-first opener (`tasks/prompts/expand-task.md`)

Before:

```text
You are a senior technical project manager and architect.
Your goal is to break down a high-level task into clear, actionable, and
atomic subtasks.
```

After:

```text
Break the task below into clear, atomic subtasks that one agent can complete
in one session each.
```

The persona line bought nothing; the JSON field table below it is the
load-bearing contract and stays byte-identical (rule 6).

### 4. A prohibition that keeps its emphasis (`agents/backend-developer.yaml`)

Before and after — this one survives, with its reason attached:

```text
- **Never run the full test suite** — it takes 30+ minutes and starves the
  worker pool. Scope Rust to `cargo test -p <package>` or
  `cargo test <name> -p <package>`.
```

Destructive-risk rules (full-suite runs, foreign uncommitted files, force
pushes) are what the emphasis budget is for. One bolded rule, its reason, and
the positive alternative in the same breath.

## Review checklist

When reviewing an instruction surface against this contract:

- Zero `CRITICAL RULES:`-style caps headers; at most 3 bolded rules, each
  destructive-risk.
- Every surviving prohibition carries its reason; prefer the positive form.
- Persona blocks contain interactive guidance only; agent lifecycle terms stay
  in agent blocks.
- No experience flattery outside `default`/`comms-agent`/chat assembly.
- No thoroughness/verification exhortations.
- Exact commands, tool names, markers, and output contracts byte-identical to
  what the consuming code expects.
- Diff is prose-only for agent YAMLs: step workflows, rule selectors, tool
  policies, and skill references unchanged.
