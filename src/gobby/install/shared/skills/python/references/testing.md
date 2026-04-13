# Testing — Reference

## Fixtures with Yield

```python
@pytest.fixture
def db() -> Generator[Database, None, None]:
    database = Database(":memory:")
    database.connect()
    yield database
    database.disconnect()
```

## Parameterized Tests

```python
@pytest.mark.parametrize("email,valid", [
    ("user@example.com", True),
    ("no-at-sign", False),
    ("", False),
])
def test_email_validation(email: str, valid: bool) -> None:
    assert is_valid_email(email) == valid
```

Use `pytest.param` for cases that need IDs or marks:

```python
@pytest.mark.parametrize("val,expected", [
    pytest.param(0, False, id="zero"),
    pytest.param(-1, False, id="negative"),
    pytest.param(1, True, id="positive"),
])
def test_is_positive(val: int, expected: bool) -> None:
    assert (val > 0) == expected
```

## Mocking External Dependencies

Patch where the thing is **used**, not where it's defined:

```python
# myapp/service.py imports requests
# Patch in myapp.service, not in requests
@patch("myapp.service.requests.get")
def test_fetch_user(mock_get: Mock) -> None:
    mock_get.return_value.json.return_value = {"id": 1, "name": "Test"}
    mock_get.return_value.raise_for_status = Mock()

    user = fetch_user(1)

    assert user["name"] == "Test"
    mock_get.assert_called_once()
```

## Testing Error Paths

```python
def test_fetch_retries_on_transient_error() -> None:
    client = Mock()
    client.request.side_effect = [
        ConnectionError("down"),
        ConnectionError("still down"),
        {"status": "ok"},
    ]
    service = RetryingService(client, max_retries=3)
    assert service.fetch() == {"status": "ok"}
    assert client.request.call_count == 3

def test_fetch_raises_after_max_retries() -> None:
    client = Mock()
    client.request.side_effect = ConnectionError("down")
    service = RetryingService(client, max_retries=3)
    with pytest.raises(ConnectionError):
        service.fetch()
```

## Async Tests

```python
@pytest.mark.asyncio
async def test_async_fetch() -> None:
    async with AsyncClient(app=app) as client:
        response = await client.get("/users/1")
    assert response.status_code == 200
```

## Markers

```python
@pytest.mark.slow          # Expensive tests
@pytest.mark.integration   # Requires external services
@pytest.mark.e2e           # Full system tests

# Run only fast tests:  pytest -m "not slow"
# Run integration:       pytest -m integration
```

## Freezing Time

```python
from freezegun import freeze_time

@freeze_time("2026-01-15 10:00:00")
def test_token_expiry() -> None:
    token = create_token(expires_in=3600)
    assert token.expires_at == datetime(2026, 1, 15, 11, 0, 0)
```
