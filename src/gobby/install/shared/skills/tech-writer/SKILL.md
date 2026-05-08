---
name: tech-writer
description: How to author, audit, and refresh Gobby technical documentation. Use when writing or rewriting a guide, validating an existing doc against the codebase, generating Mermaid diagrams, or explaining a technical concept to docs readers.
category: engineering
version: "1.0.0"
triggers:
  - tech writer
  - technical writing
  - write documentation
  - audit documentation
  - refresh guide
  - validate doc
  - mermaid diagram
  - explain concept
tags:
  - gobby
  - docs
metadata:
  gobby:
    audience: all
    depth: 0
---

# Tech Writer

Methodology for authoring, auditing, and maintaining Gobby's technical
documentation. Covers `docs/guides/`, in-repo READMEs, architecture notes, and
inline reference material. For design-surface copy (product UI, marketing,
installer), the canonical authority is `.impeccable.md` — read it before
touching any UI string.

## Principles

- **Write for the reader's task, not the author's checklist.** A guide exists
  to make the reader successful, not to enumerate features.
- **A diagram beats a thousand-word paragraph.** When structure, flow, or
  state transitions are involved, lead with Mermaid; let prose explain only
  what the diagram cannot.
- **Audience-aware depth.** Operator docs assume terminal fluency; contributor
  docs assume the codebase; user docs assume nothing. Don't mix levels in one
  doc — split it.
- **One source of truth.** If a fact lives in code (a CLI flag, an MCP tool
  signature, an HTTP route), the doc cites the code, never restates it from
  memory. Drift kills docs faster than anything else.
- **Diátaxis where it fits.** Tutorials, how-to guides, reference, and
  explanation each have a different shape. Don't blend them in one document.

## Capabilities

| Operation | When to use |
|-----------|-------------|
| **Write** a new document | Net-new guide, feature launch, new MCP server, new CLI surface |
| **Audit/refresh** an existing document | Code has drifted from docs, periodic freshness pass, post-refactor cleanup |
| **Mermaid diagram** | Sequence, flow, state, ER, or architecture visualization carries more signal than prose |
| **Explain** a concept | Reader needs the *why*, not the *how* — architecture rationale, design trade-offs, mental model |

The four operations below are inline methodology, not separate dispatchable
modes. Pick the one that matches the ask and follow its process.

## Writing a Document

1. **Discover intent.** Ask until scope, audience, and purpose are concrete.
   Refuse to draft a guide whose audience you cannot name.
2. **Locate authority.** Identify the source-of-truth code (a CLI module, MCP
   tool, route file). Every concrete claim in the doc must trace back to a
   file path or symbol you can `gcode search` or `Read`.
3. **Outline before prose.** Headings first; verify the structure makes sense
   to the target reader before writing paragraphs.
4. **Draft.** Lead each section with the action or the answer; put background
   below. Use fenced code blocks for any command, config, or API shape.
5. **Diagram where structure matters.** State machines, dispatch flows,
   request lifecycles — Mermaid them.
6. **Cross-link.** If another guide owns a concept, link to it; do not
   re-explain.
7. **Footer.** Add `_Last verified: YYYY-MM-DD_` as the final line so future
   auditors can see freshness at a glance.
8. **Index.** Update `docs/guides/README.md` so the new guide is discoverable.

## Auditing / Refreshing a Document

This is the dominant mode in Gobby — most doc work is keeping existing guides
honest. Apply the eight-point checklist established by the audit epic:

1. Every CLI command, flag, MCP tool name, MCP server name, CLI subcommand,
   and HTTP route mentioned in the guide exists in the current codebase
   (verify via `gcode search`, `gobby --help`, or route inspection in
   `src/gobby/servers/routes/`).
2. No references to retired concepts. Maintain a project-specific deny-list
   when refactors retire terms — for Gobby today: `orchestrator`, `conductor`,
   `developer` agent, `pipeline-worker`, `terminal` agent mode (renamed
   `interactive`), `list_pipeline_executions`, `search_pipeline_executions`
   (renamed `pipeline_runs`), `lobster`, `mem0`, `tool-compression`,
   `workflow-actions`.
3. All inter-guide links resolve to existing files in `docs/guides/`.
4. Code blocks showing CLI/MCP/HTTP usage match current signatures (no
   positional/keyword drift). Re-fetch tool schemas with
   `get_tool_schema(server, tool)` rather than recalling them.
