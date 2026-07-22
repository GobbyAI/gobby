---
name: wiki-research
description: Run one wiki research pass — scope a question against the repo and vault, discover and curate sources, ingest with dedup, compile a cited topic page, and record findings in the review backlog.
version: "1.0.0"
category: methodology
internal: true
triggers: wiki research, research question, standing query, research pipeline
metadata:
  gobby:
    audience: all
---

# wiki-research — One Research Pass Into the Vault

Use this skill when spawned to answer a research question into the wiki. One
run = one question = one cited topic page. Inputs arrive with the question:
`max_sources` (discovery stop), `max_items` (curation cap), `create_tasks`
(whether backlog findings also become gobby-tasks), and optionally an explicit
output contract. **Explicit output-contract instructions in the question
override the default note template in step 6.**

All wiki tools live on the `gobby-wiki` MCP server (`wiki_ask`, `wiki_search`,
`wiki_read`, `wiki_list_sources`, `wiki_ingest`, `wiki_compile`,
`wiki_write_page`). Discover them through progressive discovery before first
use.

**Scope warning — never pass `topic` or `project` arguments to wiki tools.**
Those parameters select the wiki SCOPE: `topic=<name>` routes every ingest and
compile into a separate topic wiki under `~/wiki/topics/<name>/`, silently
bypassing the project vault. Research runs in the ambient project scope; the
research topic only appears as the compile article title (`compile_topic`) and
the target page path.

## 1. Context bootstrap

Scope relevance before searching the web:

- `gcode repo-outline` plus the repo README — this works even while the
  vault's synthesis layer is empty.
- `wiki_ask(query=<question>)` when the vault has content — use its answer and
  evidence to learn what the wiki already covers and where the gaps are.

Write down (for yourself) what would make a source relevant to gobby. Every
curation decision in step 3 references this scope.

## 2. Discovery

Derive 3–5 search angles from the question (synonyms, adjacent systems,
competing implementations, canonical docs).

- Time-windowed "everything new since X" queries: prefer structured indexes —
  RSS/Atom feeds, new-listing pages, public APIs — fetched with `WebFetch`.
  They enumerate completely; search engines do not.
- Everything else: `WebSearch` fan-out across the angles, then `WebFetch` the
  promising hits.

Stop discovering when you reach `max_sources` candidates. Do not curate during
discovery; collect first.

## 3. Curate

Cut the candidate list to at most `max_items`. Record a one-line keep or
discard reason for every candidate — the run report (step 9) includes them.
Keep reasons must tie back to the step-1 relevance scope, not to novelty.

## 4. Pre-ingest dedup

Before ingesting, check every kept URL against the vault:

- `wiki_list_sources` — match on location/canonical URL.
- `wiki_search(query=<url or title>)` — catch prior ingests under a different
  location.

Already-present sources are reused, not re-ingested: cite their existing raw
path in step 6 and note the dedup hit in the run report.

## 5. Ingest

`wiki_ingest` with the deduplicated URL batch (`urls` only — no `topic` or
`project` scope arguments). Per-URL failures do not stop the run: record each
as a `gap:` line in the affected item's note (step 6) and continue with what
ingested.

## 6. Accepted notes (one per kept item)

Write one note file per item in the accepted-note contract parsed by compile
(`citation:` / `gap:` / `conflict:` line prefixes; `conflicting claim:` and
`missing evidence:` are accepted aliases). Default template:

```markdown
## Summary

<what the source says, grounded, few sentences>

## How this improves gobby

<the concrete connection to gobby's architecture or roadmap>

## Investigation prompt

<the follow-up an engineer should run down>

citation: <raw/<source-id>.md path from ingest>
citation: <original URL>
gap: <anything the source leaves unanswered, if any>
conflict: <claim that contradicts another kept source, if any>
```

Then `wiki_ingest` with the note file paths so the notes become vault sources
too. Skip this template only when the question carries its own output
contract.

## 7. Compile the topic page

`wiki_compile` with — always — an explicit `compile_topic` (the article
topic) and the full source list (every kept source plus every note). Never
rely on an existing research checkpoint to supply sources: an explicit
compile topic + source list replaces the checkpoint's accepted notes, so an
implicit call can hijack or be hijacked by unrelated checkpoint state. Use
`kind=topic`, target `knowledge/topics/<slug>.md` where `<slug>` is the
kebab-cased question topic. Remember: `compile_topic` names the article;
the `topic` parameter would switch wiki scope — never pass it.

