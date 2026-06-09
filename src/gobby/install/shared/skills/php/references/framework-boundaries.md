# PHP Framework Boundaries

Use this reference when changing Laravel, Symfony, Doctrine, Eloquent, PSR
interfaces, dependency injection, routing, middleware, queues, events, commands,
or serialization.

## Boundary Shape

- Keep controllers, commands, jobs, listeners, middleware, repositories, and ORM
  models at the edge when domain logic can remain plain PHP.
- Translate request objects, container services, ORM entities, framework
  collections, and response objects into domain contracts before core logic.
- Keep framework annotations/attributes out of domain types unless the repo
  intentionally couples domain and framework layers.
- Prefer constructor injection and explicit interfaces over service locators or
  global helpers unless local framework conventions require them.

## Laravel

- Validate requests through Form Requests, validators, or explicit DTO factories.
- Keep authorization in policies, gates, middleware, or explicit use-case checks.
- Avoid hiding database work in accessors, observers, events, or lazy-loaded
  relationships without tests.
- Use transactions around multi-write use cases.

## Symfony

- Keep controller actions thin; validate and map request data before services.
- Preserve service visibility, autowiring, tags, compiler passes, and env config.
- Use Messenger, EventDispatcher, Serializer, Validator, and Security components
  through explicit boundary adapters.
- Test route, container, and security behavior when configuration changes.

## Persistence

- Avoid lazy-loading surprises in serializers, templates, logs, and tests.
- Keep migrations, entity mappings, repository queries, and fixtures aligned.
- Test database-specific behavior with a matching database family where SQL or
  ORM semantics are the change.

## PSR And Package Boundaries

- Respect PSR-3 logging, PSR-7/15 HTTP, PSR-11 container, PSR-14 events, and
  PSR-18 client contracts where the package exposes them.
- Avoid binding package APIs to one framework unless the package is explicitly
  framework-specific.
