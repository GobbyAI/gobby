# Drawbridge Import Enhancement Plan

**Status:** Planned
**Created:** 2026-02-11
**Updated:** 2026-02-11
**Task:** #8038
**Rationale:** Drawbridge annotations live in `.moat/moat-tasks-detail.json` as flat JSON objects with no dependencies, session linking, or validation. By importing them as gobby tasks, they gain the full task lifecycle: dependency graphs, session tracking, validation gates, and the expand/close workflow. The `/bridge` skill becomes a focused importer rather than a parallel task system.

---

## Overview

The current `/bridge` skill (286 lines) and `drawbridge-workflow.md` (802 lines) implement a standalone task processing system that duplicates gobby's own task management. This plan replaces that with an import flow: read `.moat/` annotations, create gobby tasks, and let the existing task system handle the rest.

**Key decisions:**
- **Import only** — `.moat/` files are read-only input; no dual-tracking back to Drawbridge files
- **Auto-create epic** — all imported tasks nest under a parent epic per project
- **Label-based dedup** — `drawbridge:{uuid}` labels make re-runs safe

**Total scope:**

- **1 file to rewrite** (`bridge.md` ~150 lines replacing 286)
- **1 file to trim** (`drawbridge-workflow.md` ~350 lines replacing 802)
- **0 new source files** (import logic lives entirely in the skill prompt; existing MCP tools are sufficient)

---

## Context: Drawbridge Data Model

Each annotation in `moat-tasks-detail.json` is a JSON object:

```json
{
  "id": "d9c69e4d-4d2e-4b74-83c3-c81c8f9de990",
  "title": "Freeform Rectangle Task",
  "comment": "Remove the lines between Workflows and Cron Jobs",
  "selector": "freeform",
  "boundingRect": { "x": 8, "y": 12, "w": 372, "h": 691 },
  "screenshotPath": "./screenshots/moat-1770769416084-swqxu5mqu.png",
  "status": "to do",
  "timestamp": 1770769416104,
  "boundingBox": { "xyxy": {...}, "xywh": {...}, "normalized": {...}, "viewport": {...}, "type": "freeform" }
}
```

Key observations:
- `title` is often generic ("Freeform Rectangle Task") — `comment` carries the real intent
- `status` uses Drawbridge values: `"to do"`, `"doing"`, `"done"`, `"failed"`
- `selector` is either a CSS selector or `"freeform"` for region annotations
- `screenshotPath` is relative to `.moat/` (needs resolution)
- `id` is a UUID assigned by the Chrome extension

---

## Field Mapping

### Drawbridge JSON -> Gobby Task

| Drawbridge Field | Gobby Task Field | Transformation |
| --- | --- | --- |
| `comment` | `title` | First 120 chars, truncated at word boundary |
| `comment` (full) + metadata | `description` | Structured markdown (see template below) |
| `id` | label: `drawbridge:{id}` | For deduplication |
| (inferred from `comment`) | `task_type` | See inference rules below |
| — | `category` | `"code"` for all (UI implementation) |
| — | `priority` | `2` (medium) default; user can override post-import |
| — | `labels` | `["drawbridge", "drawbridge:{id}"]` |
| epic ref | `parent_task_id` | Auto-created epic |
| — | `validation_criteria` | `"Visual change matches the annotation screenshot and comment description"` |

### Task Type Inference

Applied to `comment` text (case-insensitive):

| Pattern in comment | task_type |
| --- | --- |
| "doesn't work", "broken", "missing", "wrong", "incorrect", "not working", "bug", "error", "disabled", "can't" | `bug` |
| "add", "need", "want", "should have", "create", "new" | `feature` |
| Everything else (styling, layout, tweaks) | `task` |

### Description Template

```markdown
{full comment text}

---
**Drawbridge annotation**
- Selector: `{selector}`
- Screenshot: `{resolved_screenshot_path}`
- Region: {x},{y} {w}x{h} (viewport: {viewport.width}x{viewport.height})
- Annotated: {ISO timestamp from epoch ms}
- Drawbridge ID: {id}
```

### Status Mapping

| Drawbridge Status | Import Action |
| --- | --- |
| `"to do"` | Create as `open` gobby task |
| `"doing"` | Skip — may be actively worked in old system; mention in summary |
| `"done"` | Skip — already completed |
| `"failed"` | Create as `open` gobby task (retry) |

---

## Import Flow

The import is a prompt-driven workflow executed by the agent when the user runs `/bridge`. No new Python code is required — the agent reads files and calls existing MCP tools.

### Step-by-step

