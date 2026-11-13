# Log spam ranking since restart #26 (Q2)

Researcher gobby#14435 for the PD gobby#14018, 2026-09-24. Window: 13:06:26 to 18:00 CT
(daemon PID 25724, about 21,200 hooks by `metric_snapshots.hooks_total`).

Sources are daemon.log, hooks.log, mcp.log and llm.log. errors.log is a second sink for
WARNING and above (errors.log and errors.log.1 hold 1,746 + 1,637 = 3,383 execute_hook
lines, the same count as mcp.log), so each logger is counted once, from its own file.
runtime.log and the parser-error logs have no logger field and are not ranked here.
#22866 (Log noise: demote periodic INFO lines to DEBUG, silence httpx2, capture gcode
prune stdout, sample slow-hook warnings) already covers runtime.log.

## INFO, top 10

| # | Logger (file) | Count | Template | Why it fires | Noise or symptom | Right level | In #22866 |
|---|---|---:|---|---|---|---|---|
| 1 | `hooks.inbox._consume_inbox_delivery_receipt` (hooks.log) | 3,837 | "Delivery receipt %s generation %s was stale or unknown at consumption" | A second, redundant consumption of an ack. All 2,991 distinct receipts are `acknowledged` in `hook_receipt_effects` (2,972 at gen 1 with transition 1.2 s after creation; 19 at gen 2). 3,814 lines are gen 1. `consume_pending_delivery_receipts` runs unlocked on every hook request (`servers/routes/mcp/hooks.py:529`, `asyncio.to_thread`) and races with other requests and the drain loop (`hooks/inbox.py:475`). Concurrent sweeps read the same ack file before one unlinks it, and the losers' CAS no-ops. | Symptom of a race (harmless to state because of the CAS; costs a DB UPDATE and a default-executor slot per duplicate) | DEBUG, after the race is fixed | Only as log dedup (criterion 2). The race is not covered: filed separately |
| 2 | `sessions.mailbox_delivery.dispatch_mailbox_wakes` (daemon.log) | 304 | 154 per-session "Mailbox wake dispatch: session=…", 143 "dispatch started: recipients=%d", 7 "declined" | Each durable message send | "started" is noise; per-session result is useful | "started" to DEBUG; keep per-session INFO | Yes |
| 3 | `ai.text_generation._log_generation_event` (llm.log) | 282 | "feature_llm_call \| feature=…" | Every LLM call, in its own file | Audit trail, not spam | Keep INFO | Yes (kept) |
| 4 | `hooks.receipt_redelivery._carry_forward_staged_effects` (hooks.log) | 167 | "Re-delivering hook receipt %s (generation %s) on envelope %s" | A hook response re-prepares an undelivered receipt onto its envelope | Mostly normal; its rate follows #1's race | DEBUG | Yes |
| 5 | `events.wake._refresh_and_retry_wake` (daemon.log) | 126 | 71 "Deferred wake … delivered=True", 55 "delivered=False skipped=debounced/session_active" | Deferred wake retries | Skipped ones are noise. Delivered durations are p50 9.2 s, p90 26 s, max 384 s; this includes the deferral wait and was not triaged | Skipped to DEBUG; keep delivered | Partly (skipped only) |
| 6 | `code_index.community_labeler.label_batch` (daemon.log) | 59 | One line per labelled community | Community labelling | Noise | One summary INFO per batch | Yes |
| 7 | `hooks._log_session_start_timing` (hooks.log) | 46 | SESSION_START timing | Each session start | Useful | Keep | Yes (kept) |
| 8 | `hooks.terminal_handoff_delivery` schedule and settle (hooks.log) | 36 + 36 | Handoff delivery scheduled and settled | Each terminal handoff | Low volume, useful | Keep | n/a |
| 9 | `mcp_proxy.tools.tasks._lifecycle_close._evaluate_close` (mcp.log) | 27 | Close gate evaluation | Each close attempt | Useful | Keep | Yes (kept) |
| 10 | `mcp_proxy.tools.internal.add_registry` (mcp.log) | 23 | Registry add | Tool registry changes | Low | Keep | n/a |

