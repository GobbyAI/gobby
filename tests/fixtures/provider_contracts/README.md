# Provider Contract Fixtures

These fixtures are scrubbed provider-contract captures for provider spike tasks.
They are intentionally structural: enough to implement adapters without
committing private paths, auth tokens, email addresses, raw model transcripts, or
large outputs.

Rules:

1. Keep provider behavior out of these fixtures. They are evidence, not adapter
   code.
2. Commit JSONL samples only after scrubbing private paths and secrets.
3. Do not commit AGY `.pb` conversation files or Grok `chat_history.jsonl`
   records containing full system prompts.
4. Version filenames or fixture metadata with the CLI version that produced the
   capture.
