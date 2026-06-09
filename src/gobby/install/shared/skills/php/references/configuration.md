# PHP Configuration

Use this reference before changing Composer files, PHP version requirements,
autoloading, extensions, static-analysis config, style tooling, framework config,
or generated files.

## Composer

- Use the checked-in Composer workflow and lockfile policy.
- Keep `require` and `require-dev` scopes intentional.
- Do not change `config.platform.php`, extension requirements, minimum-stability,
  repository mirrors, plugin allowances, or scripts incidentally.
- Preserve `autoload` and `autoload-dev` namespace mappings; namespace changes
  can break framework discovery, package consumers, and tests.
- Run targeted scripts first, such as `composer test -- --filter AccountLookup`
  or the repo's equivalent.

## PHP Runtime

- Match the repo's PHP version and extension assumptions.
- Avoid using language features newer than the configured minimum PHP version.
- Keep INI, opcache, JIT, memory, timezone, and locale assumptions explicit
  where tests or runtime behavior depend on them.
- Do not rely on local extensions unless Composer or docs declare them.

## Static Analysis

- Treat PHPStan/Psalm levels, baselines, generics, and stubs as part of the
  public safety contract.
- Do not add new baseline entries for code you touched unless the baseline is an
  explicit migration artifact and the task calls for it.
- Keep suppressions local, documented, and tied to framework or extension
  boundaries.
- Prefer precise PHPDoc over broad `mixed`, `array`, or `object`.

## Style And Refactoring Tools

- Follow configured Pint, PHP-CS-Fixer, PHPCS, ECS, Rector, and editorconfig.
- Run dry-run refactor tools before applying broad transformations.
- Avoid unrelated style churn across generated code, vendor directories,
  migrations, snapshots, and fixtures.

## Framework Config

- Keep env-specific config, service discovery, route registration, event wiring,
  and cache warmup behavior in sync.
- Avoid global config changes when a local service registration or test override
  proves the behavior.
