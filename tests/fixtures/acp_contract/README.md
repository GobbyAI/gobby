# ACP Contract Fixtures

These JSONL files are golden stdout streams captured from real ACP CLI runs.
Filenames pin the provider and CLI version, for example:
`gemini-0.40.1-session-new-prompt.stdout.jsonl`.

To re-record a fixture:

1. Capture the current CLI version with `gemini --version` or `qwen --version`.
2. Start the CLI in ACP mode with `gemini --acp` or `qwen --acp`.
3. Send the same JSON-RPC request sequence used by
   `tests/adapters/test_acp_contract_fixtures.py`: `initialize`, then
   `session/new` or `session/load`, then `session/prompt`.
4. Save stdout as JSONL and update the pinned version in the filename.

Keep fixtures minimal and scrubbed. Do not include user secrets, local project
paths, auth tokens, or large model output.
