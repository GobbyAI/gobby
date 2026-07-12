---
name: bridge
description: Process Drawbridge UI annotation tasks from .moat/ files. Use when the user runs /bridge, mentions Drawbridge or moat tasks, or wants browser UI annotations turned into code changes.
version: "1.0.0"
category: integration
triggers: bridge, drawbridge, moat, ui annotation, annotation tasks, visual feedback
metadata:
  gobby:
    audience: all
    depth: 0
---

# Bridge — Drawbridge UI Annotation Tasks

Process visual UI annotation tasks created with the
[Drawbridge](https://github.com/breschio/drawbridge) Chrome extension.

Drawbridge is **file-based**: the extension writes task files into the
project's `.moat/` directory and the agent reads and updates those files
directly. There is no Drawbridge MCP server — the extension never shipped
one, so do not attempt to build, configure, or connect one. (If an MCP-based
annotation flow is wanted, the published alternative in this niche is
`agentation-mcp` on npm.)

## Task Files

Search in this order:

1. `.moat/moat-tasks-detail.json` (current directory)
2. `moat-tasks-detail.json` (current directory — legacy)
3. `../.moat/moat-tasks-detail.json` (parent directory)

- **Primary data**: `moat-tasks-detail.json` — full task details
- **Human-readable**: `moat-tasks.md` — task checklist
- **Screenshots**: `.moat/screenshots/` — visual context

Each task carries:

- `comment` — the user's instruction
- `selector` — CSS selector for the target element
- `screenshotPath` — visual context; JSON stores relative paths, so resolve
  `./screenshots/...` and `screenshots/...` to `.moat/screenshots/...`
- `status` — `"to do"`, `"doing"`, or `"done"`

## Status Lifecycle

Every task follows `"to do" → "doing" → "done"`, updated in the files:

1. **Before implementing**: set the task's JSON `status` to `"doing"`.
2. Implement the change.
3. **After implementing**: set JSON `status` to `"done"` and tick the
   matching `moat-tasks.md` checkbox to `[x]`.

Never skip the `"doing"` update. The extension may regenerate the markdown
after JSON changes — always re-read `moat-tasks.md` before editing it, and if
an edit fails, re-read and verify the checkbox state before retrying.

## Processing

1. **Load tasks** from `moat-tasks-detail.json`; batch-read all referenced
   screenshots up front.
2. **Analyze dependencies** before starting: pronouns ("that button", "it")
   and descriptive references ("the blue button", "the updated header") mean
   tasks build on each other — process them in order.
3. **Pick a processing mode** (ask if the user didn't specify):
   - `step` — one task at a time with approval
   - `batch` — group related tasks, single approval per group
   - `yolo` — process all tasks autonomously (use with caution)
4. **Implementation standards**: prefer design tokens/CSS variables over
   hardcoded values, `rem` over `px`, match existing code patterns, and keep
   accessibility in mind (ARIA, keyboard navigation, contrast). Reference
   code as `path:line`. Ask when a task is ambiguous.

## When Files Are Missing

- **No `.moat/` directory**: the Chrome extension is not connected to this
  project (or you are in the wrong directory). The user connects it by
  opening the project in the browser, pressing Cmd+Shift+P / Ctrl+Shift+P,
  and selecting the project directory — Drawbridge then creates `.moat/`.
- **No pending tasks**: all annotations are done or none exist yet. New
  annotations are created in the browser by pressing `f` and clicking an
  element.
- Report which paths were checked and the current working directory so the
  user can tell which case they are in.
