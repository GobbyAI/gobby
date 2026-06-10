# YAML Anchors Tags And Templates

## Anchors And Aliases

- Anchors (`&name`) and aliases (`*name`) preserve shared structure. Do not
  inline, rename, or remove them without checking every alias.
- Merge keys can change override precedence. Verify the final expanded mapping
  before claiming behavior is unchanged.
- Keep anchor names descriptive when they are part of review or operational
  documentation.

## Tags

- Tags such as `!Ref`, `!Sub`, custom application tags, or language-specific tags
  are interpreted by the consumer. Treat them as executable semantics, not
  comments.
- Preserve tag placement and quoted values around tagged nodes.
- Validate with the owning platform because generic YAML parsers may reject or
  ignore tags that the platform accepts.

## Templates

- Helm, Jinja, GitHub Actions expressions, CloudFormation substitutions, and
  application templates can make a file invalid until rendered.
- Preserve delimiters, whitespace trimming, indentation helpers, include/import
  behavior, and variable scope.
- Render templates with representative values before relying on schema or parser
  checks against the source template.

## Refactoring

- When moving repeated YAML into anchors, templates, or shared values, verify the
  expanded output and target behavior.
- Avoid clever anchors or templates when a small explicit mapping is easier to
  review and safer for operators.
- Keep generated YAML snapshots or rendered manifests in sync if the repo tracks
  them.
