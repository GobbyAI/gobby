# C# Framework Boundaries

Keep framework code thin and domain code portable.

## ASP.NET Core

- Controllers, minimal APIs, endpoint filters, middleware, and Razor/Blazor
  components should validate inputs, call typed services, and map responses.
- Put business decisions in domain/application services, not route handlers,
  attributes, filters, or model binders.
- Use typed request/response models. Avoid passing `HttpContext`, raw JSON,
  service providers, or framework abstractions into domain code.
- Return stable status codes, problem details, headers, and response contracts.

## Dependency Injection

- Register services with lifetimes that match ownership: singleton for
  stateless thread-safe services, scoped for request/unit-of-work state,
  transient for cheap stateless objects.
- Do not inject scoped services into singletons or cache scoped dependencies.
- Avoid service locator patterns. Constructor inject explicit dependencies.
- Validate options at startup when invalid configuration should stop the app.

## Hosted Services And Jobs

- Use `BackgroundService`, durable queues, or scheduler abstractions for
  background work.
- Create scopes for scoped dependencies inside hosted services.
- Handle cancellation, retries, idempotency, logging, and failure visibility.
- Do not start untracked tasks from request paths.

## Configuration And Options

- Bind configuration to typed options and validate required fields.
- Keep secrets in secret stores or environment-specific providers, not source.
- Avoid reading environment variables deep in business logic.

## Observability

- Log at boundaries with structured context and no secrets.
- Emit metrics/traces for queue, HTTP, database, and external service latency.
- Keep correlation IDs and cancellation context flowing across async calls.
