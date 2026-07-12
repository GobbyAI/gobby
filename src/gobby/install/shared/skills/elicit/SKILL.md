---
name: elicit
description: "Relentless one-question-at-a-time interview that pressure-tests a plan or design before building. Walks every branch of the decision tree, recommends an answer per question, and ends with a confirmed Decision Record."
version: "1.0.0"
category: core
triggers:
  - elicit
  - grill
  - interview me
  - stress-test this plan
  - shared understanding
metadata:
  gobby:
    audience: interactive
    depth: 0
---

# Elicit

Interview the user relentlessly about a plan or design until you reach shared
understanding. The most common failure mode in building software is
misalignment between what the user meant and what gets built. Elicit closes
that gap before any work starts.

Adapted from `grill-me`/`grilling` in [mattpocock/skills](https://github.com/mattpocock/skills) (MIT).

## Protocol

1. **Walk the decision tree.** Identify every open decision in the plan or
   design. Resolve dependencies between decisions in order — settle the
   decisions that other decisions hang on first, then descend each branch
   until it is fully resolved.
2. **One question at a time.** Ask a single question, wait for the answer,
   then continue. Multiple questions at once are bewildering and produce
   shallow answers. Where the host provides `AskUserQuestion`, use it with
   your recommended option listed first and suffixed "(Recommended)";
   otherwise ask plain conversational questions.
3. **Always recommend an answer.** Every question ships with your recommended
   answer and a one-line reason. Recommendations keep momentum and give the
   user something concrete to push against.
4. **Facts are yours; decisions are the user's.** Anything discoverable in
   the codebase is a fact — look it up with gcode (`gcode search`,
   `gcode grep`, `gcode outline`) instead of asking. Never ask the user
   something the index can settle. Decisions — trade-offs, scope, naming,
   priorities — belong to the user; put each one to them and wait.
5. **End with a Decision Record.** When no unresolved branches remain,
   present a compact summary of every decision made and its answer. Ask the
   user to confirm it.

## Boundaries

- Do not build, edit, or enact the plan until the user confirms the Decision
  Record.
- Do not pad the interview. When every branch is resolved, stop asking and
  summarize.
- If an answer invalidates earlier decisions, revisit only the affected
  branch — say which decisions reopened and why.
