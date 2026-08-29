# Herdr foundation landing — live matrix

Evidence for plan `herdr-foundation-landing` leaf 2.1 (task #21125, epic #21120).
Recorded by session #11155 on 2026-08-28.

## Protocol deviation

The plan's live-window protocol called for running the merged daemon from a
`0.5.0-test` worktree against a **cloned** hub (`gobby_050test`), then landing onto
`0.5.0` only after that loop was green, with the rollback sequence rehearsed at the
end of every window.

That is not what happened. The user directed the merge to land directly, so
`841ceca7ce` (`[gobby-#21125] merge: 0.5.0-test into 0.5.0`) went onto `0.5.0` and the
matrix below ran against the **real** hub with migration 411 applied. Two consequences
follow and are recorded here rather than glossed:

1. The cloned-database window was abandoned, so **rollback was never rehearsed**.
2. Because the schema runner refuses a database newer than the binary, a rollback is
   not just a git operation. The sequence would be:
   - `git revert -m 1 841ceca7ce` (pre-merge state is `e1305021b6`)
   - `uv run gobby install` from the main checkout
   - hand-drop migration 411 from the hub before restarting.

## Environment

| Item | Value |
| --- | --- |
| Checkout | `/Users/josh/Projects/gobby`, branch `0.5.0` |
| Daemon | `python -m gobby.runner`, pid 74400, started 2026-08-28 20:18:55 CDT |
| Hub schema | 411 (real hub, not a clone) |
| Ports | daemon 60887, websocket 60888, UI 60889 |
| Clocks | the hub stores UTC; local is CDT (UTC−5). Timestamps below are CDT unless marked. |

## Matrix

### Row 1 — startup and health

`GET http://localhost:60887/api/health` → `200` in 0.01 s,
`{"status":"ok","degraded_services":[], ...}`.

The `gterm` host is live: the attention roster reports a native terminal whose attach
block carries `frame_host_epoch 991d2feb-20e8-49d0-b4a3-b0bb3ff9a3d4`,
`host_socket /Users/josh/.gobby/gterm-frames.sock` and `host_terminal_id ht-31`.

### Row 2 — tmux spawns with no backend specified

Verified earlier in this session. Confirmed again here: the panel's own data source
(`terminal_list` over the websocket) returns 8 live `tmux` rows, including agent
terminals such as `85abe7ff` (`gobby-claude-d0`, run `92436fba`), which reached its
first turn and answered prompts.

### Row 3 — `send_keys`

`gobby-sessions:send_keys` delivered a multi-line prompt plus Enter to session
`74f0d368` at 20:32:41; the agent acted on it and raised the dialog used in row 5.

### Row 4 — web terminal fidelity

Recorded in an earlier window of this session (attach history, scrollback, no-op
resize, alternate screen, large paste).

**Currently blocked.** As of 20:33–20:45 the web UI at `http://localhost:60889/` pins
its renderer process at 98–100 % CPU on load (24 of 24 samples over 46 s between
97.9 % and 100.0 %, renderer pid 77752). Browser-level CDP calls still answer in
~230 ms, but `evaluate_script`, `take_snapshot` and even `navigate_page {reload}` all
hang, so the page cannot be driven or re-verified. Filed as **#21204**. The daemon and
UI server are healthy throughout (`GET /` → 200 in 0.01 s), and the spin began at page
load, before this session created any terminal.

### Row 5 — attention episode and deliberate response

Verified end to end in this window, and it is also the live proof for #21201.

1. At 20:32:41 the probe agent raised a real Claude Code `AskUserQuestion` dialog
   ("Which probe path should I take?", `❯ 1. alpha`, `2. beta`, `Enter to select`).
2. A sampler captured the pane every 5 s for 105 s (20:32:41 → 20:34:26, 22 samples).
   The dialog was present in **all 22 samples** with no answer produced.
   `auto_enter_agent_interval_seconds` is 30, so the periodic-Enter path had roughly
   3.5 opportunities and took none. The dialog was still standing at 20:37:45.
3. `GET /api/attention/roster` reported the episode as actionable:
   `attention_id 968af40b-0d31-4098-8ec4-0aea131ee7ee`, `reason: question`,
   `kind: actionable`, `state: blocked`, with all four parsed options in the payload.
4. `POST /api/attention/run:92436fba.../respond` with
   `{"attention_id": ..., "fingerprint": ..., "answer": {"option": 1}}` returned
   `200 {"status":"accepted"}`. The agent then printed `PROBE-ANSWER=alpha` and the run
   completed with status `success`.

The answer was delivered through the respond API rather than a browser click, because
of the row 4 blocker (#21204). That is the same endpoint the web respond control posts
to.

### Row 6 — restart reconciliation

Recorded in an earlier window of this session: a daemon restart with agents live
reconciled the runs rather than parking them, and their terminals stayed attachable.

### Row 7 — terminate finalises the terminal row

Verified earlier: `kill_agent` removed the tmux session, `terminals` row `9a0928bf`
moved to `exited`, and the run was finalised.

Confirmed independently in this window on a non-agent terminal: `terminal_kill` for
`820365aa` returned `{"success": true}` and the row is now `exited`.

### Row 8 — explicit `backend: native` spawn

`spawn_agent(provider="claude", isolation="none", terminal_backend="native")` produced
run `443ed572-63fe-4296-88c7-a21ddbbe9bf9` with terminal
`9676db30-bfcd-48d7-9fac-ca420c651af1`.

Result: **a live native terminal, not a silent tmux fallback.**

- `terminals` row: `state=live`, `backend=native`.
- No tmux session named `9676db30…` exists on the gobby socket
  (`tmux -L gobby list-sessions` lists only the four unrelated sessions).
- The roster's attach block for it names the gterm host
  (`frame_host_epoch`, `host_socket`, `host_terminal_id ht-31`), so the frame stream is
  reachable through the host proxy.
- `terminal_list` returns it alongside the tmux rows, so the panel's data source sees
  both backends: 9 rows total, 8 `tmux` + 1 `native`.

One cosmetic wart worth noting: the `spawn_agent` response still echoes a
`tmux_session_name` field for a native spawn (it carries the terminal id). That is the
`SpawnResult` tmux-alias the plan already assigns to the follow-on epic, not a
fallback — the row's backend and the absent tmux session both confirm it.

### Row 9 — `gclient` health probe

Fixed under #21200: `crates/gclient/src/startup.rs` probed `/api/admin/health`, which
returns 401 for a non-admin caller. `HEALTH_PATH` now points at `/api/health` and the
payload carries `gterm_host` state. Rebuilt and installed via a new inode; the probe
`~/.gobby/bin/gclient --project /Users/josh/Projects/gobby` exits 0 silently.

### Row 10 — comparison against `0.5.0`

Regressions found while running the matrix and **fixed on the landed tree** before this
leaf closed:

| Task | Fix |
| --- | --- |
| #21185, #21186, #21190, #21195 | fixed earlier in the landing |
| #21199 | fixed (`a627bb7777`) |
| #21200 | gclient health path (`1d4d7353db`) |
| #21201 | periodic Enter no longer answers question dialogs (`3814d63480`) |
| #21202 | stale-pending terminal reaper runs from the lifecycle loop (`86d9a39d35`, `bda41b8caa`, `6d421c846b`) |
| #21198 | web create falls back to the global project and reports a reason (`85ea815d09`, `be8f465677`) |

Also filed during the landing: #21191.

## Open items at close

- **#21204** — the web UI pins a renderer at 100 % CPU on load. This is the one matrix
  row that is worse in practice than a plain `0.5.0` daemon session, and it is left
  open rather than fixed because the user directed this session to wrap up without
  claiming further found work. It blocks re-verification of row 4.
- **#21198** — code is complete, committed and validated (47 pytest, 19 vitest, ruff
  and mypy clean), and the fixed branch was proven over the live websocket: a
  `terminal_create` with no `session_config` returned
  `{"success": true, "terminal_id": "820365aa-…", "backend": "tmux", "reason": null}`
  and the row landed under `project_id 00000000-0000-0000-0000-000000000002`
  (`GLOBAL_PROJECT_ID`). The task's criteria additionally require a chrome-devtools
  browser click, which #21204 makes impossible, so it is **escalated**, not closed.
- Native probe run `443ed572` from row 8 was left running.

## Rollback (not rehearsed)

```
git revert -m 1 841ceca7ce      # pre-merge state: e1305021b6
uv run gobby install            # from the main checkout
# then hand-drop migration 411 from the hub before restarting
```
