# YAML Syntax And Types

## Structure

- YAML structure is indentation-sensitive. Keep sibling indentation aligned and
  avoid mixing tabs with spaces.
- Preserve whether a value is a mapping, sequence, scalar, or null. A one-line
  change can alter the type consumed by the platform.
- For multi-document streams, keep document order and `---` boundaries
  intentional.
- Do not remove comments that carry operational meaning, schema hints, or
  migration instructions.

## Scalars

- Quote strings that can be misread by YAML 1.1 parsers, YAML 1.2 parsers, or the
  target platform: `on`, `off`, `yes`, `no`, dates, times, version numbers,
  leading-zero values, colon-containing strings, glob patterns, `*` prefixes,
  `!` prefixes, and values with `#`.
- Use block scalars intentionally:
  - `|` preserves newlines and is usually appropriate for scripts, certificates,
    multiline messages, and embedded config.
  - `>` folds newlines and is usually appropriate for prose, not scripts.
- Keep null representation consistent. Empty values, `null`, and `~` are not
  always equivalent for downstream tooling.

## Mappings

- Detect duplicate keys. Many parsers accept the last duplicate silently, which
  can hide dangerous changes.
- Preserve key order when it has human review value or target semantics.
- Be careful with flow style maps and arrays. They are compact, but harder to
  diff and easier to misquote.

## Numbers And Booleans

- Treat numeric-looking identifiers as strings unless the consumer expects a
  number: ZIP codes, ports with leading zeros, account IDs, versions, dates, and
  image tags should often be quoted.
- Avoid relying on implicit boolean parsing. Quote strings such as `on`, `off`,
  `yes`, `no`, and `none` when the value is not intentionally boolean/null.
- Preserve units explicitly where the platform supports them, such as memory,
  CPU, timeout, and duration fields.
