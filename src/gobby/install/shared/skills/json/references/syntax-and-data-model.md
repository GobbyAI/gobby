# JSON Syntax And Data Model

## Core Shape

- JSON values are objects, arrays, strings, numbers, booleans, and null.
- Top-level arrays and primitives are valid JSON, but many consumers require an
  object. Follow the consumer contract.
- Object member ordering is not semantically significant in JSON itself, but
  many repos use stable order for reviewability, generated files, snapshots, or
  canonical signatures.

## Duplicate Keys

- Duplicate object keys are a bug unless the consumer documents a merge rule.
- Different parsers may keep the first value, keep the last value, reject the
  file, or expose all pairs.
- When changing generated or third-party JSON, run the same parser the consumer
  uses so duplicate-key behavior is not guessed.

## Numbers

- JSON numbers do not encode integer size, decimal precision, `NaN`,
  `Infinity`, or `-Infinity`.
- JavaScript consumers may lose precision for integers outside the safe integer
  range; database, Python, Rust, Go, and JVM consumers may impose different
  limits.
- Preserve numeric strings when IDs, versions, account numbers, timestamps, or
  exact decimals must not be rounded.

## Strings And Nulls

- Escape control characters and preserve required Unicode escaping conventions.
- Distinguish missing fields, explicit `null`, empty strings, empty arrays, and
  empty objects. Consumers often treat them differently.
- Do not convert booleans or numbers to strings unless the schema or platform
  requires stringly typed values.
