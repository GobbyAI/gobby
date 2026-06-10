# docs/reviews/

Findings store for the repo-wide review epic (**#15764**). Every review leaf writes
its findings here so they survive `compact_self` between `/goal` leaves — context is
disposable, these files are the durable record.

## Files

| File | Authored by | Purpose |
|---|---|---|
| `RUBRIC.md` | scaffold | Python review rubric (severity, evidence discipline, repo contracts). |
| `web/RUBRIC.md` | scaffold | Web (TS/React) rubric — design system + React/TS correctness. |
| `TEMPLATE.md` | scaffold | Per-finding shape every review file follows. |
| `<area>.md` | review leaves | One file per Python area (e.g. `dispatch.md`, `storage-core.md`). |
| `web/<area>.md` | web review leaves | One file per web area (e.g. `web/hooks.md`). |
| `docs-findings.md` | docs-accuracy leaves | Code-vs-doc mismatches that imply a **code** bug (not a doc fix). |
| `TRIAGE.md` | triage leaf (#15802) | Deduped, prioritized master list. |

## Flow

1. **Scaffold (#15765)** writes the rubrics + template (these files).
2. **Review leaves** (Python + web, all depend on scaffold) each deep-review one
   area and write `<area>.md` per `TEMPLATE.md`. Clean areas still write a file
   stating "No findings."
3. **Docs-accuracy leaves** reconcile `docs/guides` and `docs/architecture` against
   the code, fixing wrong docs in place and logging code-vs-doc bugs to
   `docs-findings.md`.
4. **Triage (#15802)** reads every file here, dedups systemic issues, prioritizes,
   writes `TRIAGE.md`, and creates one fix task per actionable finding under the
   **Fixes sub-epic (#15766)** — a second `/goal` run works those.

## Conventions

- Rubrics are **starting points** — reviewers may extend them for a subsystem's
  specific risks, but not drop below the floor.
- Findings without an exact `file:line` don't belong here.
- These review files are committed deliverables; the directory is git-tracked.
