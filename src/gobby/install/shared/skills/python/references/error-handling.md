# Error Handling - Reference

## Exception Hierarchy

Define a base error for your domain, then specialize:

```python
class AppError(Exception):
    """Base for all application errors."""

class NotFoundError(AppError):
    def __init__(self, resource: str, identifier: str):
        super().__init__(f"{resource} not found: {identifier}")
        self.resource = resource
        self.identifier = identifier

class ValidationError(AppError):
    """Input failed validation at a boundary."""

class ExternalServiceError(AppError):
    """Wraps third-party library exceptions."""
    def __init__(self, service: str, cause: Exception):
        super().__init__(f"{service} failed: {cause}")
        self.service = service
```

Add machine-readable fields when callers need to branch. Avoid forcing callers to parse error text.

## Exception Chaining

Always chain when wrapping:

```python
try:
    result = external_api.fetch(resource_id)
except httpx.HTTPError as e:
    raise ExternalServiceError("payments", e) from e
```

Use `from None` only when hiding implementation details is intentional and the original exception would confuse users.

## Guard Clauses

```python
def process_order(order: Order) -> Receipt:
    if order.is_cancelled:
        raise ValidationError("Cannot process cancelled order")
    if not order.items:
        raise ValidationError("Order has no items")
    if order.total <= 0:
        raise ValidationError("Order total must be positive")

    # Happy path — no nesting
    receipt = charge_payment(order)
    send_confirmation(order, receipt)
    return receipt
```

Validate early at boundaries. Keep core logic working with already-typed domain values.

## Boundary Validation

Untrusted input includes CLI args, environment variables, JSON/YAML/TOML files, HTTP payloads, database rows, queue messages, and subprocess output:

```python
def parse_retry_count(raw: str | None) -> int:
    if raw is None:
        return 3
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValidationError("RETRY_COUNT must be an integer") from exc
    if value < 0:
        raise ValidationError("RETRY_COUNT must be non-negative")
    return value
```

Keep validators small and tested. Return typed values, not loosely validated dictionaries.

## Expected Failures

For expected domain outcomes, use explicit return types when exceptions would make normal control flow noisy:

```python
@dataclass(frozen=True)
class LookupFailure:
    code: Literal["not_found", "permission_denied"]
    user_id: str

type LookupResult = User | LookupFailure
```

Use exceptions for broken invariants, dependency failures, unavailable services, or unexpected third-party behavior.

## Process Boundary Pattern

Top-level handlers catch broadly for cleanup — this is the one place `except Exception` is appropriate:

```python
def main() -> int:
    try:
        app = create_app()
        app.run()
        return 0
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        return 130
    except Exception:
        logger.exception("Unhandled error — shutting down")
        return 1
```

The same boundary rule applies to web handlers, worker loops, scheduler jobs, and CLI commands. Lower layers should catch specific exceptions or let them propagate.

## Async And ExceptionGroup

When using `asyncio.TaskGroup`, multiple failures may be grouped. Handle the specific exception group at the boundary that can decide retry, cleanup, or reporting:

```python
try:
    async with asyncio.TaskGroup() as group:
        group.create_task(sync_users())
        group.create_task(sync_orders())
except* ExternalServiceError as group:
    for error in group.exceptions:
        logger.warning("external_sync_failed", extra={"service": error.service})
    raise
```

Do not swallow `CancelledError`; clean up and re-raise.

## Logging

Log once at the boundary that owns the failure. Include stable fields such as IDs, operation names, and dependency names. Never log secrets, raw tokens, cookies, or full credentials.
