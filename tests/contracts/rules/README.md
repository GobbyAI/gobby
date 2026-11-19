# Rule-pass corpus

This directory holds the committed contract for one rule pass: `RuleEngine.evaluate`
run over synthetic rules, plus the normalizer functions the rule conditions read.

- `manifest.json` names the schema version, the case kinds, the seams the recorder wraps,
  the required coverage sets and the platform record.
- `cases/*.json` hold the rule-pass cases. Each case defines synthetic rules, fires one
  event, and carries an `expect` block: outcome class, eager variable delta, the staged
  receipt payload, the effect log, the audit records, the non-default response fields and
  any skipped groups.
- `normalize_vectors.json` holds the normalizer vectors. Eleven entry points each have a
  vector, every cross-module function in `normalize_modules.json` has one, and 27 negative
  tests from `tests/workflows/test_command_matching.py` and
  `tests/hooks/test_code_navigation_recovery.py` replay through their vectors.
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
