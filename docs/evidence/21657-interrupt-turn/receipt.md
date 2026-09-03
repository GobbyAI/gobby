# 21657 receipt: live Esc-interrupt check on a real claimed Claude session

- date recorded: Thu Sep  3 06:49:36 UTC 2026
- daemon: real daemon on 0.5.0 (restart #2 at 2026-09-03T05:58:50Z, #21657 commits 3b07921890 + 7124f3626d live)
- session: Claude Code interactive session #11333 (778f9e2b-ea02-4ddb-90e8-8a1a47756047), claimed tasks incl. #21657, tmux pane %190, terminal 8bd6619a-16d8-4fe7-9b64-c26b70053ff1
- driver: session #11373 via gobby-sessions:send_keys -> Escape at 06:06:30Z (interrupted a running turn), +2 s 'ping from 11373' + Enter
- reply: one line ('pong: ping received; the Esc interrupt landed at 06:06:30Z ...'), then the turn ended
- Stop hook outcome: the gobby stop gates allowed the stop (rule interrupt-initiated-turn, result allow); the only re-prompt came from the native Claude Code /goal hook, which is outside gobby's turn_end gates; the Stop hook returned its decision without error: Claude Code surfaced only the /goal hook feedback, and neither ~/.gobby/logs/hooks.log nor errors.log carries an error for the session in the 06:06-06:08Z window

## rule-allow-audit.jsonl row (verbatim)
```json
{"event":"stop","latency_ms":0.0,"result":"allow","rule_name":"interrupt-initiated-turn","session_id":"778f9e2b-ea02-4ddb-90e8-8a1a47756047","timestamp":"2026-09-03T06:06:56.886725+00:00"}
```

## daemon.log line (verbatim, local time UTC-5)
```
2026-09-03 01:06:56 - INFO     - workflows.engine.evaluation._suppress_interrupt_turn_end_blocks - Suppressed 2 turn_end stop gate(s) for interrupt-initiated turn (session 778f9e2b-ea02-4ddb-90e8-8a1a47756047): require-task-close, require-epic-tree-close
```

## errors.log growth across the check
```
errors.log lines mentioning 778f9e2b or Stop between 06:06Z and 06:08Z: 0
```
