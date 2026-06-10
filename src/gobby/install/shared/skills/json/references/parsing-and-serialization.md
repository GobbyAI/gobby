# JSON Parsing And Serialization

## Parser APIs

- Use structured JSON parsers and serializers instead of regex or string splice
  edits.
- For large files or streams, use an incremental parser when the language and
  library support it.
- Set explicit size, depth, and timeout limits when parsing untrusted input.

## Round Trips

- Round-trip with the same library or command used by the consuming system when
  comments, trailing commas, duplicate keys, number precision, or ordering could
  matter.
- Preserve stable formatting for files reviewed in Git and canonical formatting
  for files signed, hashed, cached, or generated.
- Avoid pretty-printing minified artifacts unless the repo expects checked-in
  expanded JSON.

## Data Conversion

- Validate dates, durations, binary data, decimal money values, IDs, and enum
  strings at the boundary instead of assuming host-language conversions are
  reversible.
- Preserve exact decimals or large integers with strings or decimal libraries
  when the domain requires precision.
- Avoid lossy map/object conversions in languages that do not preserve key order
  when the output is compared as text.

## Patch Formats

- JSON Patch and JSON Merge Patch have different semantics. Confirm which one
  the API expects before editing patches or examples.
- Test add, remove, replace, null, and missing-field behavior for patch changes.
