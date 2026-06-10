# Elixir Configuration

## Mix Project Shape

- Inspect `mix.exs`, `mix.lock`, umbrella app boundaries, aliases, compiler
  options, release settings, and application env before adding modules or deps.
- Keep dependency changes narrow. Match existing version constraints, optional
  deps, only/test/dev/prod flags, and umbrella app ownership.
- Preserve `elixirc_paths`, app start mode, extra applications, release steps,
  aliases, and compiler options unless the task explicitly changes them.
- In umbrellas, update the owning app, shared app, or top-level config according
  to existing dependency direction.

## Formatter And Static Analysis

- Use repo formatter settings from `.formatter.exs`; include new paths only when
  needed and keep import_deps intentional.
- Respect Credo config and inline suppressions. Fix warnings instead of adding
  broad disables.
- Respect Dialyzer PLT, ignore file, type checking scope, and existing
  `@dialyzer` attributes.
- Run Sobelow or security analysis when the repo already uses it and the change
  touches Phoenix, auth, file upload, or user input boundaries.

## Runtime Config

- Keep compile-time config in `config/config.exs` and runtime-only secrets in
  `config/runtime.exs` or the repo's equivalent.
- Do not read environment variables deep inside domain code when config is
  already passed through application env or supervised children.
- Keep dev/test/prod config differences explicit. Avoid making tests depend on
  prod-only settings or local machine env.

## Validation

- Prefer existing aliases such as `mix check`, `mix ci`, or app-specific test
  aliases.
- Focus commands on changed files or apps: `mix test path/to/test.exs`,
  `mix test --only tag`, or `mix test apps/app_name/test/...` in umbrellas.
- Compile after config/dependency changes and treat warnings as defects when the
  repo does.
