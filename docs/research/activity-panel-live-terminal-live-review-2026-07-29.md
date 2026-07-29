# Activity Panel Live Terminal — Live Review

Date: 2026-07-29

Plan reviewed: `.gobby/plans/activity-panel-live-terminal.md`

Research task: #19243

## Scope and evidence

The review compared the plan's acceptance criteria with the current terminal
implementation, the running daemon, focused frontend tests, the production
bundle, repository history, and the original annotated UI capture.

Runtime environment:

- Gobby 0.5.0 daemon running on HTTP `60887` and WebSocket `60888`.
- Authenticated `tmux_sessions_list` returned 24 live/dead rows across the
  `default` and `gobby` sockets.
- An authenticated attach to live default-socket session `266` succeeded,
  delivered an ANSI-bearing `terminal_output` frame before the attach result,
  and detached successfully.
- The in-app browser had no connected page, so this session could not repeat
  the current pixel/mobile interaction smoke. Commit #19155's browser
  verification remains applicable because `b4f176ba8..HEAD` has no changes in
  the terminal frontend, tmux hook, PTY bridge, or WebSocket tmux handler.

## Findings

| Plan promise | Observed behavior | Disposition |
| --- | --- | --- |
| ActivityPanel side-tab terminal with a usable live renderer | The original annotated capture showed a blank, unfitted terminal with opaque labels. The delivered product now routes the registered `terminal` action to a bottom dock with expand/collapse, clearer labels, session/provider titles, and explicit input mode. | Deliberate supersession tracked and completed by #19155 at `b4f176ba8`. |
| Attach streams ANSI output and composer input reaches the pane | Current authenticated daemon smoke attached to session `266`, received ANSI output, and detached cleanly. #19155 fixed the three original live blockers: missing `TERM` for `tmux attach-session`, missing `SIGWINCH` after PTY resize, and wterm's zero-size initialization. | Implemented and live-verified by #19155; current transport smoke passed. |
| Renderer and dock resize/repaint reliably | Current focused TerminalDock, TerminalTab, TerminalView, activity-hook, and ChatPage tests pass; type-check and production build pass; the build contains `ghostty-vt` wasm and `vendor-wterm` CSS/JS assets. | Implemented by #19155, except for the fallback below. |
| `refreshTerminal()` provides a no-dimensions repaint fallback | `useTmuxSessions` sends `tmux_refresh_client`, while the server dispatch table has no handler. The running authenticated daemon returned `Unknown message type: tmux_refresh_client`. | Open fix #19262, filed directly under epic #19236. |
| Socket-qualified session listing and attach | The live list contained rows from both sockets, and the default-socket attach used the socket-qualified request successfully. Current focused hook/join tests cover collisions and socket switching. | Implemented; no new divergence found. |

## Corrected false lead

#19261 was filed after a temporary detached tmux session appeared to be
unattachable. The user's global `destroy-unattached on` policy had destroyed
that temporary session immediately. Existing session `266` resolves through
all exact target forms, and the focused exact-name tmux integration tests pass.
#19261 was closed as `obsolete` with no code change.

## Validation

- `npm test -- src/hooks/__tests__/useTmuxSessions.test.ts src/components/activity/terminal/__tests__/terminalSessions.test.ts src/components/activity/terminal/__tests__/TerminalView.test.tsx src/components/activity/terminal/__tests__/TerminalKeysBar.test.tsx src/components/activity/terminal/__tests__/TerminalSessionPicker.test.tsx src/components/activity/terminal/__tests__/TerminalTab.test.tsx`
  — 6 files, 43 tests passed.
- `npm test -- src/components/activity/terminal/__tests__/TerminalDock.test.tsx src/components/activity/terminal/__tests__/TerminalTab.test.tsx src/components/activity/terminal/__tests__/TerminalView.test.tsx src/components/activity/__tests__/useActivityPanel.test.tsx src/components/chat/__tests__/ChatPage.activityPanel.test.tsx`
  — 5 files, 76 tests passed.
- `npm run type-check` — passed.
- `npm run build` — passed; Ghostty wasm and wterm assets emitted.
- `GOBBY_TEST_PROTECT=1 uv run pytest tests/agents/test_tmux_integration.py::test_get_session_requires_exact_session_name tests/agents/test_tmux_integration.py::test_kill_session_requires_exact_session_name -q`
  — 2 tests passed.

## Plan disposition

The plan is archived as a superseded implementation artifact. Its original
live failures and dock redesign are tracked by completed task #19155. The only
remaining current divergence is tracked by #19262 under epic #19236.
