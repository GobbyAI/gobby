# ACP Contract Fixtures

These JSONL files are golden stdout streams captured from real ACP CLI runs.
Filenames pin the provider and CLI version, for example:
`gemini-0.40.1-session-new-prompt.stdout.jsonl`.

To re-record a fixture:

1. Capture the current CLI version with `gemini --version`, `qwen --version`,
   or `grok version`.
2. Start the CLI in ACP mode with `gemini --acp`, `qwen --acp`, or
   `grok agent --no-leader --always-approve stdio`.
3. Send the same JSON-RPC request sequence used by
   `tests/adapters/test_acp_contract_fixtures.py`: `initialize`, then
   `session/new` or `session/load`, then `session/prompt`.
4. Save stdout as JSONL and update the pinned version in the filename.

Grok fixtures include `authenticate` after `initialize`, because Grok advertises
auth methods and requires local cached-token or API-key auth before session
creation. Grok tool prompts can also emit client-directed JSON-RPC extension
requests such as `terminal/create`; keep those records when they are part of the
transport contract.

Keep fixtures minimal and scrubbed. Do not include user secrets, local project
paths, auth tokens, or large model output.