```
1. LOCATE .moat/moat-tasks-detail.json
   ├─ Check: .moat/moat-tasks-detail.json (current dir)
   ├─ Check: moat-tasks-detail.json (legacy location)
   └─ Check: ../.moat/moat-tasks-detail.json (parent dir)
   → Error if not found (show setup instructions)

2. READ + PARSE annotations
   └─ Parse JSON array, filter to status="to do" or "failed"

3. CHECK for existing epic
   └─ list_tasks(label="drawbridge-epic")
   → If open epic found: reuse as parent
   → If not found: create epic (see below)

4. CREATE EPIC (if needed)
   └─ create_task(
        title="Drawbridge UI Review - {date}",
        task_type="epic",
        labels=["drawbridge", "drawbridge-epic"],
        category="code",
        description="Imported UI annotations from Drawbridge Chrome extension.\n\nSource: .moat/moat-tasks-detail.json",
        session_id="#current"
      )

5. DEDUPLICATE
   └─ list_tasks(label="drawbridge", parent_task_id=EPIC_REF, limit=200)
   → Extract all drawbridge:{uuid} labels into a set
   → O(1) lookup per annotation instead of N API calls

6. CREATE CHILD TASKS (for each new annotation)
   └─ create_task(
        title=first_line(annotation.comment, max=120),
        description=format_description(annotation),
        parent_task_id=epic.ref,
        task_type=infer_type(annotation.comment),
        category="code",
        labels=["drawbridge", "drawbridge:{annotation.id}"],
        validation_criteria="Visual change matches the annotation screenshot and comment description",
        session_id="#current"
      )

7. REPORT results
   └─ Print summary:
      "Epic: #{seq} 'Drawbridge UI Review - Feb 11'"
      "Imported: N new tasks (skipped M duplicates, K done, J doing)"
      List each created task with #ref and task_type
```

### Screenshot Handling

Screenshots are **not** copied or moved. The resolved path (e.g., `.moat/screenshots/moat-1770769416084-swqxu5mqu.png`) is included in the task description. Agents working on the task can read the screenshot directly from that path.

Path resolution: `./screenshots/...` → `.moat/screenshots/...`

---

## Deduplication

### Why labels?

- Labels are queryable via `list_tasks(label="drawbridge:...")` using PostgreSQL's `jsonb_array_elements_text` — exact match
- No schema changes needed
- Labels survive task updates, renames, and re-parenting
- Title-based dedup unreliable since comments can be similar across annotations

### Re-run safety

Running `/bridge` multiple times is always safe:

1. **New annotations** (no matching label) → created
2. **Previously imported** (matching label exists) → skipped with note
3. **Done annotations** (status="done") → skipped at parse time
4. **Deleted gobby tasks** (label gone) → re-imported as new task

### Label convention

| Label | Purpose |
| --- | --- |
| `drawbridge-epic` | Identifies the parent epic (one per project) |
| `drawbridge:{uuid}` | Links gobby task to Drawbridge annotation ID |
| `drawbridge` | Generic tag for filtering all imported tasks |

---

## Changes to `.claude/commands/bridge.md`

**Current:** 286 lines covering MCP setup, file-based fallback, task processing with status lifecycle, three processing modes (step/batch/yolo), error handling.

**New:** ~150 lines focused solely on import. The skill becomes a thin importer that delegates all task management to gobby.

### New structure

```
---
description: Import Drawbridge Chrome extension annotations as Gobby tasks
---

# /bridge — Drawbridge Task Importer

## Purpose
Import visual UI annotations from Drawbridge (.moat/) into the Gobby task system.
.moat/ files are READ-ONLY input. Gobby tasks are the output.

## Prerequisites
- Gobby daemon running
- .moat/moat-tasks-detail.json exists
- gobby-tasks MCP tools available

## Import Flow
### Step 1: Read Annotations
### Step 2: Find or Create Epic
### Step 3: Deduplicate
### Step 4: Create Tasks (with field mapping table + type inference rules)
### Step 5: Report Summary

## Error Handling
## Re-running (idempotent)
```

### What's removed

- MCP connection check/setup for Drawbridge MCP server (no longer needed)
- Processing modes (step/batch/yolo) — gobby's task system handles execution
- Status lifecycle management (`to do` → `doing` → `done` in .moat/ JSON files)
- File-based task processing — replaced by gobby task claims and closes
- `moat-tasks.md` checkbox updates — gobby is the source of truth

### What's kept

- `.moat/` directory detection and search priority
- Screenshot path resolution logic
- Setup instructions for the Chrome extension (in error handling)

---

## Changes to `.moat/drawbridge-workflow.md`

**Current:** 802 lines as an `alwaysApply: true` workflow covering task ingestion, status lifecycle, three processing modes, batching logic, framework detection, UI pattern library, and implementation standards.

**New:** ~350 lines retaining the implementation standards that apply when an agent works on a Drawbridge-originated task.

### What's kept

- Front-end engineer persona and role description
- Screenshot validation and path resolution
- File discovery intelligence
- Framework detection & adaptation (React, Vue, Svelte, Vanilla)
- Implementation standards (design tokens, rem units, modern CSS)
- UI change pattern library (colors, layout, typography, effects)
- Accessibility guidelines
- Error handling and quality assurance

