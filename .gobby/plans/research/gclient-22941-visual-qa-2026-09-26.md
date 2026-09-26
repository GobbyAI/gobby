# gclient #22941 exact-source visual QA (2026-09-26)

Scope: the dark and light visual QA the #22941 (Resolve Chrome visual-QA status
and pane-label discrepancies) review asked for. It covers the attention count and
⍾ glyph, the unreachable marker, the pane footer and absent segments. It was run
by gobby#14607 under #22944 (Consolidate gclient Chrome design and interaction
polish after preview approval). This report and its evidence contain no #22944
redesign code.

**Verdict: accept.** All four items render as the landed goldens and the user
guide describe, in both themes. The findings at the end are outside those four
items and do not block the #22941 close.

## What was rendered

| Fact | Value |
| --- | --- |
| Rendered source | `09b0f41781` ([gobby-#22941] fix: land reviewed Chrome alert and close-cap corrections) |
| Build tree | managed worktree `qa-22941-visual` at `ce81088dec` (0.5.0) |
| Source identity | `git diff --quiet 09b0f41781 HEAD -- crates Cargo.toml Cargo.lock rust-toolchain.toml .cargo` exited 0 |
| Binary | debug `cargo build -p gobby-client --bin gclient`, sha256 `0363abd1bdba898fd2db56c123d5d9da8dadfea15ad43b3d061b156ce99186af` |
| Goldens | `cargo nextest run -p gobby-client --test screens` with `GOBBY_UPDATE_SCREENS` unset: 5 tests run, 5 passed |

The LM (gobby#14556) granted one heavy slot for these two commands only. Both
used the same `CARGO_TARGET_DIR`.

Live renders ran against the running daemon in a private tmux server
(`tmux -L qa -f /dev/null`, 120×40, `COLORTERM=truecolor`). Each theme had its
own scratch `GOBBY_HOME` holding only `client/prefs.toml` and a copy of
`machine_id`. Clients used `--frame-delivery proxy` against a throwaway workspace,
`qa-22941`, created for the run and closed after it. No live pane or agent was
attached, typed into, or promoted. Every client exited 0.

Frames were captured with `tmux capture-pane -e -p` and decoded into text rows
(`NN |…|`) and colour runs (`NN : fg/bg[+b bold][+d dim]*cells`). tmux does not
re-emit SGR at the start of a row, so the decoder carries colour state from each
row into the next. An earlier per-row reset misread the pane's left border and
the footer title as default colours. The files below use the corrected decoder.

Evidence files are in `gclient-22941-visual-qa/`:

| File | State |
| --- | --- |
| `dark-pane.txt`, `light-pane.txt` | Pinned sidebar, one bare shell pane focused |
| `dark-attention.txt`, `light-attention.txt` | A live agent blocked on the user at capture time |
| `dark-unreachable.txt`, `light-unreachable.txt` | Client pointed at a dead port (`127.0.0.1:9`) |
| `finding-light-*.txt` | The two defects under Findings |

## Results

Hex values are the theme tokens: dark warning `#ffb330`, light warning `#8d5000`,
dark destructive `#ff73c3`, light destructive `#750047` (`theme.destructive`,
magenta in the deutan-safe palette). The status line base is dark `#232521` and
light `#e5e7e3`, and its informational text is subtext, dark `#b0b2ae` and light
`#474845`.

| Item | Dark (live) | Light (live) | Golden at 09b0f41781 |
| --- | --- | --- | --- |
| Attention count | `⍾ 1 needs you · 20 agents · 0 terminals` with the count in `#ffb330` | `⍾ 1 needs you · 20 agents · 1 terminal · —` with the count in `#8d5000` | `status_segments.txt` row 39: `⍾ 1 needs you · 2 agents · 0 terminals · fable-5.1-xhigh …` |
| ⍾ on the sidebar | machine row `⍾ MBP · local` and project row `⍾ gobby (0.5.0 ↑168)`, glyph `#ffb330` | same rows, glyph `#8d5000` | sidebar unit tests expect ` ⍾ ` on attention rows |
| Unreachable | `× Daemon unreachable · retry in 2 s` in `#ff73c3` | same text in `#750047` | `daemon_unreachable.txt` row 39: `× Daemon unreachable · retry in 3 s │ — … — · —` |
| Bare-terminal footer | `└ zsh · Focused ─… gclient 0:3:0:0 ┘`, bold accent `#a7d91d` | same text, bold accent `#4c7200` | `unnamed_pane.txt` row 38: `└ zsh · Focused ─… gclient ┘`; `pane_edges.txt` row 38: `└ term-bet · Focused ─… tmux ┘` |
| Agent-pane footer | not rendered live | not rendered live | `status_segments.txt` row 38: `└ Codex · Focused ─… gclient ┘`; `pane_edges.txt` row 02: `Codex (alpha#1742) · Read-only · gclient` |
| Absent segments with a focused pane | `20 agents · 1 terminal · — … — · —` | same | `pane_edges.txt` row 39: `1 agent · 1 terminal · — … — · —` |
| Absent segments with no pane | omitted: `20 agents · 0 terminals` | omitted in the unreachable frame | `empty_workspace.txt` row 39: `0 agents · 0 terminals` |
| Machine detail dimming | ` · local` in `#71726e`, undimmed | ` · local` in `#858783`, undimmed | the landed undim fix, 8301e88e51 |

## Gaps and differences, stated

- **Attention rows.** The count, the machine row and the project row were seen
  live in both themes. The blocked agent's own sidebar row was off screen in both
  attention frames, so count equals ⍾ agent rows was not verified live. The
  plural `⍾ N need you` was not observed live. Both are covered by the passing
  unit tests and goldens.
- **Agent-pane footer.** Golden only. A live render would need a live agent pane
  attached to the QA workspace, and QA does not adopt anyone's live panes.
- **Unreachable countdown and segments.** The golden shows `3 s` and dashes
  because its test workspace has panes and a clock fixed 3 s ahead. The live
  client never connected, so it had no panes and the segments were omitted. The
  countdown read 2 s at capture.
- **The one dim cell.** Each sidebar row dims only the blank cell between the
  state glyph and the name, which draws nothing. No text is dimmed.
- **20 agents against 18 rows.** The status total is machine-wide: it counts
  `sidebar().agents` in `render_status_line` (`crates/gclient/src/ui/status.rs`).
  The agent list admits only the focused project through `admits`
  (`crates/gclient/src/ui/sidebar/agents.rs:130`). A full scroll of the list
  found 18 gobby agent rows, so the other 2 belong to other projects. This
  matches the approved machine-wide totals pick.
- **Warning glyphs.** The paused `‖` uses the palette's `yellow` and the
  attention `⍾` uses `peach`. Both resolve to the same warning hex in each theme,
  so only the glyph tells them apart.
- **CPU.** The debug build used about one core while it decoded the daemon's
  event stream, with or without panes. That is a debug-build cost. It did not
  change what was drawn.

## Findings for #22944

None of these touches the four #22941 items. All are in gclient code that #22944
already owns, so they are fixed there with no new task.

Confirmed in code:

1. **A failed projects fetch is never retried.** `apply_sidebar_fetch`
   (`crates/gclient/src/app/live_sidebar.rs`) re-queues a failed sessions fetch
   and nothing else. When `GET /api/projects` timed out at 23:31:42Z
   (`sidebar row refresh failed what=projects … did not answer GET /api/projects
   in time`), the light client kept an empty project list and a tab labelled
   `Project:qa-shell` for over 40 s, until it was restarted
   (`finding-light-projects-never-retried.txt`).
2. **The connecting timer never ticks.** `StartupStages::now` advances only in
   `mark_running` and `mark_done` (`crates/gclient/src/app/startup_stages.rs`).
   A running stage therefore always reads 0.0 s. Live, the light client showed
   `◐ connecting · first frame · 0.0 s` for over 60 s
   (`finding-light-connecting-timer-frozen.txt`). #22944 slice 1 removes this
   status segment, and slice 1 must not carry the frozen clock into its
   replacement.
3. **GOBBY_HOME does not redirect the client log.** `logging::init`
   (`crates/gclient/src/logging.rs`) builds the path from `$HOME/.gobby`, while
   prefs honour `GOBBY_HOME`. The scratch-home QA clients appended their startup
   and teardown lines to the real `~/.gobby/logs/gclient.log`.

Observed, not yet explained:

4. **The first-frame stage can stall indefinitely.** While the daemon was slow
   (project and session fetches and `terminal_attach` requests timing out), one
   light start with a pane present stayed in the first-frame stage for over 60 s
   and never completed. A second start drew the tab with no pane at all. A start
   into a healthy daemon completed first frame in 116 ms. Under #22944 slice 1 the
   splash is the whole frame until startup finishes. A stalled stage would then
   leave only the splash on screen with no status line. Slice 1 needs a way out,
   either an attach failure or a stage deadline, before it ships.
5. **An automatically created shell tab vanished.** In the dark run, the shell
   tab gclient opened in the empty workspace was removed on the daemon side at
   23:28:16Z (workspace `602fe4ef-4b94-4720-a901-069bdad2e543`) with no log
   record. Just before that, the status line briefly read `2 terminals`.
   Replaying the same wheel input on a fresh tab did not reproduce it. This went
   to the #22883 lane, which now owns automatic shell creation.

## Cleanup

The `qa-22941` workspace was closed, which killed its owned shells. The private
`-L qa` tmux server was stopped after every client exited 0. No binary was
promoted, and Josh's prefs, panes and `~/.gobby/bin` were not touched.
