# Test Quality

The `gobby test-quality` command audits tests for weak assertions, brittle
patterns, and suppression debt. It is a focused static analyzer for tests, not a
replacement for pytest.

## Mental Model

The analyzer walks test files, reports issues with severity and stable
fingerprints, and can compare the current report to a baseline. The baseline lets
Gobby tolerate known debt while failing on new issues at a chosen severity.

Use the audit before adding or expanding tests in an area with known quality
concerns. Use focused mutation testing when static checks pass but the risk is in
behavioral coverage.

## Quick Start

Audit all tests:

```bash
uv run gobby test-quality audit
```

Audit a focused path:

```bash
uv run gobby test-quality audit tests/tasks
```

Write a baseline:

```bash
uv run gobby test-quality audit tests/tasks --write-baseline .gobby/test-quality-baseline.json
```

Fail only on new high-severity issues:

```bash
uv run gobby test-quality audit tests/tasks \
  --baseline .gobby/test-quality-baseline.json \
  --fail-on-new \
  --min-severity high
```

## Audit Findings

The analyzer detects patterns including:

- `ASSERT_TRUE`
- `SLEEP_IN_TEST`
- `TODO_IN_TEST`
- `UNCONDITIONAL_SKIP`
- `XFAIL_WITHOUT_STRICT_OR_REASON`
- `NO_ASSERTION`
- `ONLY_MOCK_ASSERTIONS`
- `HEAVY_MOCK_LOW_ASSERT`

Findings include file location, severity, issue code, and a fingerprint suitable
for baselines.

## Baselines

Baselines use schema version 1 and store issue fingerprints. A baseline run
compares the current report to the saved baseline, then reports only new issues
that meet the configured severity threshold.

Use baselines to keep existing debt visible without blocking unrelated cleanup.
Do not use them to hide new test weaknesses.

## Suppressions

Suppress a finding only with an explicit reason:

```python
# test-quality: allow NO_ASSERTION -- verifies fixture construction side effects
```

Suppression comments without a reason are not valid. Prefer making the test
stronger over suppressing the finding.

## Focused Mutation Testing

The project includes `mutmut` configuration:

```toml
[tool.mutmut]
paths_to_mutate = ["src/gobby"]
pytest_add_cli_args_test_selection = ["tests/"]
runner = "python -m pytest -q"
```

Use mutation testing for a focused module or task surface after adding targeted
tests. Keep the mutation scope narrow; running broad mutation testing across the
full project is too expensive for normal agent work.

## CLI

`gobby test-quality audit` options:

```bash
uv run gobby test-quality audit [PATH ...]
uv run gobby test-quality audit --format text
uv run gobby test-quality audit --format json --output report.json
uv run gobby test-quality audit --write-baseline .gobby/test-quality-baseline.json
uv run gobby test-quality audit --baseline .gobby/test-quality-baseline.json --fail-on-new
uv run gobby test-quality audit --min-severity low
```

`--fail-on-new` requires `--baseline`.

## HTTP

There is no dedicated HTTP API for test-quality audits. Treat audit output as a
local development artifact and include relevant results in task validation notes.

## MCP

There is no dedicated public test-quality MCP server. Agents run the CLI under
the claimed task and include the command output in task validation. If an audit
or pytest run exposes a failure, fix it before closing the task.

## File Locations

- `src/gobby/test_quality/cli.py`: audit command.
- `src/gobby/test_quality/analyzer.py`: AST analyzer.
- `src/gobby/test_quality/baseline.py`: baseline read/write and diff logic.
- `src/gobby/test_quality/models.py`: report and issue models.
- `src/gobby/test_quality/render.py`: text and JSON renderers.
- `src/gobby/cli/test_quality.py`: root CLI registration.
- `.gobby/test-quality-baseline.json`: conventional baseline path.
- `pyproject.toml`: `mutmut` configuration.

## See Also

- [testing.md](testing.md)
- [tdd-enforcement.md](tdd-enforcement.md)
- [tasks.md](tasks.md)
- [observability.md](observability.md)

_Last verified: 2026-05-08_