### What's removed (~450 lines)

- "Critical Workflow Requirements" — batched status updates (lines 29-118)
- Processing modes: Step/Batch/YOLO (lines 289-487)
- "Status File Management" (lines 493-525)
- "Status Transition Validation" (lines 527-567)
- Task ingestion from `.moat/` files (lines 145-157)
- All references to updating `moat-tasks-detail.json` and `moat-tasks.md`
- Concurrent file update handling
- Task announcement template (gobby has its own)
- Communication style guidelines (verbose/terse)

### Frontmatter change

```yaml
# Before
alwaysApply: true
globs:
  - ".moat/**"
  - "**/moat-tasks.md"
  - "**/moat-tasks-detail.json"

# After
alwaysApply: false
globs:
  - ".moat/**"
```

Add header note: "Tasks are managed in Gobby. Use `/bridge` to import annotations. This file provides implementation standards for working on Drawbridge UI tasks."

---

## No Backend Changes Required

The existing gobby-tasks MCP API fully supports this workflow:

- `create_task` — supports `task_type: "epic"`, `parent_task_id`, `labels`, `category`, `validation_criteria`
- `list_tasks` — supports `label` filter for dedup, `parent_task_id` for hierarchy
- `claim_task` — agents claim imported tasks the normal way
- `close_task` — standard close with commit linking
- `validate_task` — validation criteria set at import time

No new tools, models, or storage changes needed.

---

## Error Handling

| Scenario | Handling |
| --- | --- |
| No `.moat/` directory | Error with Chrome extension setup instructions |
| Empty JSON or all tasks done | "No pending tasks" with count of done tasks |
| MCP tools unavailable | Error asking user to ensure gobby daemon is running |
| Individual task creation failure | Log error, continue with remaining tasks, report in summary |
| Re-run with partial import | Dedup skips already-imported, imports only new annotations |
| Very long comment text | Title truncated to 120 chars at word boundary; full comment in description |
| Missing screenshot | Note in description: "Screenshot not available"; don't fail import |
| Tasks with "doing" status | Skip during import; mention in summary |

---

## Verification

### Manual test steps

1. **Fresh import:**
   - Ensure `.moat/moat-tasks-detail.json` exists with mix of "to do" and "done" tasks
   - Run `/bridge`
   - Verify: epic created with `drawbridge-epic` label
   - Verify: only "to do" tasks imported as children
   - Verify: each task has `["drawbridge", "drawbridge:{uuid}"]` labels
   - Verify: task descriptions include screenshot paths and selector info

2. **Spot-check task_type inference:**
   - "Create job doesn't work. Button remains disabled." → `bug`
   - "Might want a card up here for unimportant memories..." → `feature`
   - "the graph animation toggle should go up here with the other options" → `task`

3. **Re-run (idempotent):**
   - Run `/bridge` again without changes
   - Verify: all tasks skipped as duplicates
   - Verify: no new epic created
   - Verify: summary shows "0 new, N skipped"

4. **New annotations added:**
   - Add a new entry to `moat-tasks-detail.json`
   - Run `/bridge`
   - Verify: only the new annotation imported
   - Verify: existing tasks untouched

5. **No `.moat/` directory:**
   - Run `/bridge` from a directory without `.moat/`
   - Verify: helpful error with Chrome extension setup instructions

6. **Task workflow integration:**
   - Claim an imported task via gobby
   - Verify: screenshot path in description is readable
   - Close the task with a commit
   - Verify: standard gobby close flow works

---

## File Summary

| File | Action | Lines Before | Lines After |
| --- | --- | --- | --- |
| `.claude/commands/bridge.md` | Rewrite | 286 | ~150 |
| `.moat/drawbridge-workflow.md` | Trim | 802 | ~350 |

**No new Python source files.** The import logic is entirely prompt-driven, using existing gobby MCP tools (`create_task`, `list_tasks`).

---

## Risks and Mitigations

| Risk | Mitigation |
| --- | --- |
| Large annotation sets (50+) hit MCP rate limits | Import sequentially; report progress per task |
| Generic titles ("Freeform Rectangle Task") reduce task readability | Use `comment` first line as title; full comment in description |
| Screenshot paths break if `.moat/` moves | Paths are relative to project root; documented in description |
| Users expect `/bridge` to still process tasks directly | Clear messaging: "Tasks imported — use gobby task workflow to manage" |
| `alwaysApply: false` means agents miss implementation standards | Add note in imported task descriptions pointing to drawbridge-workflow.md |

---

## Out of Scope

- **Drawbridge MCP server** — no longer needed for import; may be deprecated separately
- **Bidirectional sync** (gobby → `.moat/`) — one-way import only; Drawbridge JSON is read-only input
- **Automatic re-import on file change** — manual `/bridge` invocation required
- **Screenshot embedding** — paths are referenced, not copied into gobby storage
- **Task expansion** — users can run `/gobby expand` post-import for complex tasks
