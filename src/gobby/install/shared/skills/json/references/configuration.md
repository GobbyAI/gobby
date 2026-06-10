# JSON Configuration

## Identify The Consumer

- Start by naming the program, library, service, or platform that reads the file.
- Check whether the file is strict JSON, JSONC, JSON5, package metadata,
  lockfile data, generated fixture data, schema, OpenAPI, or app-specific config.
- Follow the consumer's documented parser behavior before applying generic JSON
  assumptions.

## Dialect Boundaries

- Strict `.json` files do not allow comments, trailing commas, single-quoted
  strings, unquoted keys, `Infinity`, `NaN`, or hex numbers.
- JSONC and JSON5 allow some human-authored conveniences, but only use them when
  the exact target parser supports them.
- Do not rename `.jsonc` or `.json5` files to `.json` without removing dialect
  features and validating the strict parser.

## Formatting And Ordering

- Preserve indentation, newline-at-EOF policy, key ordering, and stable sorting
  conventions used by the repo.
- For generated JSON, update the source input or generator instead of hand
  editing the output unless the repo explicitly accepts manual edits.
- For lockfiles and package metadata, use the repo's package manager or a narrow
  verified edit path so integrity fields and ordering stay consistent.

## Config Ownership

- `package.json`, lockfiles, `tsconfig*.json`, `jsconfig*.json`, `deno.json`,
  `biome.json`, `.eslintrc`, `.prettierrc`, `.babelrc`, `.swcrc`,
  `.stylelintrc`, and `manifest.json` each have consumer-specific semantics.
- Preserve required fields, tool versions, environment assumptions, extension
  points, and generated sections.
- Check repo docs, schemas, examples, and existing validation commands before
  introducing a new config pattern.
