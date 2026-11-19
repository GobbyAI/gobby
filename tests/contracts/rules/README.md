# Rule-pass corpus

This directory holds the committed contract for one rule pass: `RuleEngine.evaluate`
run over synthetic rules, plus the normalizer functions the rule conditions read.

- `manifest.json` names the schema version, the case kinds, the seams the recorder wraps,
  the required coverage sets and the platform record.
- `cases/*.json` hold the rule-pass cases. Each case defines synthetic rules, fires one
  event, and carries an `expect` block: outcome class, eager variable delta, the staged
  receipt payload, the effect log, the audit records, the non-default response fields and
  any skipped groups.
- A case of kind `seam_caller` drives one real caller of a seam instead of the rule engine,
  because the engine never reaches that seam: `background_delivery` runs the background
  run-command delivery, `tool_result_offload` runs the MCP proxy's result offload. Its
  `expect` adds the caller's `returned` value.
- `normalize_vectors.json` holds the normalizer vectors. Eleven entry points each have a
  vector, and every cross-module function in `normalize_modules.json` has one. Each of the
  151 listed functions whose parameters are all builtin types (`str`, `int`, `bool`, `bytes`,
  containers of those, or `Optional` of those) has at least one vector with a non-empty input
  and a result that is not an exception. The 148 realistic vectors (`id` prefix `realistic-`)
  hold calls captured from committed vectors and from shell commands run through the hook
  normalizer (87 vectors), or hand-written shell inputs where no captured call gave a stronger
  result (61 vectors): 41 return true, 100 return a value, and 7 return false or null for a
  realistic input that does not match. Every vector of a function that takes a parameter
  outside the builtin set, or no parameter, carries `metadata.reason`, a one-line note (146
  vectors, the gate vector included). A vector with `setup` builds a fixture checkout; a
  `bytes` argument is written `{"bytes": text}`.
  Every parametrize combination of the 27 negative tests in
  `tests/workflows/test_command_matching.py` and `tests/hooks/test_code_navigation_recovery.py`
  replays through its own vector. The two combinations of
  `test_refused_outline_opens_only_requested_read` differ in the refusal line their
  `gcode outline` event carries, so each vector replays the normalizer step that builds the
  gate's state from that event. The gate call itself is replayed once, by
  `gate-refused-outline-broad-read`, which carries no `negative_from` and so is not counted
  as a combination; the test asserts the gate too. Production records the same recovery for
  either code, so the line, not the code, changes the outcome.
  A vector with `scan_budget` meters the scanner it names and records whether the scan stayed
  within the test's budget.
- `normalize_modules.json` lists the 46 normalizer modules the vectors cover, with a goal
  clause for each.

## Synthetic only

Every rule id starts with `synthetic-`. No case or vector asserts the behavior of a bundled
rule. The bundled rule YAML is loaded through the engine loader only to check that each
file still validates. Case and vector JSON carry no absolute or transcript paths.

## Replay

Replay runs the engine and the normalizers against the committed expect blocks:

```bash
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test \
  GOBBY_TEST_PROTECT=1 uv run pytest tests/contracts/test_rules_corpus.py -q
```

The engine replay runs each case in an isolated schema on the test hub. A mismatch names
the case or vector id.

## Re-record

A deliberate engine or normalizer change is re-recorded, not hand-edited. Set
`GOBBY_CORPUS_RECORD=1` and run the re-record test; it writes the observed blocks back into
the case and vector files:

```bash
GOBBY_CORPUS_RECORD=1 DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test \
  GOBBY_TEST_PROTECT=1 uv run pytest tests/contracts/test_rules_corpus.py -k rerecord_committed_corpus
```

Review the diff before committing. A re-record that changes an expect block is a behavior
change and needs its own rationale.
