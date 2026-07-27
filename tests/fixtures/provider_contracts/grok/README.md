# Grok Provider Contract Captures

Captured against Grok `0.1.216 (b139744655)` on 2026-05-22.

The focused shell-outcome fixture was recaptured against Grok
`0.2.67 (03e13f99286)` on 2026-07-17. Current normal `PostToolUse` payloads expose
`toolResult.exit_code`; both exit `0` and exit `7` were observed. The earlier
0.1.216 payloads expose only `toolResult.status: "completed"` for both outcomes
and remain as the ambiguous legacy contract fixture.

The `PostCompact(source=manual)` record was captured against Grok Build
`0.2.112` on 2026-07-27 after successful conversation-state replacement.

Capture procedure:

1. Record `grok version`, `grok --help`, `grok agent --help`, `grok models`,
   and scrubbed `grok inspect --json` summaries.
2. Summarize `~/.grok/models_cache.json` without committing tokens or etags.
3. Run ACP with `grok agent --no-leader --always-approve stdio`.
4. Send `initialize`, `authenticate`, `session/new`, `session/prompt`,
   `session/load`, and a second `session/prompt`.
5. Install temporary hook file
   `~/.grok/hooks/gobby-contract-probe-15038.json`, capture stdin/env payloads,
   then remove it.
6. Map `sessionId` to
   `~/.grok/sessions/<encoded-cwd>/<session-id>/{summary.json,updates.jsonl,chat_history.jsonl}`.

Do not commit raw `chat_history.jsonl`; it includes full system prompts.
