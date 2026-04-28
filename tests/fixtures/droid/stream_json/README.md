# Droid Stream JSON Fixtures

Captured contract fixtures for Factory Droid `0.106.0` stream-json mode.

Capture command template:

```bash
droid exec --input-format stream-json --auto low --cwd /tmp/gobby-droid-smoke
```

Each fixture is intentionally minimal and records the event shapes Gobby normalizes for
web-chat tests: session initialization, text, tool calls/results, permission requests,
thinking, errors, malformed lines, and EOF-before-result behavior.
