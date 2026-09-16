# Droid Stream JSON Fixtures

Captured contract fixtures for Factory Droid `0.106.0` stream-json mode. That version
is provenance, not a preference: these files record what 0.106.0 actually emitted, so
the pin stays until the fixtures are re-captured (#22408).

Each fixture is intentionally minimal and records the event shapes Gobby normalizes for
web-chat tests: session initialization, text, tool calls/results, permission requests,
thinking, errors, malformed lines, and EOF-before-result behavior.

## Re-capturing

Capture through `gobby-agents:spawn_agent` with `provider="droid"`. Direct provider
launches are blocked by the `block-direct-provider-launch` rule, which has no
per-command or environment bypass.

The command that produced the 0.106.0 fixtures is recorded here only as history:

```bash
droid exec --input-format stream-json --auto low --cwd /tmp/gobby-droid-smoke
```

It is not runnable today, for two independent reasons. The rule above forbids invoking
the CLI directly, and Droid 0.219.0 rejects `--input-format stream-json` unless a
matching `--output-format` is supplied, so the run dies at startup on current builds
(the same defect fixed for Gobby's own Droid spawns in `7becb987ea`).
