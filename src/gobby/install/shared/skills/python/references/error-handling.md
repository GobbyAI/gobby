# Error Handling — Reference

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

## Exception Chaining

Always chain when wrapping:

```python
try:
    result = external_api.fetch(resource_id)
except httpx.HTTPError as e:
    raise ExternalServiceError("payments", e) from e
```

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
