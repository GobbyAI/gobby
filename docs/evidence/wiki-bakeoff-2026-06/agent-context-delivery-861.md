# Agent-context delivery — #861 post-adoption matrix

**Task:** #861 — wiki bake-off "agent-context delivery" audit → close the gobby-cli residuals and
write the comparison doc (epic **#908**, leaf **#914**).
**Sibling doc:** [`narrative-quality-853.md`](narrative-quality-853.md) is the *human-reading-quality*
half of the bake-off (curated narrative depth). This doc is the *agent-context-delivery* half — how
an **agent** (not a human) gets grounded, bounded, queryable context out of gobby's surfaces.
**Status:** epic #908 Workstream C (gwiki ergonomics) + Workstream D (diagrams) complete; the daemon
Workstream B handoff (the daemon repo) landed in parallel. Candidate sources are the
C1–C10 log in [`ADOPTION-CANDIDATES.md`](ADOPTION-CANDIDATES.md).

## TL;DR

The bake-off's central finding for agent context is an **audience split competitors conflate**:

- **Agents** want **structured, bounded, queryable** retrieval — symbol/graph traversal with
  resolvable `file:line` grounding, token-capped output, and static machine-readable exports.
  **Agents never need diagrams.** A mermaid flowchart is pixels an agent has to re-parse back into
  the graph it already has.
- **Humans** want **narrative + flow diagrams** — a curated top-down tour with a picture per page.

Competitors ship one artifact to both audiences. DeepWiki-Open emits a diagram on **all 10 pages**
(human axis) but its agent-facing citation links are empty `()` — non-navigable, non-verifiable
(W1). Graphify is the mirror failure: strong agent primitives (path / per-edge confidence /
token-budget) and **no human narrative layer at all**. Gobby already separated the two surfaces:
`gcode` (graph + cited retrieval) + gwiki exports serve agents; `codewiki` curated narrative +
diagrams serve humans.

So **agent-context delivery was effectively already done** — `gcode` graph search + bounded cited
retrieval is the agent surface, and it wins the bake-off on grounding. Epic #908 closed the small
residual gaps the audit surfaced, on the correct side of the split each time:

| Candidate | Axis | What it is | Status |
|---|---|---|---|
| **C8** token-budget retrieval | agent | `--token-budget N` bounded output + narrowing hint | **done** (L1 #909, L2 #912) |
| **C10** static agent exports | agent | `llms.txt` / `llms-full.txt` / `graph.jsonld` | **done** (L3 #910) |
| **C4** diagram fallback | human | flow diagrams on curated pages | **pre-landed (#891) → completed** (L4 #911) |
| **C5** concept-module clustering | human | curated semantic navigation layer | **closed** (#762, `c07db15f`) |
| lint backstop | both | mermaid validity + grounding lint | **done** (L5 #913) |
| Workstream B | daemon | wire gwiki session sync + `gcode path` | **done** (#17333, `9f36ecf80`) |

Execution order was **C → D → A** (gwiki ergonomics → diagrams → this doc), per #908.

---

## 1. The split, stated against the surfaces

| | Agent surface | Human surface |
|---|---|---|
| **Primary tool** | `gcode` (search / symbol / callers / blast-radius / graph) + gwiki `search`/`ask` + static exports | `gcode codewiki` curated narrative + diagrams |
| **Output shape** | ranked rows, resolvable `file:line`, JSON, token-capped | prose pages, tables, mermaid per page |
| **Diagrams?** | **no** — the graph is the API; a picture is lossy re-encoding | **yes** — a picture is the comprehension aid |
| **Grounding** | inline `file:line` that resolve (lint-checked) | per-page provenance + curated links |
| **Bound** | `--limit`, `--token-budget N` (token-aware) | page count, frontmatter caps |

The two residual agent-axis gaps the audit found were both **ergonomics, not correctness**: gobby's
retrieval was already grounded and bounded-by-count, but not bounded-by-**tokens** (C8) and not
emitted as a **portable static artifact** an external agent could load without the CLI (C10).

---

## 2. Agent axis — what landed (Workstream C)

### 2a. C8 — token-budget-capped retrieval (L1 #909, L2 #912)

**Why (agent axis):** agents reason in tokens, not row counts. `--limit` caps rows; a 1,200-token
context window doesn't care how many rows that is. Graphify's `query --budget 1200` returned the
highest-priority subgraph within a token ceiling **and told the agent how to narrow** — the
ergonomic gobby lacked.

**L1 — shared helper extracted to gobby-core** (commit `6f77e2f`, #909). The estimate/trim
vocabulary lives once, always-compiled, consumed by both CLIs:
- `crates/gcore/src/token_budget.rs:20` — `estimate_tokens()` (`ceil(chars / 4)` heuristic,
  divisor at `:10`).
- `crates/gcore/src/token_budget.rs:36` — `trim_results()` keeps the longest priority-order prefix
  that fits the budget; returns `TokenBudgetTrim` (`:14`) carrying the kept rows **plus an optional
  narrowing hint**. `None` budget returns input untouched (`:45`).

**L2 — `--token-budget N` on gwiki `search` and `ask`** (commit `601bed9`, #912):
- `crates/gwiki/src/main.rs:238` — `#[arg(long = "token-budget", …)]` on `search`; `:260` on `ask`
  (both validate positive `usize`).
- `crates/gwiki/src/commands/search.rs:320` — calls `token_budget::trim_results` (the gobby-core
  helper imported at `:10`), so the gwiki budget vocabulary is byte-identical to gcode's.
- Contract surface extended: `crates/gwiki/contract/gwiki.contract.json` (the `--token-budget` flag
  is part of the pinned CLI contract).

This is the *agent* knob: bounded retrieval that fits a context window, with a "refine with
`--token-budget`" hint when rows were dropped — never a diagram.

### 2b. C10 — static agent-context exports (L3 #910)

**Why (agent axis):** an external agent (or a crawler) shouldn't need the gwiki binary to consume
the vault. llm-wiki shipped `llms.txt` / `llms-full.txt` / `graph.jsonld`; gwiki had the data and no
portable emission.

**L3 — three portable formats** (commit `5c8a301`, #910), all in `crates/gwiki/src/exports.rs`:
- `:184` — `export_agent_artifacts()` writes all three to `outputs/`.
- `:198` — `graph.jsonld` (schema.org JSON-LD of the vault document graph).
- `:205` — `llms.txt` (portable link index, llmstxt.org convention; renderer at `:349`).
- `:212` — `llms-full.txt` (full Markdown body bundle; renderer at `:375`).

Exports are read-only over vault state (test `exports_do_not_mutate_wiki_state`), so an agent gets a
machine-readable snapshot without invoking retrieval — the static counterpart to C8's live bounded
retrieval.

---

## 3. Human axis — what landed (Workstream D), and why it's *not* agent context

### 3a. C4 — diagram fallback: pre-landed (#891) → completed (L4 #911)

**Why (human axis only):** `narrative-quality-853.md` root cause #4 was "**curated pages render 0
diagrams**" — the diagram capability existed but the human-facing concept/narrative pages never
called it. Diagrams are a **human** comprehension aid; this gap was on the human side of the split,
which is why it's Workstream D, not an agent-context fix.

**Pre-landed:** the architectural-diagram renderer already existed —
`crates/gcode/src/commands/codewiki/architecture_diagrams.rs:40` `render_architecture_diagrams()`
(topology flowchart + runtime sequence from the `SystemModel`), shipped by commit `8b80e750`
(#891). It is **honest by construction**: it re-gates every block through `is_valid_mermaid`
(`:53`, validator at `:433`) and returns `None` rather than emit a broken or empty diagram (doc at
`:36`). Module-level dependency diagrams predate even that
(`crates/gcode/src/commands/codewiki/render/diagrams.rs`, `collect_subsystem_dependency_edges`).

**Completed (L4, commit `57dc95e`, #911):** conceptual-flow diagrams now render on the curated
concept/narrative pages — closing the "0 curated diagrams" gap:
- `architecture_diagrams.rs:540` — `render_conceptual_flow()` chains real page members into a
  flowchart; returns `None` (omits the section) when the flow is too sparse or fails
  `is_valid_mermaid` — **honest omission at the generator** (no fabricated diagram).
- `crates/gcode/src/commands/codewiki/build_parts/curated_content.rs:372` — the curated builder
  calls it, passing only real page members, so a node can't appear unless it's a documented member.

### 3b. C5 — semantic concept-module navigation: closed (#762)

**Why (human axis):** C5 was the highest-confidence candidate (CodeWiki + Graphify both converged on
semantic graph-community clustering). It groups code by *what it does* into named concept-modules —
a **human** navigation spine, not an agent primitive (an agent queries the graph directly).

**Closed** by commit `c07db15f` (#762, "feat: add curated codewiki navigation layer"):
- `crates/gcode/src/commands/codewiki/build_parts/concepts.rs` — `build_curated_navigation_docs()`
  produces the curated concept pages (the concept-module view) alongside the directory-faithful
  reference pages.

This is why #908 carried no C5 leaf: the navigation layer was already shipped; #908's diagram work
(C4/L4) layers a per-page picture onto those existing curated pages.

### 3c. Lint backstop — mermaid validity + grounding (L5 #913)

**Why (both axes):** the generator does honest omission (§3a); the lint is the **backstop** that
catches a wrong diagram if one ever leaks into a committed vault.

**L5** (commit `df7fcf0`, #913), all in `crates/gwiki/src/lint.rs`:
- `:22` — `invalid_diagrams: Vec<DiagramIssue>` added to `LintReport`; `DiagramIssue` at `:44`.
- `:553` — `invalid_diagrams()` flags any mermaid block that is malformed **or** whose node labels
  aren't grounded in the surrounding page prose.
- `:546` — `VALID_DIAGRAM_HEADERS` mirrors gcode's `architecture_diagrams::VALID_HEADERS`
  (`flowchart` / `graph` / `sequenceDiagram`) so the lint accepts exactly what the generator can
  produce. (Mirrored, not imported — `is_valid_mermaid` is `pub(crate)` in the gcode **binary**.)
- `:526` — `render_diagram_issues()` reports `- none` on a clean vault.

On the live deployed vault this list is **empty** (`invalid_diagrams=0`, `broken_links=0`),
confirming the generator's honest-omission path holds end to end.

---

## 4. Daemon — Workstream B (parallel handoff)

Workstream B is daemon integration in the daemon repo — explicitly **not** part of
#908 (epic description), but its outcomes complete the agent-context picture because the daemon is
how an agent reaches these surfaces over MCP/HTTP.

**#17333** (commit `9f36ecf80`, "feat: wire gwiki session sync and gcode path", 13 files /
+407):
- `src/gobby/wiki/scheduled_jobs.py` — **gwiki session sync** scheduled job (the daemon-owned
  "sessions → vault" path; pairs with C10's exports and the C10 session-ingestion candidate).
- `src/gobby/code_index/gcode_gateway.py` + `src/gobby/servers/routes/code_index.py` — daemon
  gateway/route for **`gcode path`** (the C6 shortest-path-between-two-concepts agent primitive),
  surfaced to agents over HTTP.
- `src/gobby/mcp_proxy/tools/wiki.py` — MCP wiki tool wiring; `src/gobby/code_index/context.py`
  context plumbing.

The Rust side owns the projection and the CLI contract; the daemon wires them to the agent
transport. Both halves landed; the split holds across the repo boundary (agents call the daemon →
gcode graph/path + gwiki search/exports; humans read the codewiki vault).

---

## 5. Competitor conflation — the proof matrix

| Tool | Agent axis | Human axis | Conflation failure |
|---|---|---|---|
| **DeepWiki-Open** | citation labels with **empty `()` hrefs** — non-navigable, non-verifiable (W1) | diagram on all 10 pages, dense tables | ships human-grade diagrams to everyone while the agent-facing provenance doesn't resolve |
| **Graphify** | strong: `path`, per-edge confidence (`[EXTRACTED]`), `--budget` | **none** — no narrative pages | agent-only; a human gets a graph dump, no tour |
| **gobby** | `gcode` graph + resolvable `file:line` + `--token-budget` + static exports | `codewiki` curated narrative + per-page diagrams | **separated**: diagrams on the human surface, bounded cited retrieval on the agent surface |

DeepWiki proves the failure in one direction (W1: a `file:line` label with an empty link is *worse*
than useless to an agent — it looks grounded and isn't). Graphify proves it in the other (great
agent primitives, zero human onboarding). Gobby's win on the agent axis is the one that's
structurally hard: **resolvable, lint-checked grounding**, now bounded by tokens (C8) and emitted
statically (C10).

---

## 6. Closeout

- **Agent axis:** done — `gcode` graph retrieval was already the win; #909/#912 add token bounding,
  #910 adds portable exports. No diagrams on the agent surface, by design.
- **Human axis:** done — C5 navigation (#762) + C4 diagrams (#911) give the curated tour a picture
  per page; #913 lint backstops diagram honesty.
- **Daemon:** done — #17333 wires session sync + `gcode path` to the agent transport.
- **Verification:** every status claim above cites a `file:line` and/or commit; the live vault lints
  clean (`invalid_diagrams=0`, `broken_links=0`).

The bake-off verdict holds: gobby is ahead on every structurally-hard agent-context dimension
(resolvable grounding, search-over-own-output, bounded retrieval); the adopted candidates were
additive ergonomics and a human-comprehension layer, each landed on the correct side of the
agent/human split.
