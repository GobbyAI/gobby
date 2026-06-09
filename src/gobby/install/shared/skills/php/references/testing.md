# PHP Testing

Use this reference when adding or changing PHPUnit/Pest tests, fixtures,
framework tests, snapshots, static-analysis checks, or Composer scripts.

## Test Scope

- Start with the narrowest test that proves the changed behavior.
- Add unit tests for domain logic, value objects, validation, error translation,
  and pure services.
- Add integration or slice tests when correctness depends on routing,
  middleware, container wiring, ORM mappings, database transactions, queues,
  events, serialization, or filesystem behavior.
- Broaden to full Composer/framework test scripts after focused tests when
  wiring or package metadata changed.

## PHPUnit And Pest

- Follow the repo's chosen test framework and naming conventions.
- Use data providers or Pest datasets for input variations.
- Assert exceptions, validation payloads, response status, response shape,
  emitted events, database side effects, and logs where those are observable
  behavior.
- Use temporary directories, fake clocks, fake queues/mailers/storage, and
  factories/builders to keep tests deterministic.

## Mocking

- Mock external systems and framework adapters, not simple value objects or the
  class under test.
- Prefer fakes for repositories, clocks, HTTP clients, and queues when stateful
  behavior matters.
- Avoid asserting incidental method call counts unless the interaction is the
  behavior.

## Framework Coverage

- Laravel: use HTTP tests, feature tests, model factories, database refresh
  traits, and fake queues/events/mail only where they prove framework behavior.
- Symfony: use kernel tests, WebTestCase, container overrides, and functional
  tests where routing or services matter.
- Doctrine/Eloquent: test query behavior with the database family that matches
  production when SQL or ORM semantics matter.

## Static Analysis And Commands

- Run PHPStan/Psalm on touched namespaces when types or PHPDoc change.
- Run formatter/linter checks on touched files.
- Example focused commands:
  - `vendor/bin/phpunit tests/Account/AccountLookupHandlerTest.php`
  - `vendor/bin/pest tests/Account/AccountLookupHandlerTest.php`
  - `vendor/bin/phpstan analyse src/Account src/Controller`