## 8. Review backlog (always)

The canonical destination for findings is
`knowledge/topics/wiki-research-backlog.md`. Read it with `wiki_read`; if it
does not exist, create it with `wiki_write_page` using this header:

```markdown
---
title: Wiki research review backlog
source_kind: topic
lifecycle: draft
tags:
  - wiki-research
  - backlog
---

# Wiki Research Review Backlog

Detailed research ideas awaiting manual review. Status edits on existing
entries are authoritative and must be preserved by later research runs.

## Findings
```

Append one entry per kept item. The finding slug is the kebab-cased accepted
note basename, which is stable across retries. Derive both hidden markers from
the canonical compiled topic path and that finding slug. Use this exact shape:

```markdown
<!-- wiki-research-backlog:knowledge/topics/<topic>.md#<finding-slug> -->
<a id="wiki-research-<topic>--<finding-slug>"></a>
### <finding title>

- Status: pending review
- Topic: [[knowledge/topics/<topic>|<compiled topic title>]]
- Rationale: <concrete connection to gobby>
- Investigation prompt: <the engineer follow-up from the accepted note>
- Citations:
  - <citation from the accepted note>
  - <every other citation from the accepted note>
```

Before appending, search the existing backlog text for the full hidden marker.
When it exists, leave the entire entry unchanged, including any manually edited
status. For a missing marker, append the entry without rewriting existing
entries.

Read the compiled topic page and add one compact backlink per finding under a
`## Later review` heading, using this exact two-line shape:

```markdown
<!-- wiki-research-topic:knowledge/topics/<topic>.md#<finding-slug> -->
- Later review: [<finding title>](wiki-research-backlog.md#wiki-research-<topic>--<finding-slug>)
```

Check the backlog marker and topic marker independently so a retry repairs a
partial write. Never replace an existing marked entry. Preserve the compiled
page's source evidence and all pre-existing content verbatim.

## 9. Investigation tasks (only when `create_tasks=true`)

Triage every kept item before filing — the vault knows sources, but the task
graph and the codebase know whether the idea is new:

1. `search_tasks` on `gobby-tasks` with the finding's key terms (limit 10).
   Results include closed tasks — that is the point:
   - An open task already covering the investigation → do not file.
   - A closed hit closed as duplicate, wont_fix, already_implemented, or
     obsolete → do not re-file; its close rationale is the answer.
   - Related-but-distinct tasks → still file, and add one `related: #NNNN`
     line per related task to the description.
2. `gcode search "<proposed mechanism>"` (plus `gcode search-content` for
   docs and code comments) to check the idea against the codebase. Already
   implemented, or explicitly settled in a code comment (e.g. a "regressed
   in #NNNN" note) → do not file; record the prior art instead.

Then one `create_task` on `gobby-tasks` per surviving item: title from the
finding, description = the item's investigation prompt, its `citation:` lines,
any `related: #NNNN` lines, and a Markdown link to the exact backlog anchor from
step 8. Label it `wiki-research`. The backlog entry remains canonical. No tasks
for discarded candidates. Every triaged-away item MUST appear in the run report
(step 10) with its reason — a duplicate task ref or a prior-art pointer. Skipped
filings are visible, never silent.

## 10. Run report

Write the run report to a temporary local Markdown file covering: the question,
angles searched, candidates found, keep/discard reasons, dedup hits, ingest
failures, compiled page path, tasks filed, and items triaged away in step 9
when task creation was enabled (with their duplicate task refs or prior-art
reasons). Include the backlog path, finding count, and whether optional task
creation was enabled. Then `wiki_ingest` the local path so the report becomes a
vault source under `raw/`; retain the returned source path as the run report
path.

Keep `outputs/**` reserved for generated artifacts; `wiki_write_page` only
writes `knowledge/**`. Leave the root `log.md` to gwiki: `wiki_compile` records
the topic page creation there automatically.

## 11. Finish

Close your claimed task with the DEFAULT completed reason and a
`changes_summary` naming the compiled topic page path, the backlog path, the run
report path, and the source/finding counts (the run report path is its ingested
`raw/` source path; attach `commit_sha` only when repo files actually changed —
vault-only runs close without a commit). Never close with
`out_of_repo`, `wont_fix`, or any validation-skipping reason: validation must
check your summary against the task's criteria. Then call `end_agent_run`.
Never leave the run open after the report is written.