## WARNING, top 10

| # | Logger | Count | Template | Why it fires | Noise or symptom | Right level | In #22866 |
|---|---|---:|---|---|---|---|---|
| 1 | `servers.routes.mcp.hooks.execute_hook` | 3,031 | "Slow hook execution dominated by %s": admission_wait 1,676, rule_evaluation 1,355 (plus response 140 and others) | Hook over 5 s. admission_wait peaked at 1,188 in the 14:00 hour, which was my own subagent fan-out (session 486e595d). rule_evaluation is steady at 120-581 an hour | Symptom: the stability problem (#22864; #22870 for Stop) | Sampled WARNING | Yes (criterion 4) |
| 2 | same logger | 153 | "Retrying hook after adapter timeout": pre-tool-use 55, post-tool-use 39, subagent-stop 35 | The adapter timed out waiting for the daemon | Symptom (user-visible latency) | Keep WARNING, and keep it separate from slow-hook sampling | No: flag |
| 3 | `code_index.trigger._flush` | 41 | "gcode index timed out after Ns for project … at ~/.gobby/worktrees/gobby/epic-{stability 20, overflow 12, rust-gclient 9}" | gcode index of lane worktrees exceeds its timeout | Symptom (not profiled; not called load-induced) | Keep WARNING | No: flag |
| 4 | `workflows.engine.mcp_injections._log_inline_timeout` | 32 | "Inline mcp_call %s timed out … dispatch continues": recall_review_lessons_for_files 20, surface_memories 10, list_hubs 1 | Inline rule MCP calls over budget | Symptom (surface_memories p50 went from about 0.4 s to 1-1.5 s this afternoon) | Keep WARNING | No: flag |
| 5 | `runner_lifecycle.watch` | 14 | "Startup event-loop lag Ns \| task=…" | Startup loop-lag probe (only in the first 25 minutes) | Startup only | Keep | n/a (#22864 asks for a steady-state probe) |
| 6 | `agents.tmux.pane_monitor._check_attention_panes` | 13 | "TmuxPaneMonitor: failed to capture terminal for session <id>" | Capture of an attention pane failed | Possible symptom (gclient lane); not triaged | Keep WARNING, and add the error cause to the line | No: flag |
| 7 | `code_index.sync_worker._sync_vector_file_with_retry` | 7 | "transient vector sync failure … embedding response was invalid" | Embedding call failed and was retried | Transient | Keep | n/a |
| 8 | `hooks.inbox._drain_hook_inbox_once_locked` | 9 | "Hook inbox replay returned N for …" | Replay of an envelope got a non-2xx | Low | Keep | n/a |
| 9 | `mcp_proxy.oauth._record_token_endpoint_failure` | 4 | OAuth token endpoint failure | External MCP OAuth | Low | Keep | n/a |
| 10 | `servers.websocket.server._handle_message`, `admin._health._collect_status_item` | 3 + 3 | — | — | Low | Keep | n/a |

ERROR: `code_index.sync_worker._sync_file` 3 and `knowledge_graph.code_linker` 2.

## What #22866 does not cover

1. The receipt-consumption race behind INFO #1. #22866 criterion 2 dedups the log line,
   which would hide a real race. The fix belongs in the consumer: claim the ack file
   atomically (rename) before acknowledging, or take the drain lock.
2. The adapter-timeout retry WARNING (153), gcode index timeouts on lane worktrees
   (41), inline MCP timeouts (32) and pane-capture failures (13) are symptoms. They
   should stay at WARNING and not be swept into a demotion.
3. Hourly INFO counts for the receipt line: 606 (13h), 1,497 (14h), 623 (15h),
   551 (16h), 560 (17h).