5. Architecture references align with current `CLAUDE.md` (dispatch
   architecture, plan-coverage contract, stage-manifest model).
6. If guide is < 100 lines and content overlaps another guide, justify
   keeping it standalone or merge it. Two thin guides that overlap is worse
   than one complete guide.
7. Update the `_Last verified: YYYY-MM-DD_` footer on completion.
8. Update `docs/guides/README.md` if the guide was renamed, deleted, or
   merged.

When the audit finds a guide that is obsolete (feature removed, replaced by
another guide, or stub-quality with no path forward), prefer **delete** over
preserve. Stale docs are worse than missing docs because they actively
mislead. Update the index and any inbound links when removing.

## Generating Mermaid Diagrams

1. Pick the diagram type up front: `flowchart`, `sequenceDiagram`,
   `stateDiagram-v2`, `classDiagram`, `erDiagram`, `gantt`. State the choice
   before drawing.
2. Use stable IDs (`A`, `B`, `node1`) and human labels (`A[Reader]`); never
   put long sentences inside node labels.
3. Render inside a fenced ` ```mermaid ` code block so GitHub/Sphinx/MkDocs
   can hand it to a renderer.
4. Test the diagram with the Mermaid live editor or a local renderer before
   committing — silent syntax errors break the whole page in some renderers.
5. Constrain layout: `direction LR` or `direction TB` for flowcharts when the
   default makes the diagram unreadable.
6. For sequence diagrams of dispatch or hook flows, name the actors after the
   real subsystems (`Dispatcher`, `RuleEngine`, `AgentRunner`) so the diagram
   maps 1:1 to code.

## Explaining a Concept

1. **Anchor.** Open with the one-sentence definition the reader can carry
   away even if they read nothing else.
2. **Motivate.** Why does this exist? What was broken before it? Skip when
   the answer is obvious.
3. **Demonstrate.** A minimal worked example — code, command, or diagram —
   beats abstract description.
4. **Bound.** State what the concept is *not* responsible for. Misuse of a
   concept usually traces back to an unbounded explanation.
5. **Link.** Point to the canonical reference doc and the code that
   implements it.

## Gobby Conventions

### File locations
- Bundled guides: `docs/guides/<name>.md`
- Index: `docs/guides/README.md` (must include every linked guide)
- Design system: `.impeccable.md` (root) — authoritative for any UI/color/typography/copy work
- Architecture and contracts: `docs/contracts/`
- Research drafts: `docs/research/` (not user-facing — do not link from `docs/guides/`)

### Cross-references
Use relative paths (`./tasks.md`, not `/docs/guides/tasks.md`) so links
resolve in both GitHub view and local clones. Verify every link before
closing the task.

### Source-of-truth lookups
- CLI commands → `src/gobby/cli/` (use `gobby --help` to confirm runtime
  visibility, since some commands gate on feature flags)
- MCP tools → `src/gobby/mcp_proxy/tools/` and `get_tool_schema(server, tool)`
- HTTP routes → `src/gobby/servers/routes/`
- Hook events → `src/gobby/hooks/events.py` and `src/gobby/hooks/hook_types/`
- Agent definitions → `src/gobby/agents/definitions.py` and
  `src/gobby/install/shared/agents/`
- Dispatch architecture → `src/gobby/dispatch/rules.py` plus the dispatch
  section of `CLAUDE.md`

### Commit and close
Doc work is real work — claim a Gobby task, commit the changes, validate
against the task's acceptance criteria, and close. Closing a docs task
without a commit is blocked by Gobby rules. Treat a half-finished refresh as
unfinished, not as merge-ready.

### Freshness footer
End every guide with `_Last verified: YYYY-MM-DD_` on its own line. The date
reflects when the doc was last cross-checked against the codebase, not when
prose was last edited. The footer is the cheapest staleness signal an
auditor has.

## Boundaries

- Do not invent CLI flags, tool names, or routes — verify or omit.
- Do not document features that aren't shipped. Forward-looking text belongs
  in `docs/research/` or a plan, not `docs/guides/`.
- Do not blend Diátaxis modes. A how-to that turns into an explanation halfway
  through is a sign the doc should be split.
- Do not edit `.impeccable.md` freehand — that file is updated through the
  `impeccable` skill's `teach` mode.
- Do not rely on memory for tool signatures. Fetch the schema, then write.

_Last verified: 2026-05-04_
