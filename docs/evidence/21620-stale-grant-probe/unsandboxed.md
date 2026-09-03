# 21620/21667 unsandboxed counterpart (coordinator S#11333, real interactive zsh, no SRT)
date: Thu Sep  3 07:05:10 UTC 2026
cwd: /Users/josh/.gobby/worktrees/gobby/probe-21620-stale-grant-receipt
daemon: restarted 2026-09-03T07:00:41Z (restart #4), fencing epoch 372 (was 371 when the sandboxed probe's grant was issued at 06:45:27Z)

## gcode search-symbol CloseEvaluation src/gobby/mcp_proxy/tools/tasks --limit 3
```
src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_preview.py:17-157 [class] CloseEvaluation sig=class CloseEvaluation:
src/gobby/mcp_proxy/tools/tasks/_close_evaluation_support.py:61-79 [method] CloseEvaluationFingerprint.capture sig=def capture(
src/gobby/mcp_proxy/tools/tasks/_close_evaluation_support.py:47-79 [class] CloseEvaluationFingerprint sig=class CloseEvaluationFingerprint:

continue: gcode search-symbol CloseEvaluation src/gobby/mcp_proxy/tools/tasks --limit 3 --offset 3
exit=0
```
grant-lock-errors: 0

## daemon-side epoch at the time of both post-restart runs (read through the daemon storage layer, non-secret fields only)
```
deployment_runtime.fencing_epoch = 372, epoch_updated_at = 2026-09-03 06:59:48Z (restart #4)
sandboxed probe grant: fencing_epoch = 371, issued_at = 1788417927 (2026-09-03T06:45:27Z), expires_at = 1788421827, unchanged after every run
```

## sandboxed probe run
- agent run 270e163d-506b-476f-8d6a-1060464ecd27 (codex, SRT sandbox, worktree probe-21620-stale-grant-receipt), receipt: docs/evidence/21620-stale-grant-probe/receipt.md, commit bb7a2e0347
- pre-restart exit 0; post-restart exits 0/0/0 with the stale epoch-371 grant against the epoch-372 daemon; grant-lock-errors 0; no file-write-create violation under gcode-runtime
