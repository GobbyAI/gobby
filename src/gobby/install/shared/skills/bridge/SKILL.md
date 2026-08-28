---
name: bridge
description: Process Drawbridge UI annotation tasks from .moat/ files. Use when the user runs /bridge, mentions Drawbridge or moat tasks, wants browser UI annotations turned into code changes, or wants a live annotate-implement-verify session (`bridge live`).
version: "1.2.0"
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

## Invocation

- `/gobby bridge [step|batch|yolo]` (Codex: `$gobby bridge ...`) — one-shot
  processing of pending annotations using the sections below.
- `/gobby bridge live`, or any bridge request containing the standalone word
  "live" — jump to `## Live Mode`.

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
2. **Reconcile interrupted work** before starting: for every pre-existing
   `"doing"` entry, inspect the current implementation and either finish it and
   mark it `"done"` or reset it to `"to do"` for processing in this run. Never
   silently skip an entry because a prior run left it `"doing"`.
3. **Analyze dependencies** before starting: pronouns ("that button", "it")
   and descriptive references ("the blue button", "the updated header") mean
   tasks build on each other — process them in order.
4. **Pick a processing mode** (ask if the user didn't specify):
   - `step` — one task at a time with approval
   - `batch` — group related tasks, single approval per group
   - `yolo` — process all tasks autonomously (use with caution)
5. **Implementation standards**: prefer design tokens/CSS variables over
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

## Live Mode

A continuous annotate → implement → verify loop: the user stays in the browser
annotating while you process each round as it arrives. The generic
`live-session` skill owns the umbrella task, turn-end exemption, validation,
commit, and close lifecycle. Bridge owns annotation processing.

### Setup

1. Locate the `.moat` files (same search order as `## Task Files`); if
   missing, follow `## When Files Are Missing` and stop.
2. Load `live-session` with `gobby-skills:get_skill`, then execute
   `live start "Drawbridge annotations — <scope or date>"`. It handles
   re-entry, mixed-claim refusal, authorization, task creation, and claiming.
3. Reconcile `.moat` statuses: finish or reset every pre-existing `doing`
   entry before accepting new annotations.
4. Merge `"bridge"` into the `additional_skills` session variable so this
   skill reloads after a context compaction.
5. Process any already-pending `"to do"` entries as round 1.

### Round Loop

1. Read `moat-tasks-detail.json`. New work = entries with status `"to do"`
   whose id you have not processed this session. The extension rewrites the
   whole array — never detect new work by array position or count; the
   statuses in the file are the recoverable loop state.
2. Check for a sentinel first: a new entry whose trimmed comment matches,
   case-insensitively, exactly `done`, `stop`, or `end session` (optional
   trailing `.` or `!`). Mark it `done` without implementing it and go to
   Wrap-Up.
3. Read screenshots for the new entries only — never re-read processed ones.
4. Process the round as one dependency-ordered group following
   `## Status Lifecycle` exactly. No chat approvals mid-loop: the annotation
   is the instruction and the browser is the review surface.
5. Verify: with a browser MCP connected (chrome-devtools, playwright),
   snapshot the affected element and check for console errors; otherwise the
   user verifies visually via hot reload.
6. Record the ids as processed, report a one-line round summary, and wait
   for the next round.

### Waiting for Annotations

**Never end the turn to wait** — while the umbrella task is claimed, ending
the turn is blocked, and every wait must be a tool call.

- **Claude Code**: use the `Monitor` tool with an until-condition on
  `.moat/moat-tasks-detail.json` changing (content hash or mtime), polling
  every 5-10 seconds, bounded at ~2 minutes per arm; re-arm when it expires
  with no change.
- **Other CLIs**: use the harness's blocking file-change monitor as a tool
  call, bounded at ~2 minutes per arm. If the monitor yields a process handle,
  resume it with the harness's process-wait tool until it reports a change or
  reaches its deadline. Do not run shell sleep loops.
- The watcher exits on **any** file change (the sentinel is itself a file
  change — detect it in the loop, not the watcher) and if the file
  disappears (extension disconnected → Wrap-Up).
- Idle timeout: after ~5 consecutive empty waits (≈10 minutes with no
  changes), go to Wrap-Up and tell the user how to start a new live session.
- If the harness offers neither mechanism, say live mode is unsupported
  there and fall back to one-shot `/gobby bridge` per batch.

### Long Sessions

- Checkpoint commits of accumulated verified work (same task ref) are fine
  at any time; never close the task mid-session.
- Under context pressure (~10+ rounds): checkpoint commit, then call
  `gobby-sessions:set_handoff` with concise current state and next steps and
  `clear_session=false`. In a terminal session that call comes back as a
  rejected or cancelled tool use attributed to the user. That is the daemon
  interrupting the turn to deliver the compaction command, never a refusal: do
  not stop, do not ask the user about it, and resume from the continuation
  prompt. On resume, rebuild the processed set from the file itself — every entry
  with status other than `"to do"` is already handled.

### Wrap-Up

1. Reconcile: no entries may remain `"doing"`.
2. Execute `live done`. The live-session skill owns validation, the final
   task-linked commit when changes exist, and `close_task`.
3. Report: rounds processed, annotations implemented, files changed.
